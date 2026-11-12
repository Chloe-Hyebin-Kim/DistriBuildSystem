#!/usr/bin/env python3
"""End-to-end tests with the real MSVC toolchain. Nothing is mocked except ShaderCompileWorker.

Task files and response files follow what XGE.cs and VCToolChain.cs write in UE 4.26 and 5.8, so
the code paths are the ones a UBT build takes. The agent runs on this machine, on another port.

    python e2e_windows.py [--cl-filter-source <Engine/Extras/Windows/cl-filter/cl-filter.cpp>]
    python e2e_windows.py --ue-source <dir holding Runtime/Core> --ue-version 58   # also a real engine file
"""

import argparse
import csv
import glob
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "DistriBuild"))
sys.path[:0] = [TOOLS, HERE]
# The agents here stand for other PCs; builds would otherwise leave this PC's own agent out.
os.environ["DISTRIBUILD_AGENT_MACHINE"] = "e2e-helper"
from check_plugin_headers import DEFINES, scan_headers  # noqa: E402
from common import PROTOCOL_VERSION, recv_frame, remap_shader_source_dirs, request, send_frame  # noqa: E402

PCH = """#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string>
#include <vector>
#include <map>
#define PCH_VALUE 40
"""
LIB = """#include "shared.h"
#include "lib_%(n)d.h"
int value_%(n)d() { return PCH_VALUE + LIB_%(n)d + (int)std::string(__FILE__).size() * 0; }
"""
MAIN = """#include "shared.h"
#include <cstdio>
%(decls)s
int main() { int v = 0; %(calls)s printf("%%d %%s\\n", v, COMPILE_DEFINE); return 0; }
"""
CSRC = "int c_value(void) { return 7; }\n"
# UE's Windows headers turn hundreds of warnings into errors; /external:W0 keeps them out of SDK and
# third-party headers. Without that treatment this header breaks every UE 5 compile on the agent.
SHARED = """#pragma once
#ifdef TEST_EXTERNAL
#pragma warning(error: 4141)
#endif
#include "extra.h"
"""
EXTRA = """#pragma once
#pragma warning(disable: 4996)
#ifdef TEST_EXTERNAL
__inline inline int from_external_header(int unused) { return 1; }
#endif
"""
BROKEN = '#include "shared.h"\nint broken() { return undeclared_symbol_xyz; }\n'
WARNS = '#include "shared.h"\nint warns() { int local_never_used_xyz = 1; return 2; }\n'


def find_toolchain():
    vswhere = os.path.join(os.environ["ProgramFiles(x86)"], "Microsoft Visual Studio", "Installer", "vswhere.exe")
    installs = subprocess.run([vswhere, "-all", "-products", "*", "-property", "installationPath"],
                              stdout=subprocess.PIPE).stdout.decode().splitlines()
    found = []
    for install in filter(None, installs):
        found += glob.glob(os.path.join(install, "VC", "Tools", "MSVC", "*", "bin", "Hostx64", "x64", "cl.exe"))
    # Prefer the VS 2022 toolchain, the one UE 5.8 officially builds with.
    cl = sorted(found, key=lambda path: ("14.4" in path, path))[-1]
    vc = os.path.normpath(os.path.join(os.path.dirname(cl), "..", "..", ".."))
    kit_inc = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\Include\10.*"))[-1]
    sdk = os.path.basename(kit_inc)
    kit_lib = os.path.join(os.path.dirname(os.path.dirname(kit_inc)), "Lib", sdk)
    env = dict(os.environ)
    env["INCLUDE"] = ";".join([os.path.join(vc, "include"), os.path.join(kit_inc, "ucrt"), os.path.join(kit_inc, "um"),
                               os.path.join(kit_inc, "shared")])
    env["LIB"] = ";".join([os.path.join(vc, "lib", "x64"), os.path.join(kit_lib, "ucrt", "x64"), os.path.join(kit_lib, "um", "x64")])
    env["PATH"] = os.path.dirname(cl) + ";" + env["PATH"]
    return cl, os.path.join(os.path.dirname(cl), "link.exe"), env


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Results:
    def __init__(self):
        self.failed = []

    def check(self, ok, text, detail=""):
        print("  %s %s" % ("PASS" if ok else "FAIL", text))
        if not ok:
            self.failed.append(text)
            if detail:
                print("       " + detail.replace("\n", "\n       ")[:3000])


R = Results()


