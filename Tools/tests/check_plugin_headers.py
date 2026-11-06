#!/usr/bin/env python3
"""Compiles the plugin's C++ against real engine headers with cl.exe /Zs (no code generation).

Needs only an unpacked Engine/Source tree (no Setup.bat, no engine build), so it catches API
mismatches in seconds. UHT does not run: *.generated.h are stubbed, which is why this does not
replace Tools\\BuildPlugin.bat for the final check.

    python check_plugin_headers.py --source <dir holding Runtime, Developer, Editor>
        --cl <cl.exe> --sdk 10.0.26100.0 --version 426|58 [--only substring]
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

PLUGIN = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Plugin", "DistriBuild", "Source"))

DEFINES = """UE_BUILD_DEVELOPMENT=1 UE_BUILD_DEBUG=0 UE_BUILD_TEST=0 UE_BUILD_SHIPPING=0 UE_GAME=0 UE_EDITOR=1 UE_SERVER=0
WITH_EDITOR=1 WITH_EDITORONLY_DATA=1 WITH_ENGINE=1 WITH_UNREAL_DEVELOPER_TOOLS=1 WITH_UNREAL_TARGET_DEVELOPER_TOOLS=1
WITH_PLUGIN_SUPPORT=1 WITH_APPLICATION_CORE=1 WITH_COREUOBJECT=1 WITH_SERVER_CODE=1 WITH_PERFCOUNTERS=1
IS_MONOLITHIC=0 IS_PROGRAM=0 PLATFORM_WINDOWS=1 PLATFORM_MICROSOFT=1 OVERRIDE_PLATFORM_HEADER_NAME=Windows
UBT_COMPILED_PLATFORM=Windows UBT_COMPILED_TARGET=Editor UNICODE _UNICODE __UNREAL__ _WIN32_WINNT=0x0601 WINVER=0x0601
NTDDI_VERSION=0x06010000 UE_IS_ENGINE_MODULE=0 UE_ENABLE_ICU=1 WITH_VS_PERF_PROFILER=0 WITH_CONCURRENCYVIEWER_PROFILER=0
UE_MERGED_MODULES=0 WINDOWS_MAX_NUM_TLS_SLOTS=2048 WINDOWS_MAX_NUM_THREADS_WITH_TLS_SLOTS=512 WITH_VERSE=0 WITH_VERSE_VM=0 FORCE_ANSI_ALLOCATOR=0 USE_LOCALIZED_PACKAGE_CACHE=1 _CRT_SECURE_NO_WARNINGS""".split()

# Stands in for the macros UHT normally generates. Engine classes only have to stay parseable.
PRELUDE = """#pragma once
#include <strsafe.h>
#include "CoreMinimal.h"
#include "UObject/ObjectMacros.h"
#include "Templates/Greater.h"
#undef BODY_MACRO_COMBINE
#define BODY_MACRO_COMBINE(...)
#undef UCLASS
#define UCLASS(...)
#undef UINTERFACE
#define UINTERFACE(...)
#undef GENERATED_BODY
#undef GENERATED_UCLASS_BODY
#undef GENERATED_USTRUCT_BODY
#undef GENERATED_UINTERFACE_BODY
#undef GENERATED_IINTERFACE_BODY
#define DISTRIBUILD_STUB_BODY public: static class UClass* StaticClass(); static class UScriptStruct* StaticStruct();
#define GENERATED_BODY(...) DISTRIBUILD_STUB_BODY
#define GENERATED_UCLASS_BODY(...) DISTRIBUILD_STUB_BODY
#define GENERATED_USTRUCT_BODY(...) DISTRIBUILD_STUB_BODY
#define GENERATED_UINTERFACE_BODY(...) DISTRIBUILD_STUB_BODY
#define GENERATED_IINTERFACE_BODY(...) DISTRIBUILD_STUB_BODY
"""


def module_dirs(source):
    """Every directory UBT would plausibly put on the include path for a module."""
    dirs = [source, os.path.join(source, "Runtime", "Launch", "Resources"), os.path.join(source, "ThirdParty")]
    for group in ("Runtime", "Developer", "Editor"):
        root = os.path.join(source, group)
        dirs.append(root)
        for base, subdirs, _ in os.walk(root):
            if base[len(root):].count(os.sep) > 3:
                subdirs[:] = []
                continue
            for name in ("Public", "Classes", "Internal"):
                if name in subdirs:
                    dirs.append(os.path.join(base, name))
                    subdirs.remove(name)
    return dirs


def scan_headers(source, stub_dir):
    """Creates an empty stub for every *.generated.h and collects <MODULE>_API macro names."""
    generated = re.compile(rb'#\s*include\s*"([^"/\\]+\.generated\.h)"')
    api = re.compile(rb"\b([A-Z][A-Z0-9_]*_API)\b")
    macros = {"DISTRIBUILD_API", "DISTRIBUILDEDITOR_API"}
    for group in ("Runtime", "Developer", "Editor"):
        for base, _, files in os.walk(os.path.join(source, group)):
            for name in files:
                if not name.endswith((".h", ".inl")):
                    continue
                try:
                    with open(os.path.join(base, name), "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                macros.update(m.group(1).decode() for m in api.finditer(data))
                for m in generated.finditer(data):
                    stub = os.path.join(stub_dir, m.group(1).decode())
                    if not os.path.exists(stub):
                        open(stub, "w").close()
    with open(os.path.join(stub_dir, "DistriBuildSettings.generated.h"), "w") as f:
        f.write("#undef GENERATED_BODY\n#define GENERATED_BODY(...) public: typedef UDeveloperSettings Super; "
                "static class UClass* StaticClass();\n")
    return {m for m in macros if not m.startswith("UE_")}  # UE_VALIDATE_INTERNAL_API and friends are not exports


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--cl", required=True)
    ap.add_argument("--sdk", required=True)
    ap.add_argument("--version", required=True, choices=["426", "58"])
    ap.add_argument("--only")
    args = ap.parse_args()

    vc_root = os.path.normpath(os.path.join(os.path.dirname(args.cl), "..", "..", ".."))
    kit = os.path.join(r"C:\Program Files (x86)\Windows Kits\10\Include", args.sdk)
    tmp = tempfile.mkdtemp(prefix="dbcheck_")
    macros = scan_headers(args.source, tmp)
    with open(os.path.join(tmp, "prelude.h"), "w") as f:
        f.write(PRELUDE)

    ue5 = args.version == "58"
    lines = ["/nologo", "/Zs", "/EHsc", "/GR-", "/bigobj", "/W1", "/utf-8", "/wd4996", "/wd5054", "/wd4005", "/wd4141",
             "/std:c++20" if ue5 else "/std:c++17", "/Zc:preprocessor" if ue5 else "/Zc:wchar_t",
             '/FI"%s"' % os.path.join(tmp, "prelude.h")]
    lines += ["/D%s" % d for d in DEFINES]
    lines += ["/D%s=" % m for m in sorted(macros)]
    lines += ['/DUE_MODULE_NAME=\\"DistriBuild\\"', '/DUE_PLUGIN_NAME=\\"DistriBuild\\"',
              '/DUE_PROJECT_NAME=\\"x\\"', '/DUE_TARGET_NAME=\\"x\\"']
    includes = [tmp] + module_dirs(args.source)
    includes += [os.path.join(PLUGIN, m, s) for m in ("DistriBuild", "DistriBuildEditor") for s in ("Public", "Private")]
    includes += [os.path.join(args.source, "Developer", "DistributedBuildInterface", "Public")]
    includes += [os.path.join(vc_root, "include"), os.path.join(kit, "ucrt"), os.path.join(kit, "um"), os.path.join(kit, "shared")]
    lines += ['/I"%s"' % d for d in includes]
    rsp = os.path.join(tmp, "args.rsp")
    with open(rsp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    sources = sorted(glob.glob(os.path.join(PLUGIN, "*", "Private", "*.cpp")))
    if args.only:
        sources = [s for s in sources if args.only in s]

    def compile_one(src):
        p = subprocess.run([args.cl, "@" + rsp, src], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=dict(os.environ, VSLANG="1033"))
        return src, p.returncode, p.stdout.decode("utf-8", "replace")

    failed = 0
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for src, code, out in ex.map(compile_one, sources):
            errors = [l for l in out.splitlines() if ": error " in l or ": fatal error " in l]
            # Engine-header errors come from the stubbed UHT output; only the plugin's own lines count.
            mine = [l for l in errors if l.startswith(PLUGIN) or ": fatal error " in l]
            print("%-34s %s" % (os.path.basename(src), "%d errors" % len(mine) if mine else
                                "ok (%d engine-header errors ignored)" % len(errors) if errors else "ok"))
            if mine:
                failed += 1
                for line in mine[:30]:
                    print("    " + line.replace(PLUGIN + os.sep, ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