class TaskFile:
    """Writes the XML exactly as UBT's XGE.WriteTaskFile does for each engine version."""

    def __init__(self, ue5):
        self.ue5 = ue5
        self.tools, self.tasks = [], []

    def add(self, status, path, params, cwd, remote, deps=(), compile_action=True, produced=()):
        i = len(self.tools)
        prefix = ("@action_%d %s" % (i, status)) if self.ue5 else status
        tool = {"Name": "Tool%d" % i, "AllowRemote": str(remote), "AllowIntercept": str(remote), "OutputPrefix": prefix,
                "Params": params, "Path": path, "SkipIfProjectFailed": "true", "AutoReserveMemory": "*.pch",
                "OutputFileMasks": ",".join(produced)}
        if compile_action and self.ue5:
            tool["AutoRecover"] = "C1060,C1076,C2855,C3435,C3859"
        task = {"SourceFile": "", "Name": "Action%d" % i, "Tool": "Tool%d" % i, "WorkingDir": cwd, "SkipIfProjectFailed": "true",
                "AllowRestartOnLocal": "true"}
        if self.ue5:
            task["Caption"] = status  # 4.26 only writes Caption for actions that print no status of their own
        if deps:
            task["DependsOn"] = ";".join("Action%d" % d for d in deps)
        self.tools.append(tool)
        self.tasks.append(task)
        return i

    def write(self, path):
        root = ET.Element("BuildSet", FormatVersion="1")
        envs = ET.SubElement(ET.SubElement(root, "Environments"), "Environment", Name="Default")
        tools = ET.SubElement(envs, "Tools")
        for t in self.tools:
            ET.SubElement(tools, "Tool", t)
        project = ET.SubElement(root, "Project", Name="Default", Env="Default")
        for t in self.tasks:
            ET.SubElement(project, "Task", t)
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Project:
    """A small module with a shared PCH, laid out the way UBT lays out an engine module."""

    COUNT = 6

    def __init__(self, root, ue5, cl, link, cl_filter, debug_flag="/Z7", broken=False, relative_pch=False):
        self.root, self.ue5, self.cl, self.link, self.cl_filter = root, ue5, cl, link, cl_filter
        self.debug_flag, self.broken, self.relative_pch = debug_flag, broken, relative_pch
        self.src = os.path.join(root, "Source", "모듈")  # non-ASCII on purpose: UBT reads these paths back
        self.inc = os.path.join(root, "Source", "Public")
        self.ext = os.path.join(root, "Source", "ThirdParty")
        self.out = os.path.join(root, "Intermediate", "Build")
        self.cwd = os.path.join(root, "Source")
        os.makedirs(self.out, exist_ok=True)
        write_text(os.path.join(self.inc, "SharedPCH.h"), PCH)
        write_text(os.path.join(self.inc, "shared.h"), SHARED)
        write_text(os.path.join(self.ext, "extra.h"), EXTRA)
        decls, calls = [], []
        for n in range(self.COUNT):
            write_text(os.path.join(self.src, "lib_%d.h" % n), "#define LIB_%d %d\n" % (n, n))
            write_text(os.path.join(self.src, "lib_%d.cpp" % n), LIB % {"n": n})
            decls.append("int value_%d();" % n)
            calls.append("v += value_%d();" % n)
        write_text(os.path.join(self.src, "main.cpp"), MAIN % {"decls": "\n".join(decls), "calls": " ".join(calls)})
        write_text(os.path.join(self.src, "cfile.c"), CSRC)
        if broken:
            write_text(os.path.join(self.src, "broken.cpp"), BROKEN)
        write_text(os.path.join(self.src, "warns.cpp"), WARNS)

    def shared_args(self):
        args = ["/nologo", "/c", "/EHsc", "/MD", "/W4", "/O1", "/Gy", "/GF", "/GR-", "/bigobj", "/fp:fast", "/Zc:inline", "/Zc:wchar_t",
                "/utf-8", '/D"COMPILE_DEFINE=\\"hello world\\""', self.debug_flag,
                '/I "%s"' % self.inc, '/I "%s"' % self.src]
        if self.debug_flag == "/Zi":
            args += ["/FS", '/Fd"%s"' % os.path.join(self.out, "module.pdb")]
        if self.ue5:
            args += ["/std:c++20", "/Zc:preprocessor", "/permissive-", "/Zc:__cplusplus", "/external:W0",
                     '/external:I "%s"' % self.ext, "/experimental:deterministic", "/DTEST_EXTERNAL"]
        else:
            args += ["/std:c++17", '/I "%s"' % self.ext]
        if self.relative_pch:
            args.append('/I "."')  # /FI looks in include paths, not the working directory
        # A real module has hundreds of include paths; together they exceed a Windows command line.
        args += ['/I "%s"' % os.path.join(self.root, "Source", "Runtime", "SomeVeryLongModuleNameForPadding%04d" % i, "Public")
                 for i in range(450)]
        return args

    def compile_action(self, name, extra):
        """Returns (tool path, params, dependency file): what VCToolChain produces for one source."""
        rsp = os.path.join(self.out, name + ".rsp")
        dep = os.path.join(self.out, name + (".json" if self.ue5 else ".txt"))
        lines = self.shared_args() + extra
        if self.ue5:
            lines.append('/sourceDependencies "%s"' % dep)
            lines.append('/experimental:log "%s"' % os.path.join(self.out, name + ".sarif"))
        with open(rsp, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines))
        if self.ue5:
            return self.cl, '@"%s"' % rsp, dep
        params = '-dependencies="%s" -compiler="%s" -stderronly -- "%s" @"%s" /showIncludes' % (dep, self.cl, self.cl, rsp)
        return self.cl_filter, params, dep

    def task_file(self, path, seed_pch=False):
        """seed_pch: only create the PCH and compile one file, which sends the PCH to the agent."""
        tf = TaskFile(self.ue5)
        pch_h = os.path.join(self.inc, "SharedPCH.h")
        pch = os.path.join(self.out, "SharedPCH.h.pch")
        pch_cpp = os.path.join(self.out, "SharedPCH.cpp")
        write_text(pch_cpp, '#include "%s"\n' % pch_h)
        if self.relative_pch:
            # UBT's spelling for engine and plugin modules: relative to Engine/Source, starting with "..".
            wrapper = os.path.join(self.out, "RelativeSharedPCH.h")
            write_text(wrapper, '#include "SharedPCH.h"\n')
            write_text(pch_cpp, "\n")
            pch_h = os.path.relpath(wrapper, self.cwd)
        pch_obj = os.path.join(self.out, "SharedPCH.h.obj")
        tool, params, _ = self.compile_action("SharedPCH", ['"%s"' % pch_cpp, '/Yc"%s"' % pch_h, '/Fp"%s"' % pch,
                                                            '/FI"%s"' % pch_h, '/Fo"%s"' % pch_obj])
        pch_task = tf.add("SharedPCH.h", tool, params, self.cwd, False)
        if seed_pch:
            tf.tools[-1]["AllowRemote"] = "False"
        self.objs = [pch_obj]
        compile_tasks = []
        names = ["lib_%d.cpp" % n for n in range(self.COUNT)] + ["main.cpp", "warns.cpp"] + (["broken.cpp"] if self.broken else [])
        if seed_pch:
            names = names[:1]
            pch_task = None
        elif getattr(self, "pch_exists", False):
            tf.tools.pop()
            tf.tasks.pop()
            pch_task = None
        for name in names:
            obj = os.path.join(self.out, name + ".obj")
            self.objs.append(obj)
            tool, params, _ = self.compile_action(name, ['"%s"' % os.path.join(self.src, name), '/Yu"%s"' % pch_h,
                                                         '/Fp"%s"' % pch, '/FI"%s"' % pch_h, '/Fo"%s"' % obj])
            compile_tasks.append(tf.add(name, tool, params, self.cwd, True, deps=[] if pch_task is None else [pch_task], produced=[name + ".obj"]))
        if seed_pch:
            # The PCH action must finish before the one compile, or the compile finds no PCH to send.
            tf.tasks[-1]["DependsOn"] = "Action0"
            tf.write(path)
            self.pch_exists = True
            return
        c_obj = os.path.join(self.out, "cfile.c.obj")
        self.objs.append(c_obj)
        tool, params, _ = self.compile_action("cfile.c", ['"%s"' % os.path.join(self.src, "cfile.c"), '/Fo"%s"' % c_obj])
        compile_tasks.append(tf.add("cfile.c", tool, params, self.cwd, True, produced=["cfile.c.obj"]))
        self.exe = os.path.join(self.out, "module.exe")
        link_rsp = os.path.join(self.out, "module.link.rsp")
        with open(link_rsp, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(['/NOLOGO', '/OUT:"%s"' % self.exe, "/DEBUG"] + ['"%s"' % o for o in self.objs]))
        tf.add("module.exe", self.link, '@"%s"' % link_rsp, self.cwd, False, deps=compile_tasks, compile_action=False)
        tf.write(path)


def run_xgconsole(task_xml, cfg, env, report_dir):
    cfg_path = os.path.join(report_dir, "client.json")
    os.makedirs(report_dir, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(dict(cfg, report_dir=report_dir, discovery=False), f)
    run_env = dict(env, DISTRIBUILD_CONFIG=cfg_path)
    # The same arguments XGE.ExecuteTaskFile passes.
    args = [sys.executable, os.path.join(TOOLS, "xgconsole.py"), task_xml, "/Rebuild", "/NoWait", "/NoLogo", "/ShowAgent", "/ShowTime",
            "/Title=UnrealBuildTool Compile", "/UseIdeMonitor"]
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=run_env, timeout=600)
    text = p.stdout.decode(sys.stdout.encoding or "utf-8", "replace")
    rows = []
    report = os.path.join(report_dir, "last_build.csv")
    if os.path.isfile(report):
        with open(report, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    return p.returncode, text, rows


def dep_set(path):
    with open(path, "rb") as f:
        raw = f.read()
    if path.endswith(".json"):
        return set(os.path.normcase(os.path.normpath(i)) for i in json.loads(raw.decode("utf-8"))["Data"]["Includes"])
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("mbcs")
    return set(os.path.normcase(os.path.normpath(l.strip())) for l in text.splitlines() if l.strip())


def test_build(label, ue5, cl, link, cl_filter, env, agent_port, tmp, debug_flag):
    print("[%s, %s]" % (label, debug_flag))
    remote_root = os.path.join(tmp, label + debug_flag[1:], "remote")
    local_root = os.path.join(tmp, label + debug_flag[1:], "local")

    # Plain local build: the reference for dependency files and program output.
    project = Project(local_root, ue5, cl, link, cl_filter, debug_flag)
    xml = os.path.join(local_root, "XGETasks.xml")
    project.task_file(xml)
    code, text, rows = run_xgconsole(xml, {"agents": []}, env, os.path.join(local_root, "report"))
    R.check(code == 0 and os.path.isfile(project.exe), "local-only build links", text)
    if code != 0:
        return
    reference = subprocess.run([project.exe], stdout=subprocess.PIPE).stdout.decode()
    reference_deps = {os.path.basename(d): dep_set(d) for d in glob.glob(os.path.join(project.out, "lib_0.cpp.*")) if d.endswith((".json", ".txt"))}

    # Distributed build: the PCH goes first, as it would in a real build where other compiles run
    # locally meanwhile. Then everything that can go remote must go remote.
    # 5.8 runs use a relative PCH path and 4.26 runs an absolute one, so both spellings stay covered.
    project = Project(remote_root, ue5, cl, link, cl_filter, debug_flag, relative_pch=ue5)
    xml = os.path.join(remote_root, "XGETasks.xml")
    client = {"agents": ["127.0.0.1:%d" % agent_port], "max_local": 1, "local_first": False}
    project.task_file(xml, seed_pch=True)
    code, seed_text, rows = run_xgconsole(xml, client, env, os.path.join(remote_root, "report"))
    R.check(code == 0, "the PCH is created and a first compile uses it", seed_text)
    if code != 0:
        return
    if debug_flag == "/Z7":
        R.check("sent SharedPCH.h.pch" in seed_text, "the agent received the PCH", seed_text)
    project.task_file(xml)
    code, text, rows = run_xgconsole(xml, client, env, os.path.join(remote_root, "report"))
    R.check(code == 0 and os.path.isfile(project.exe), "distributed build links", text)
    if code != 0:
        return
    remote = [r for r in rows if r["ran_on"] not in ("local", "not run")]
    wanted = Project.COUNT + 3
    if debug_flag == "/Z7":
        R.check("sent SharedPCH.h.pch" not in text, "later builds reuse the agent's PCH", text)
    R.check("local_never_used_xyz" in text and "C4189" in text, "warnings in the project's own code reach UBT", text)
    R.check(len(remote) == wanted, "all %d compiles ran on the agent (got %d)" % (wanted, len(remote)), json.dumps(rows, indent=1))
    output = subprocess.run([project.exe], stdout=subprocess.PIPE).stdout.decode()
    R.check(output == reference, "program output matches the local build (%r)" % output.strip(), reference)

    if ue5:
        R.check(os.path.getsize(os.path.join(project.out, "lib_0.cpp.sarif")) > 0, "the /experimental:log file comes back")
        marks = [l for l in text.splitlines() if l.startswith("@action_")]
        R.check(len(marks) == len(rows), "one @action_N line per action for UBT's progress parser", text)
    else:
        R.check("lib_0.cpp" in text and "Action" not in text.replace("DistriBuild", ""), "4.26 status text comes from OutputPrefix, not task names", text)

    for dep in glob.glob(os.path.join(project.out, "lib_0.cpp.*")):
        if not dep.endswith((".json", ".txt")):
            continue
        ours = dep_set(dep)
        names = set(os.path.basename(p) for p in ours)
        R.check({"lib_0.h", "shared.h", "extra.h"} <= names, "%s lists the headers UBT must track" % os.path.basename(dep), repr(sorted(names)))
        ref = set(os.path.basename(p) for p in reference_deps.get(os.path.basename(dep), set()))
        R.check(ref <= names, "dependency names cover the real compiler's list", repr(sorted(ref - names)))
        R.check(any("모듈" in p for p in ours), "non-ASCII include path survives in the dependency file", repr(sorted(ours)[:5]))


def test_failure(ue5, cl, link, cl_filter, env, agent_port, tmp):
    print("[compile error, %s]" % ("5.8" if ue5 else "4.26"))
    root = os.path.join(tmp, "broken" + ("58" if ue5 else "426"))
    project = Project(root, ue5, cl, link, cl_filter, broken=True)
    xml = os.path.join(root, "XGETasks.xml")
    project.task_file(xml)
    code, text, rows = run_xgconsole(xml, {"agents": ["127.0.0.1:%d" % agent_port], "max_local": 2, "local_first": False}, env,
                                     os.path.join(root, "report"))
    pch_row = [r for r in rows if r["task"].startswith("SharedPCH")]
    R.check(pch_row and pch_row[0]["ran_on"] == "local" and "remote-safe" in pch_row[0]["reason_local"], "PCH creation stays local")
    R.check(code != 0, "build fails with a nonzero exit code")
    R.check("broken.cpp" in text and "undeclared_symbol_xyz" in text, "the real error reaches UBT, naming the source file", text)
    R.check("C2065" in text, "the compiler's error code is in the output", text)
    # The first build ran broken.cpp locally while the PCH uploaded. Now the agent has it, so it fails there.
    project.pch_exists = True
    project.task_file(xml)
    run_xgconsole(xml, {"agents": ["127.0.0.1:%d" % agent_port], "max_local": 2, "local_first": False}, env, os.path.join(root, "report2"))
    with open(os.path.join(tmp, "agent.log"), errors="replace") as f:
        agent_log = f.read()
    R.check("undeclared_symbol_xyz" in agent_log, "the agent window shows why a remote compile failed", agent_log[-2000:])
    linked = [r for r in rows if r["task"] == "module.exe"]
    R.check(linked and linked[0]["ran_on"] == "not run", "link is skipped after a failed compile", json.dumps(linked))
    R.check(not os.path.isfile(project.exe), "no executable is produced")


# --------------------------------------------------------------------------- shader jobs

FAKE_SCW = r"""
#include <stdio.h>
#include <string.h>
#include <windows.h>
static int exists(const char* dir, const char* rel) {
    char path[1024]; snprintf(path, sizeof path, "%s/%s", dir, rel);
    return GetFileAttributesA(path) != INVALID_FILE_ATTRIBUTES;
}
/* argv: WorkingDir/ ParentPID ThreadId Input Output -xge_int ... (same indexes as the real worker) */
int main(int argc, char** argv) {
    char in[1024], out[1024];
    FILE* f;
    long n; char* job; char report[4096] = "";
    if (argc < 7 || strcmp(argv[6], "-xge_int") != 0) { fprintf(stderr, "argv[6] is not -xge_int\n"); return 3; }
    snprintf(in, sizeof in, "%s%s", argv[1], argv[4]);
    snprintf(out, sizeof out, "%s%s", argv[1], argv[5]);
    f = fopen(in, "rb"); if (!f) { fprintf(stderr, "cannot open %s\n", in); return 4; }
    fseek(f, 0, SEEK_END); n = ftell(f); fseek(f, 0, SEEK_SET);
    job = (char*)malloc(n); fread(job, 1, n, f); fclose(f);
    f = fopen(out, "wb"); if (!f) return 5;
    fwrite(job, 1, n, f);
    fprintf(f, "|ARGC=%d|", argc);
    fclose(f);
    return 0;
}
"""


def fstring(text):
    raw = text.encode("ascii") + b"\0"
    return struct.pack("<i", len(raw)) + raw


def fstring16(text):
    raw = text.encode("utf-16-le") + b"\0\0"
    return struct.pack("<i", -(len(raw) // 2)) + raw


def job_426(mappings, body=b"REST-OF-JOB"):
    """Head of a job file as FShaderCompileUtilities::DoWriteTasks writes it, FString by FString."""
    head = struct.pack("<i", 16)
    head += struct.pack("<i", 2) + fstring("PCD3D_SM5") + struct.pack("<I", 7) + fstring("PCD3D_ES31") + struct.pack("<I", 9)
    head += struct.pack("<i", len(mappings))
    for k, v in mappings:
        head += fstring(k) + v
    return head + body


def test_remap():
    print("[4.26 job file mappings]")
    job = job_426([("/Engine", fstring("C:/Client/Engine/Shaders")), ("/Plugin/한글".encode("ascii", "replace").decode(), fstring16("D:/프로젝트/Shaders"))])
    mapped = remap_shader_source_dirs(job, lambda v: "E:/agent/%d" % len(v))
    R.check(b"Client" not in mapped and "프로젝트".encode("utf-16-le") not in mapped, "client paths are gone, ANSI and UTF-16 strings both parsed")
    R.check(mapped.endswith(b"REST-OF-JOB") and mapped[:8] == job[:8], "head and body are untouched")
    R.check(mapped.count(b"E:/agent/") == 2, "every mapping is rewritten")
    try:
        remap_shader_source_dirs(job, lambda v: {}[v])
        R.check(False, "an unknown directory is refused")
    except KeyError:
        R.check(True, "an unknown directory is refused")


def reserve(port, extra=None):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    send_frame(s, dict({"type": "reserve", "token": "", "kind": "shader"}, **(extra or {})))
    return s, recv_frame(s)[0]


def test_shaders(cl, env, tmp):
    print("[shader jobs on the agent]")
    engine = os.path.join(tmp, "AgentEngine")
    bin_dir = os.path.join(engine, "Engine", "Binaries", "Win64")
    os.makedirs(bin_dir)
    os.makedirs(os.path.join(engine, "Engine", "Build"))
    with open(os.path.join(engine, "Engine", "Build", "Build.version"), "w") as f:
        json.dump({"MajorVersion": 4, "MinorVersion": 26, "PatchVersion": 2}, f)
    src = os.path.join(tmp, "fake_scw.c")
    with open(src, "w") as f:
        f.write(FAKE_SCW)
    subprocess.run([cl, "/nologo", "/O1", "/Fe" + os.path.join(bin_dir, "ShaderCompileWorker.exe"), "/Fo" + src + ".obj", src],
                   check=True, env=env, stdout=subprocess.DEVNULL, cwd=tmp)

    port, work = free_port(), os.path.join(tmp, "shader_agent")
    cfg = os.path.join(tmp, "shader_agent.json")
    with open(cfg, "w") as f:
        json.dump({"port": port, "discovery_port": free_port(), "auto_detect": False, "engines": [engine], "work_dir": work, "slots": 4}, f)
    agent = subprocess.Popen([sys.executable, os.path.join(TOOLS, "agent.py"), "--config", cfg], stdout=subprocess.DEVNULL, env=env)
    try:
        wait_for_port(port)
        info = request("127.0.0.1", port, {"type": "info", "token": ""})[0]
        R.check(info.get("engines") == ["4.26.2"], "agent offers the engine key from Build.version", repr(info))

        bundle = "A" * 40
        job = job_426([("/Engine", fstring("C:/Client/Engine/Shaders")), ("/Plugin/Mine", fstring("D:/Game/Plugins/Mine/Shaders"))])
        dirs = {"/Engine": 0, "/Plugin/Mine": 1}
        files = [{"dir": 0, "path": "Public/Platform.ush", "blob": "src000000"}, {"dir": 1, "path": "My/Deep/Effect.usf", "blob": "src000001"}]
        hdr = {"type": "run", "kind": "shader", "engine": "4.26.2", "args": "-Multiprocess -foo", "input": "job.in", "output": "job.out",
               "source_bundle": bundle, "source_dirs": dirs, "caption": "t"}

        s, reply = reserve(port, {"source_bundle": bundle})
        R.check(reply.get("ok") and reply.get("need_bundle") is True, "first job is asked to upload the sources", repr(reply))
        s2, reply2 = reserve(port, {"source_bundle": bundle})
        R.check(reply2.get("reason") == "busy", "a second client does not upload the same bundle at the same time", repr(reply2))
        s2.close()
        send_frame(s, dict(hdr, source_files=files), [("job.in", job), ("src000000", b"// engine shader"), ("src000001", b"// plugin shader")])
        resp, out = recv_frame(s)
        s.close()
        R.check(resp.get("ok") and resp.get("exit_code") == 0, "job runs after the upload", repr(resp))
        output = dict(out).get("job.out", b"")
        root = os.path.join(work, "source_bundles", bundle.lower())
        R.check(os.path.isfile(os.path.join(root, "0", "Public", "Platform.ush")) and os.path.isfile(os.path.join(root, "1", "My", "Deep", "Effect.usf")),
                "bundle is unpacked per directory")
        R.check(b"C:/Client" not in output and root.replace("\\", "/").encode() in output, "worker sees mappings that point at the bundle", repr(output[:300]))
        R.check(b"|ARGC=9|" in output, "worker gets positional arguments, -xge_int and the extras", repr(output[-40:]))

        s, reply = reserve(port, {"source_bundle": bundle})
        R.check(reply.get("ok") and reply.get("need_bundle") is False, "a later job does not upload again", repr(reply))
        send_frame(s, hdr, [("job.in", job)])
        resp, out = recv_frame(s)
        s.close()
        R.check(resp.get("ok") and resp.get("exit_code") == 0, "a later job runs from the cache", repr(resp))

        missing = "B" * 40
        s, _ = reserve(port, {"source_bundle": missing})
        send_frame(s, dict(hdr, source_bundle=missing), [("job.in", job)])
        resp, _ = recv_frame(s)
        s.close()
        R.check(not resp.get("ok"), "a job without its bundle is refused, so the client runs it locally", repr(resp))

        s, reply = reserve(port, {"source_bundle": "C" * 40})
        send_frame(s, dict(hdr, source_bundle="C" * 40, source_files=[{"dir": 0, "path": "../../evil.txt", "blob": "src000000"}]),
                   [("job.in", job), ("src000000", b"x")])
        resp, _ = recv_frame(s)
        s.close()
        R.check(not resp.get("ok") and not os.path.exists(os.path.join(work, "evil.txt")), "a path escaping the bundle is refused", repr(resp))

        # 5.8: no bundle at all; the job file is passed through untouched.
        job58 = b"self-contained job"
        s, reply = reserve(port)
        send_frame(s, {"type": "run", "kind": "shader", "engine": "4.26.2", "args": "-Multiprocess", "input": "job.in", "output": "job.out"}, [("job.in", job58)])
        resp, out = recv_frame(s)
        s.close()
        R.check(resp.get("exit_code") == 0 and dict(out).get("job.out", b"").startswith(job58), "jobs without a bundle pass through untouched", repr(resp))
    finally:
        agent.kill()
        agent.wait()


def wait_for_port(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("agent did not start")


def test_real_engine_file(args, cl, env, agent_port, tmp):
    """Compiles real engine sources through the agent with UBT's flags and compares with a direct compile."""
    print("[real engine source, UE %s]" % args.ue_version)
    ue5 = args.ue_version == "58"
    src_root = args.ue_source
    root = os.path.join(tmp, "real" + args.ue_version)
    out = os.path.join(root, "out")
    os.makedirs(out)
    inc = []
    for group in ("Runtime",):
        base = os.path.join(src_root, group)
        inc.append(base)
        for d in ("Core", "TraceLog", "BuildSettings", "AutoRTFM", "CoreUObject"):
            for s in ("Public", "Internal", "Private", os.path.join("Public", "Internationalization")):
                p = os.path.join(base, d, s)
                if os.path.isdir(p):
                    inc.append(p)
    stubs = os.path.join(root, "generated")
    os.makedirs(stubs)
    inc.append(stubs)
    defines = DEFINES + ["%s=" % m for m in sorted(scan_headers(src_root, stubs))] + ["UE_IS_ENGINE_MODULE=1"]
    flags = ["/nologo", "/c", "/EHsc", "/Zc:inline", "/Z7", "/Od", "/GR-", "/bigobj", "/fp:fast", "/utf-8", "/W3", "/wd4005",
             "/Zm1000", "/std:c++20" if ue5 else "/std:c++17"]
    if ue5:
        # As VCToolChain.AddSystemIncludePath does: compiler and SDK headers are external too.
        flags += ["/Zc:preprocessor", "/Zc:__cplusplus", "/permissive-", "/external:W0", '/external:I "%s"' % os.path.join(src_root, "ThirdParty")]
        flags += ['/external:I "%s"' % d for d in env["INCLUDE"].split(";")]
    flags += ["/D%s" % d for d in defines] + ['/DUE_MODULE_NAME=\\"Core\\"', '/DUE_PLUGIN_NAME=\\"\\"', '/DUE_PROJECT_NAME=\\"x\\"', '/DUE_TARGET_NAME=\\"x\\"']
    flags += ['/I "%s"' % d for d in inc]
    names = args.ue_files.split(",")
    tf = TaskFile(ue5)
    objs = {}
    for name in names:
        source = os.path.join(src_root, "Runtime", "Core", "Private", name)
        obj = os.path.join(out, os.path.basename(name) + ".obj")
        rsp = obj + ".rsp"
        objs[name] = (source, obj, rsp)
        with open(rsp, "w", encoding="utf-8") as f:
            f.write("\n".join(flags + ['"%s"' % source, '/Fo"%s"' % obj]))
        tf.add(os.path.basename(name), cl, '@"%s"' % rsp, root, True, produced=[os.path.basename(obj)])
    xml = os.path.join(root, "XGETasks.xml")
    tf.write(xml)

    # Direct compile first: if the harness' flags cannot build the file, the comparison means nothing.
    direct = {}
    for name, (source, obj, rsp) in objs.items():
        t0 = time.time()
        p = subprocess.run([cl, "@" + rsp, "/Fo" + obj + ".direct.obj"], cwd=root, env=dict(env, VSLANG="1033"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        direct[name] = time.time() - t0
        ok = p.returncode == 0
        print("  direct %s: exit %d, %.1fs" % (name, p.returncode, direct[name]))
        if not ok:
            print("    " + "\n    ".join(p.stdout.decode("utf-8", "replace").splitlines()[:12]))
            R.check(False, "direct compile of %s" % name)
            return
    run_env = env if not ue5 else dict(env, INCLUDE="")  # like UBT 5.8, which passes every path on the command line
    t0 = time.time()
    code, text, rows = run_xgconsole(xml, {"agents": ["127.0.0.1:%d" % agent_port], "max_local": 1, "local_first": False, "verbose": True},
                                     run_env, os.path.join(root, "report"))
    R.check(code == 0, "distributed compile of real engine files succeeds", text)
    for r in rows:
        R.check(r["ran_on"] not in ("local", "not run"), "%s ran on the agent (%ss)" % (r["task"], r["seconds"]), json.dumps(r))
    for name, (source, obj, rsp) in objs.items():
        if os.path.isfile(obj):
            size_remote, size_local = os.path.getsize(obj), os.path.getsize(obj + ".direct.obj")
            print("  %s: object %d KB remote, %d KB direct" % (name, size_remote // 1024, size_local // 1024))
            dumpbin = os.path.join(os.path.dirname(cl), "dumpbin.exe")
            syms = []
            for path in (obj, obj + ".direct.obj"):
                p = subprocess.run([dumpbin, "/nologo", "/symbols", path], stdout=subprocess.PIPE, env=env)
                syms.append(set(l.split("|")[-1].strip() for l in p.stdout.decode("utf-8", "replace").splitlines() if "External" in l and "SECT" in l))
            R.check(syms[0] == syms[1] and len(syms[0]) > 0, "%s defines the same %d external symbols as a direct compile" % (name, len(syms[1])),
                    repr(sorted(syms[0] ^ syms[1])[:10]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cl-filter-source")
    ap.add_argument("--ue-source")
    ap.add_argument("--ue-version", choices=["426", "58"])
    ap.add_argument("--ue-files", default="Misc/Paths.cpp")
    ap.add_argument("--only")
    args = ap.parse_args()

    cl, link, env = find_toolchain()
    tmp = tempfile.mkdtemp(prefix="dbe2e_")
    print("work dir:", tmp, "\ncompiler:", cl)

    cl_filter = None
    if args.cl_filter_source:
        cl_filter = os.path.join(tmp, "cl-filter.exe")
        p = subprocess.run([cl, "/nologo", "/EHsc", "/O1", "/DUNICODE", "/D_UNICODE", args.cl_filter_source, "/Fe" + cl_filter, "/Fo" + cl_filter + ".obj",
                            "/link", "version.lib", "user32.lib"], env=dict(env, VSLANG="1033"), cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode:
            print(p.stdout.decode("utf-8", "replace"))
            return 1

    port = free_port()
    agent_cfg = os.path.join(tmp, "agent.json")
    with open(agent_cfg, "w") as f:
        json.dump({"name": "e2e-agent", "port": port, "discovery_port": free_port(), "auto_detect": False, "compilers": [cl],
                   "work_dir": os.path.join(tmp, "agent_work"), "slots": os.cpu_count()}, f)
    # The agent gets none of the build environment: no INCLUDE, no LIB, no compiler on PATH.
    agent_env = {k: v for k, v in os.environ.items() if k not in ("INCLUDE", "LIB", "LIBPATH", "EXTERNAL_INCLUDE")}
    agent = subprocess.Popen([sys.executable, os.path.join(TOOLS, "agent.py"), "--config", agent_cfg], env=agent_env,
                             stdout=open(os.path.join(tmp, "agent.log"), "w"), stderr=subprocess.STDOUT)
    try:
        wait_for_port(port)
        info = request("127.0.0.1", port, {"type": "info", "token": ""})[0]
        print("agent offers:", info.get("compilers"), "protocol", PROTOCOL_VERSION)
        R.check(len(info.get("compilers", [])) == 1, "agent identifies the compiler whatever language it speaks", repr(info))
        if not args.only:
            test_remap()
            test_shaders(cl, env, tmp)
            test_build("5.8", True, cl, link, None, env, port, tmp, "/Z7")
            test_build("5.8", True, cl, link, None, env, port, tmp, "/Zi")
            test_failure(True, cl, link, None, env, port, tmp)
            if cl_filter:
                test_build("4.26", False, cl, link, cl_filter, env, port, tmp, "/Z7")
                test_failure(False, cl, link, cl_filter, env, port, tmp)
            else:
                print("[4.26 skipped: no --cl-filter-source]")
        if args.ue_source:
            test_real_engine_file(args, cl, env, port, tmp)
    finally:
        agent.kill()
        agent.wait()

    print()
    if R.failed:
        print("%d FAILED:" % len(R.failed))
        for f in R.failed:
            print("  " + f)
        print("work dir kept:", tmp)
        return 1
    print("all passed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
