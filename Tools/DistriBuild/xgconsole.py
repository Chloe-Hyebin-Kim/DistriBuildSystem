#!/usr/bin/env python3
"""DistriBuild xgConsole replacement for UnrealBuildTool.

UBT's XGE executor writes every action of a build into an XGE task XML and runs
`xgConsole.exe <file.xml> /Rebuild /NoLogo ...`. Building this script as xgConsole.exe
(see build_xgconsole.bat), putting it on PATH and installing the service UBT checks for
(see install_client.bat) makes UBT hand the whole build graph to DistriBuild, which then:

  * runs actions in dependency order using every local core,
  * offloads compile actions marked AllowRemote to agents once local cores are busy:
    the source is preprocessed locally (so agents need no source tree, SDK or engine
    headers), compiled remotely by the identical compiler, and the .obj copied back,
  * reuses precompiled headers: an MSVC .pch works anywhere with the same compiler build, so
    agents cache a copy and each job only sends the text that follows the PCH. Without this a
    UE source file means parsing its whole 25 MB of headers again,
  * re-runs anything that failed remotely on the local machine, so results never differ
    from a plain local build.

Client config (first found): $DISTRIBUILD_CONFIG, ~/.distribuild/client.json,
distribuild_client.json next to the executable.
{
  "agents": ["10.0.0.21:31890", "build-pc-07"],
  "discovery": true,
  "discovery_port": 31891,
  "token": "",
  "max_local": 0,              # 0 = CPU count
  "max_preprocess": 0,         # concurrent local preprocess steps for remote jobs; 0 = half the CPU count
  "local_first": true,         # fill local cores before going remote
  "share_pch": true,           # upload .pch files to agents; false = send the PCH's headers as text instead
  "retry_remote_failures_locally": true,
  "connect_timeout": 3,
  "job_timeout": 600,
  "verbose": false,            # print why each eligible task ran locally
  "report_dir": ""             # default ~/.distribuild: last_build.csv, last_tasks.xml, analysis.csv
}

`xgConsole --doctor` checks that UBT will actually pick DistriBuild and that agents can take its jobs.
`xgConsole --analyze <TaskFile.xml>` lists what can and cannot be distributed without building.
UBT's task file from the last build is kept as ~/.distribuild/last_tasks.xml for that purpose.
$DISTRIBUILD_AGENTS ("host:port,host") overrides "agents"; $DISTRIBUILD_DISABLE=1 forces local only.
"""

import ctypes
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from common import (DISCOVERY_PORT, IS_WINDOWS, base_name, compiler_family, compiler_id, decode_output,
                    discover_agents, expand_response_files, msvc_include_prefix, parse_agent_address, recv_frame,
                    record_agent_contacts, send_file_stream, send_frame, split_cmdline)

CPP_EXT = (".cpp", ".cc", ".cxx", ".c++", ".cp")
C_EXT = (".c",)
OBJC_EXT = {".m": "objective-c-cpp-output", ".mm": "objective-c++-cpp-output"}
PROGRESS_MARKUP = re.compile(r"^@action(_\d+ ?)?")
PCH_BOUNDARY = b"#pragma dbld_pch_boundary"


# --------------------------------------------------------------------------- config

def load_config():
    cfg = {"agents": [], "discovery": True, "discovery_port": DISCOVERY_PORT, "token": "", "max_local": 0,
           "max_preprocess": 0, "local_first": True, "share_pch": True, "retry_remote_failures_locally": True, "connect_timeout": 3,
           "job_timeout": 600, "verbose": False}
    exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    for path in (os.environ.get("DISTRIBUILD_CONFIG"),
                 os.path.join(os.path.expanduser("~"), ".distribuild", "client.json"),
                 os.path.join(exe_dir, "distribuild_client.json")):
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
            break
    if os.environ.get("DISTRIBUILD_AGENTS"):
        cfg["agents"] = [a for a in os.environ["DISTRIBUILD_AGENTS"].split(",") if a.strip()]
    if os.environ.get("DISTRIBUILD_DISABLE") == "1":
        cfg["agents"], cfg["discovery"] = [], False
    if not cfg["max_local"]:
        cfg["max_local"] = os.cpu_count() or 4
    if not cfg["max_preprocess"]:
        cfg["max_preprocess"] = max(2, (os.cpu_count() or 4) // 2)
    return cfg


# --------------------------------------------------------------------------- agent pool

class RemoteAgent:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.name = host
        self.slots = 0
        self.inflight = 0
        self.compilers = set()
        self.skip_until = 0.0
        self.done = 0
        self.failed = 0


class AgentPool:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.agents = {}
        # Pinned agents are asked all at once, while discovery listens, so a dead address costs
        # one connect_timeout per build instead of one each.
        self.answered = {}
        threads = []
        for text in cfg["agents"]:
            addr = parse_agent_address(text)
            if addr:
                self.answered[text] = False
                threads.append(threading.Thread(target=self._query, args=addr + (text,), daemon=True))
        for t in threads:
            t.start()
        if cfg["discovery"]:
            for host, info in discover_agents(int(cfg["discovery_port"])):
                self._update(host, int(info.get("port")), info)
        deadline = time.time() + float(cfg["connect_timeout"]) + 1
        for t in threads:
            t.join(max(0.0, deadline - time.time()))
        with self.lock:
            answered = dict(self.answered)
        record_agent_contacts(answered)

    def _query(self, host, port, text=None):
        try:
            with socket.create_connection((host, port), timeout=self.cfg["connect_timeout"]) as s:
                send_frame(s, {"type": "info", "token": self.cfg["token"]})
                info, _ = recv_frame(s)
            with self.lock:
                self.answered[text] = True
            if info.get("type") == "info":
                self._update(host, port, info)
            else:
                print("DistriBuild: agent %s:%d refused (%s)" % (host, port, info.get("reason")))
        except (OSError, ValueError) as e:
            print("DistriBuild: agent %s:%d unreachable (%s)" % (host, port, e))

    def _update(self, host, port, info):
        try:
            host = socket.gethostbyname(host)
        except OSError:
            pass
        # Keyed by agent name: the same machine can answer from several addresses.
        key = (info.get("name") or host, port)
        with self.lock:
            a = self.agents.get(key) or RemoteAgent(host, port)
            a.name = info.get("name", host)
            a.slots = int(info.get("slots", 0))
            a.compilers = set(info.get("compilers", []))
            self.agents[key] = a

    def capacity(self, cid=None):
        with self.lock:
            return sum(a.slots for a in self.agents.values() if cid is None or cid in a.compilers)

    def describe(self):
        with self.lock:
            return ", ".join("%s(%d)" % (a.name, a.slots) for a in self.agents.values())

    def acquire(self, cid, pch_key=None):
        """Returns (agent, connected socket holding a reserved slot, reply), or None and why not."""
        now = time.time()
        reason = "all agents with this compiler were busy"
        with self.lock:
            cands = [a for a in self.agents.values()
                     if cid in a.compilers and a.inflight < a.slots and a.skip_until <= now]
            cands.sort(key=lambda a: a.inflight / float(a.slots))
            for a in cands:
                a.inflight += 1
        for i, a in enumerate(cands):
            sock = None
            try:
                sock = socket.create_connection((a.host, a.port), timeout=self.cfg["connect_timeout"])
                send_frame(sock, {"type": "reserve", "token": self.cfg["token"], "kind": "compile", "pch": pch_key})
                reply, _ = recv_frame(sock)
                if reply.get("ok"):
                    with self.lock:
                        for other in cands[i + 1:]:
                            other.inflight -= 1
                    return (a, sock, reply), None
                # "uploading": another job is sending this PCH there. The agent is fine for other PCHs.
                backoff = {"busy": 1.0, "uploading": 0.0}.get(reply.get("reason"), 30.0)
                if reply.get("reason") == "uploading":
                    reason = "agents were still receiving this precompiled header"
            except (OSError, ValueError):
                backoff = 15.0
            if sock:
                sock.close()
            with self.lock:
                a.inflight -= 1
                a.skip_until = time.time() + backoff
        return None, reason

    def release(self, agent, sock, ok):
        try:
            sock.close()
        except OSError:
            pass
        with self.lock:
            agent.inflight -= 1
            if ok:
                agent.done += 1
            else:
                agent.failed += 1
                if agent.failed > 3 * (agent.done + 1):
                    agent.skip_until = time.time() + 60


# --------------------------------------------------------------------------- compile analysis

class Unsupported(Exception):
    """A compile that must run locally; the message says why (shown in reports)."""


class CompileJob:
    """Split of one compiler invocation into a local preprocess step and a remote compile step."""
    family = None
    source = None
    output = None
    depfile = None
    show_includes = False
    sarif = None            # /experimental:log: UE 5.8 asks for this on every compile
    pre_args = None
    remote_args = None
    remote_output = None
    external_dirs = ()      # headers below these get /external:W0 treatment (see mark_external_regions)
    warning_flags = ()      # /we4100, /w14668, ...
    pch_header = None       # set when the compile uses a .pch an agent can share
    pch_file = None
    defines = ()


def _msvc_value(args, i, prefix_len):
    """Value of a flag that may be attached (/Fofoo, /Fo:foo) or in the next token (/Fo foo)."""
    v = args[i][prefix_len:]
    if v.startswith(":"):
        v = v[1:]
    if v:
        return v, i + 1
    if i + 1 < len(args):
        return args[i + 1], i + 2
    return None, i + 1


def analyze_msvc(args):
    job = CompileJob()
    job.family = "msvc"
    pre, rem = [], []
    external_dirs, warning_flags, defines, forced = [], [], [], []
    external_w0 = uses_pdb = False
    yu = fp = None
    compile_only = False
    force_c = force_cpp = False
    i = 0
    while i < len(args):
        a = args[i]
        if not a or a[0] not in "/-":
            if a.lower().endswith(CPP_EXT + C_EXT) and job.source is None:
                job.source = a
                i += 1
                continue
            raise Unsupported("unexpected argument %r" % a)
        name = a[1:]
        low = name.lower()
        if low.startswith("fo"):
            job.output, i = _msvc_value(args, i, 3)
            continue
        if low == "sourcedependencies":
            job.depfile = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if low == "experimental:log":
            job.sarif = args[i + 1] if i + 1 < len(args) else None
            rem += ["/experimental:log", "{WORK}/out.sarif"]
            i += 2
            continue
        if low.startswith("sourcedependencies") or low.startswith(("headerunit", "reference", "ifc", "interface",
                                                                     "exporttheader", "translateinclude")):
            raise Unsupported("C++20 modules / header units (%s)" % a)
        if name.startswith("Yc"):
            raise Unsupported("creates a precompiled header (/Yc)")
        if name.startswith(("clr", "ZW")):
            raise Unsupported("managed / WinRT compile (%s)" % a)
        if low.startswith("analyze") and not low.startswith("analyze-"):
            raise Unsupported("static analysis (/analyze)")
        if name in ("P", "E", "EP") or name.startswith(("Fa", "FA", "Fe", "Fi", "Fm", "FR", "Fr", "Fx", "doc")):
            raise Unsupported("extra output file (%s)" % a[:4])
        if name.startswith("Yu"):
            yu = name[2:]
            i += 1
            continue
        if name.startswith("Fp"):
            fp = name[2:]
            i += 1
            continue
        if name.startswith(("Fd", "MP")) or name == "FS":  # PDB / parallelism
            i += 1
            continue
        if low == "showincludes":
            job.show_includes = True
            i += 1
            continue
        if low == "c":
            compile_only = True
            i += 1
            continue
        if low in ("zi",):  # /Zi and /ZI need a PDB server; embed debug info instead
            rem.append("/Z7")
            uses_pdb = True  # a .pch built with /Zi only works with its .pdb
            i += 1
            continue
        if low in ("tp", "tc"):
            force_cpp, force_c = low == "tp", low == "tc"
            pre.append(a)
            i += 1
            continue
        if low.startswith(("tp", "tc")):
            job.source, i = _msvc_value(args, i, 3)
            force_cpp, force_c = low.startswith("tp"), low.startswith("tc")
            pre.append(a[:3])
            continue
        # A preprocessed file has no include search left to mark headers as external, so the local
        # preprocess step reproduces /external:W0 inside the file instead.
        if low == "external:i":
            external_dirs.extend(args[i + 1:i + 2])
            pre.extend(args[i:i + 2])
            i += 2
            continue
        if low.startswith("external:"):
            if low.startswith("external:env:"):
                external_dirs.extend(filter(None, os.environ.get(name.split(":", 2)[2], "").split(";")))
            elif low.startswith("external:i"):
                external_dirs.append(name[len("external:I"):])
            elif low == "external:w0":
                external_w0 = True
            pre.append(a)
            i += 1
            continue
        if re.match(r"(we|w[1-4]|wo)\d{4}$", low):
            warning_flags.append(low[-4:])
        # Preprocessor-only flags: local preprocess keeps them, remote compile drops them.
        if name in ("D", "U", "I", "FI", "imsvc"):
            pre.extend(args[i:i + 2])
            if name in ("D", "U"):
                defines.append(a + args[i + 1])
            elif name == "FI":
                forced.append((len(pre) - 1, args[i + 1]))
            i += 2
            continue
        if (name.startswith(("D", "U", "I", "FI", "imsvc")) or name == "u" or name == "X"
                or low.startswith("d1trimfile")):
            pre.append(a)
            if name.startswith(("D", "U")):
                defines.append(a)
            elif name.startswith("FI"):
                forced.append((len(pre) - 1, name[2:]))
            i += 1
            continue
        pre.append(a)
        rem.append(a)
        i += 1

    if not compile_only:
        raise Unsupported("not a compile-only (/c) invocation")
    if not (job.source and job.output):
        raise Unsupported("no single source file / output object")
    if job.output.endswith(("\\", "/")):
        job.output = os.path.join(job.output, os.path.splitext(os.path.basename(job.source))[0] + ".obj")
    is_c = force_c or (not force_cpp and job.source.lower().endswith(C_EXT))
    job.pre_args = pre + [job.source]
    job.remote_output = "out.obj"
    job.remote_args = rem + ["/c", ("/Tc" if is_c else "/Tp") + "{WORK}/src.i", "/Fo{WORK}/out.obj"]
    if external_w0:
        job.external_dirs, job.warning_flags = external_dirs, warning_flags
    if yu and fp and not uses_pdb:
        # The boundary marker goes right after the header the PCH ends with, so the text that follows
        # it in the preprocessed file is exactly what a compile with that PCH still has to parse.
        for index, (position, path) in enumerate(forced):
            if os.path.normcase(path) == os.path.normcase(yu):
                job.pch_header, job.pch_file, job.defines = yu, fp, defines
                job.pre_args = pre[:position + 1] + ["/FI{BOUNDARY}"] + pre[position + 1:] + [job.source]
                break
    return job


GNU_PRE_ONLY_WITH_VALUE = {"-I", "-D", "-U", "-include", "-isystem", "-iquote", "-idirafter", "-imacros",
                           "-isysroot", "--sysroot", "-iprefix", "-iwithprefix", "-MF", "-MT", "-MQ"}
GNU_KEEP_WITH_VALUE = {"-arch", "-target", "--target", "-Xassembler", "-mllvm", "-Xarch_x86_64"}


def analyze_gnu(args):
    job = CompileJob()
    job.family = "gnu"
    pre, rem = [], []
    compile_only = has_md = has_mt = False
    lang = None
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-") or a == "-":
            ext = os.path.splitext(a)[1].lower()
            if (ext in CPP_EXT + C_EXT or ext in OBJC_EXT) and job.source is None:
                job.source = a
                i += 1
                continue
            raise Unsupported("unexpected argument %r" % a)
        if a == "-o":
            job.output = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a == "-c":
            compile_only = True
            i += 1
            continue
        if a in ("-E", "-S", "-M", "-MM", "-MJ", "-save-temps") or a.startswith(
                ("-save-temps=", "-ftime-trace", "--serialize-diagnostics", "-fmodules", "-fprofile-use=")):
            raise Unsupported("unsupported flag %s" % a)
        if a == "-x":
            lang = args[i + 1] if i + 1 < len(args) else None
            pre.extend(args[i:i + 2])
            i += 2
            continue
        if a == "-Xclang" and i + 1 < len(args):
            if args[i + 1] in ("-include-pch", "-include", "-pch-through-header") and i + 3 < len(args):
                if args[i + 1] == "-include":
                    pre.extend(args[i:i + 4])
                i += 4
                continue
            pre.extend(args[i:i + 2])
            rem.extend(args[i:i + 2])
            i += 2
            continue
        if a == "-include-pch":
            i += 2
            continue
        if a in GNU_PRE_ONLY_WITH_VALUE:
            has_mt = has_mt or a in ("-MT", "-MQ")
            if a == "-MF":
                job.depfile = args[i + 1] if i + 1 < len(args) else None
            pre.extend(args[i:i + 2])
            i += 2
            continue
        if a in GNU_KEEP_WITH_VALUE:
            pre.extend(args[i:i + 2])
            rem.extend(args[i:i + 2])
            i += 2
            continue
        if a in ("-MD", "-MMD", "-MP"):
            has_md = True
            pre.append(a)
            i += 1
            continue
        if a.startswith(("-I", "-D", "-U", "-isystem", "-iquote", "-idirafter", "-include", "--sysroot=",
                         "-isysroot", "-MF", "-MT", "-MQ", "-fmacro-prefix-map=")):
            has_mt = has_mt or a.startswith(("-MT", "-MQ"))
            pre.append(a)
            i += 1
            continue
        if a.startswith(("-fdebug-prefix-map=", "-ffile-prefix-map=", "-fcoverage-prefix-map=")):
            pre.append(a)
            i += 1
            continue
        pre.append(a)
        rem.append(a)
        i += 1

    if not compile_only:
        raise Unsupported("not a compile-only (-c) invocation")
    if not (job.source and job.output):
        raise Unsupported("no single source file / output object")
    ext = os.path.splitext(job.source)[1].lower()
    if lang in ("c", "c-header"):
        out_lang = "cpp-output"
    elif lang in ("c++", "c++-header"):
        out_lang = "c++-cpp-output"
    elif lang:
        raise Unsupported("language %s" % lang)
    elif ext in OBJC_EXT:
        out_lang = OBJC_EXT[ext]
    else:
        out_lang = "cpp-output" if ext in C_EXT else "c++-cpp-output"
    job.pre_args = pre + ["-E", job.source]
    if has_md and not has_mt:
        job.pre_args += ["-MT", job.output]
    job.remote_output = "out.o"
    job.remote_args = rem + ["-x", out_lang, "-c", "{WORK}/src.i", "-o", "{WORK}/out.o"]
    return job


WARNING_PRAGMA = re.compile(rb"(?:#\s*pragma|__pragma\s*\()\s*warning\s*\(([^)]*)\)")
LINE_DIRECTIVE = re.compile(rb'^[ \t]*#line[ \t]+\d+[ \t]+"([^"\r\n]*)"', re.M)


def mark_external_regions(data, external_dirs, warning_flags, cwd, start=0):
    """Makes /external:W0 work on preprocessed source.

    The compiler decides what is external while it searches for an #include, which never happens for
    a preprocessed file, so warnings in SDK and third-party headers would suddenly appear, many of
    them as errors. #line directives still say which header each part came from, so those parts get
    wrapped in a warning region. Warnings elevated by name ignore the warning level, and have to be
    disabled by number on top of that. Returns the file from offset `start` on; the part before it is
    only read for those elevated warnings, because a .pch keeps its warning state.
    """
    roots = []
    for d in external_dirs:
        path = os.path.normcase(os.path.normpath(os.path.join(cwd, d)))
        roots.append(path if path.endswith(os.sep) else path + os.sep)
    if not roots:
        return data[start:]

    elevated = set(warning_flags)
    for pragma in WARNING_PRAGMA.findall(data):
        for spec in pragma.split(b";"):
            kind, _, numbers = spec.partition(b":")
            if kind.strip() in (b"error", b"default", b"once", b"1", b"2", b"3", b"4"):
                elevated.update(n.decode() for n in numbers.split())
    enter = b"#pragma warning(push, 0)\n"
    if elevated:
        enter += b"#pragma warning(disable: " + " ".join(sorted(elevated)).encode() + b")\n"
    leave = b"#pragma warning(pop)\n"

    known = {}
    pieces, inside = [], False
    for m in LINE_DIRECTIVE.finditer(data, start):
        raw = m.group(1)
        external = known.get(raw)
        if external is None:
            name = decode_output(raw).replace("\\\\", "\\")
            path = os.path.normcase(os.path.normpath(os.path.join(cwd, name)))
            external = known[raw] = path.startswith(tuple(roots))
        if external != inside:
            pieces += [data[start:m.start()], enter if external else leave]
            start = m.start()
            inside = external
    pieces.append(data[start:])
    if inside:
        pieces.append(b"\n" + leave)
    return b"".join(pieces)


def write_msvc_depfile(path, source, includes):
    data = {"Version": "1.2", "Data": {
        "Source": os.path.abspath(source).lower(), "ProvidedModule": "",
        "Includes": [os.path.normpath(p).lower() for p in includes],
        "ImportedModules": [], "ImportedHeaderUnits": []}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def write_dependency_list(path, includes):
    """cl-filter's -dependencies file (UE 4.x): one included file per line."""
    seen, lines = set(), []
    for inc in includes:
        key = os.path.normcase(os.path.normpath(inc))
        if key not in seen:
            seen.add(key)
            lines.append(inc)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


# --------------------------------------------------------------------------- build graph

class Task:
    def __init__(self, el, tool):
        self.name = el.get("Name")
        # UBT 4.26 leaves Caption out when the status text is already in OutputPrefix.
        self.output_prefix = tool.get("OutputPrefix", "")
        self.caption = el.get("Caption") or PROGRESS_MARKUP.sub("", self.output_prefix).strip() or self.name
        self.auto_recover = [m.strip() for m in tool.get("AutoRecover", "").split(",") if m.strip()]
        self.cwd = el.get("WorkingDir") or os.getcwd()
        self.deps = [d.strip() for d in re.split(r"[;,]", el.get("DependsOn", "")) if d.strip()]
        self.tool_path = tool.get("Path", "")
        self.params = tool.get("Params", "")
        self.allow_remote = tool.get("AllowRemote", "").lower() == "true"
        self.dependents = []
        self.remaining = 0
        self.where = ""
        self.reason = ""
        self.eligible = False
        self.seconds = 0.0
        self.code = None


def load_tasks(xml_path):
    root = ET.parse(xml_path).getroot()
    # Only in files from `UnrealBuildTool -XGEExport`; a live UBT passes its environment by inheritance.
    for variable in root.iter("Variable"):
        os.environ[variable.get("Name")] = variable.get("Value", "")
    tools = {}
    for tool in root.iter("Tool"):
        tools[tool.get("Name")] = dict(tool.attrib)
    tasks = {}
    for el in root.iter("Task"):
        t = Task(el, tools.get(el.get("Tool"), {}))
        tasks[t.name] = t
    for t in tasks.values():
        t.deps = [d for d in t.deps if d in tasks]
        t.remaining = len(t.deps)
        for d in t.deps:
            tasks[d].dependents.append(t)
    return list(tasks.values())


class Builder:
    def __init__(self, tasks, cfg, pool, stop_on_errors):
        self.tasks = tasks
        self.cfg = cfg
        self.pool = pool
        self.stop_on_errors = stop_on_errors
        self.local_sem = threading.Semaphore(cfg["max_local"])
        self.pre_sem = threading.Semaphore(cfg["max_preprocess"])
        self.print_lock = threading.Lock()
        self.state_lock = threading.Condition()
        self.finished = 0
        self.failed = []
        self.aborted = False
        self.pending_threads = 0
        self.ids = {}

    # ---------------------------------------------------------------- execution

    def emit(self, task, where, code, text):
        with self.print_lock:
            self.finished += 1
            # UBT counts progress from the task's OutputPrefix when it asked for "@action" markup.
            if task.output_prefix.startswith("@action"):
                head = task.output_prefix
            else:
                head = "[%d/%d] %s" % (self.finished, len(self.tasks), task.caption)
            sys.stdout.write(head + ((" (%s)" % where) if where else "") + "\n")
            if text.strip():
                sys.stdout.write(text if text.endswith("\n") else text + "\n")
            if code != 0:
                sys.stdout.write("DistriBuild: '%s' failed with exit code %d\n" % (task.caption, code))
            sys.stdout.flush()

    def run_local(self, task):
        code, text = self.run_local_once(task)
        # The Tool's AutoRecover list names failures from memory pressure (C1076, C3859, ...), not bad code.
        if code != 0 and any(marker in text for marker in task.auto_recover):
            code, text = self.run_local_once(task)
        return code, text

    def run_local_once(self, task):
        if IS_WINDOWS:
            cmd = subprocess.list2cmdline([task.tool_path]) + " " + task.params
        else:
            cmd = [task.tool_path] + split_cmdline(task.params)
        try:
            p = subprocess.run(cmd, cwd=task.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            return p.returncode, decode_output(p.stdout)
        except OSError as e:
            return 1, "DistriBuild: failed to start %s: %s\n" % (task.tool_path, e)

    def compiler_id_for(self, path):
        if path not in self.ids:
            self.ids[path] = compiler_id(path) if compiler_family(path) else None
        return self.ids[path]

    def plan_remote(self, task):
        """Static check: can this task run on an agent at all? Returns (plan, None) or (None, reason).
        Agent availability is not considered here (see try_remote), so --analyze can use it offline."""
        if not task.allow_remote:
            return None, "not marked remote-safe by UBT (AllowRemote=False: link, PCH, codegen, ...)"
        plan = {"compiler": task.tool_path, "params": split_cmdline(task.params, windows=IS_WINDOWS),
                "dep_txt": None, "echo_includes": True}
        tool_name = base_name(task.tool_path).lower()
        if tool_name in ("cl-filter.exe", "cl-filter"):
            # UE 4.x: cl-filter.exe -dependencies=X.txt -compiler=cl.exe -stderronly -- cl.exe @rsp /showIncludes
            params = plan["params"]
            if "--" not in params or params.index("--") + 1 >= len(params):
                return None, "cl-filter command line without '-- <compiler>'"
            sep = params.index("--")
            plan["echo_includes"] = False  # cl-filter swallows the /showIncludes notes
            for opt in params[:sep]:
                if opt.lower().startswith("-dependencies="):
                    plan["dep_txt"] = opt.split("=", 1)[1]
                elif opt.lower().startswith("-timing="):
                    return None, "cl-filter writes a timing file (-timing)"
                elif opt.lower() == "-showincludes":
                    plan["echo_includes"] = True
            plan["compiler"] = params[sep + 1]
            plan["params"] = params[sep + 2:]
        family = compiler_family(plan["compiler"])
        if not family:
            return None, "tool is not a supported compiler (%s)" % base_name(plan["compiler"])
        try:
            argv = expand_response_files(plan["params"], IS_WINDOWS, task.cwd)
        except OSError as e:
            return None, "cannot read response file (%s)" % e
        try:
            job = analyze_msvc(argv) if family == "msvc" else analyze_gnu(argv)
        except Unsupported as e:
            return None, str(e)
        plan["family"], plan["job"] = family, job
        return plan, None

    def try_remote(self, task, plan):
        """Returns ((exit_code, output, agent_name), None) or (None, reason to run locally)."""
        family, job = plan["family"], plan["job"]
        cid = self.compiler_id_for(plan["compiler"])
        if not cid:
            return None, "cannot identify compiler version of %s" % plan["compiler"]
        if self.pool.capacity(cid) == 0:
            return None, "no agent has this exact compiler (%s)" % cid
        pch_key = self.pch_key(job, cid, task.cwd)
        lease, reason = self.pool.acquire(cid, pch_key)
        if lease is None:
            return None, reason
        agent, sock, reply = lease
        ok = False
        tmp = tempfile.mkdtemp(prefix="dbld_")
        try:
            sock.settimeout(float(self.cfg["job_timeout"]) + 60)
            if reply.get("need_pch"):
                self.upload_pch(sock, agent, os.path.join(task.cwd, job.pch_file))
            ipath = os.path.join(tmp, "src.i")
            if family == "msvc":
                # UE passes hundreds of include paths, more than a Windows command line can hold.
                boundary = os.path.join(tmp, "dbld_pch_boundary.h")
                with open(boundary, "wb") as f:
                    f.write(PCH_BOUNDARY + b"\n")
                pre_args = [a.replace("{BOUNDARY}", boundary) for a in job.pre_args]
                rsp = os.path.join(tmp, "pre.rsp")
                with open(rsp, "w", encoding="utf-16") as f:
                    f.write(subprocess.list2cmdline(pre_args + ["/P", "/Fi" + ipath, "/showIncludes"]))
                cmd = [plan["compiler"], "@" + rsp]
            else:
                cmd = [plan["compiler"]] + job.pre_args + ["-o", ipath]
            with self.pre_sem:
                p = subprocess.run(cmd, cwd=task.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            if p.returncode != 0 or not os.path.isfile(ipath):
                return None, "local preprocessing failed (the local compile shows the real error)"
            includes, notes = [], []
            if family == "msvc":
                prefix = msvc_include_prefix(plan["compiler"])
                for line in decode_output(p.stdout + p.stderr).splitlines():
                    if line.startswith(prefix) and "dbld_pch_boundary.h" not in line:
                        includes.append(line[len(prefix):].strip())
                        notes.append(line)
            with open(ipath, "rb") as f:
                pre_data = f.read()
            pch_fields = {}
            tail_start = 0
            if pch_key:
                at = pre_data.find(PCH_BOUNDARY)
                if at < 0:
                    return None, "precompiled header boundary not found in the preprocessed file"
                tail_start = pre_data.index(b"\n", at) + 1
                pch_fields = {"pch": pch_key, "pch_header": job.pch_header, "defines": job.defines}
            with self.pre_sem:
                if job.external_dirs:
                    pre_data = mark_external_regions(pre_data, job.external_dirs, job.warning_flags, task.cwd, tail_start)
                else:
                    pre_data = pre_data[tail_start:]

            outputs = [job.remote_output] + (["out.sarif"] if job.sarif else [])
            send_frame(sock, dict({"type": "run", "kind": "compile", "compiler": cid, "argv": job.remote_args,
                                   "outputs": outputs, "caption": task.caption}, **pch_fields), [("src.i", pre_data)])
            resp, blobs = recv_frame(sock)
            returned = dict(blobs)
            result = returned.get(job.remote_output)
            text = resp.get("stdout", "")
            # cl echoes the input file name; drop that line, keep diagnostics (they carry #line paths)
            text = "\n".join(l for l in text.splitlines() if l.strip() not in ("src.i", "")) + "\n"
            if not resp.get("ok") or resp.get("exit_code") != 0 or result is None:
                reason = "remote run on %s failed (%s)" % (
                    agent.name, resp.get("reason") or "exit code %s" % resp.get("exit_code"))
                if self.cfg["retry_remote_failures_locally"] or not resp.get("ok"):
                    return None, reason
                return (resp.get("exit_code") or 1, text, agent.name), None

            out_path = os.path.abspath(os.path.join(task.cwd, job.output))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path + ".dbld", "wb") as f:  # a cut-off write must not look like a finished object
                f.write(result)
            os.replace(out_path + ".dbld", out_path)
            if job.sarif and "out.sarif" in returned:
                with open(os.path.join(task.cwd, job.sarif), "wb") as f:
                    f.write(returned["out.sarif"])
            if family == "msvc" and job.depfile:
                write_msvc_depfile(os.path.join(task.cwd, job.depfile), os.path.join(task.cwd, job.source), includes)
            if plan["dep_txt"]:
                write_dependency_list(os.path.join(task.cwd, plan["dep_txt"]), includes)
            if job.show_includes and plan["echo_includes"]:
                text = "\n".join(notes) + "\n" + text
            ok = True
            return (0, text, agent.name), None
        except (OSError, ValueError, ConnectionError) as e:
            return None, "network/agent error on %s (%s)" % (agent.name, e)
        finally:
            self.pool.release(agent, sock, ok)
            shutil.rmtree(tmp, ignore_errors=True)

    def pch_key(self, job, cid, cwd):
        """Identifies this .pch build on the agents, or None to send its headers as text instead.

        Size and timestamp are enough: a rebuilt PCH gets a new key, and hashing 2 GB per job would not
        be worth it. Keys are per developer, since PCHs from two machines are never the same file.
        """
        if not (job.pch_header and self.cfg["share_pch"]):
            return None
        path = os.path.abspath(os.path.join(cwd, job.pch_file))
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return hashlib.sha1(("%s|%d|%d|%s" % (path, stat.st_size, stat.st_mtime_ns, cid)).encode("utf-8")).hexdigest()

    def upload_pch(self, sock, agent, path):
        t0 = time.time()
        send_frame(sock, {"type": "pch_put"})
        sent = send_file_stream(sock, path)
        reply, _ = recv_frame(sock)
        if not reply.get("ok"):
            raise ValueError("agent refused the precompiled header")
        with self.print_lock:
            sys.stdout.write("DistriBuild: sent %s to %s (%d MB on the wire, %.0fs)\n" % (
                os.path.basename(path), agent.name, sent // (1024 * 1024), time.time() - t0))
            sys.stdout.flush()

    def execute(self, task):
        code, text, where = 1, "", ""
        plan, reason = None, ""
        t0 = time.time()
        try:
            plan, reason = self.plan_remote(task)
            got_local = False
            result = None
            if plan is not None:
                got_local = self.cfg["local_first"] and self.local_sem.acquire(blocking=False)
                if got_local:
                    reason = "local core was free (local_first)"
                else:
                    result, reason = self.try_remote(task, plan)
            if result is not None:
                code, text, where = result
                reason = ""
            else:
                if not got_local:
                    self.local_sem.acquire()
                try:
                    code, text = self.run_local(task)
                finally:
                    self.local_sem.release()
            if self.cfg["verbose"] and reason and plan is not None:
                text = "DistriBuild: ran locally - %s\n%s" % (reason, text)
        except Exception as e:  # never leave the graph hanging
            code, text, reason = 1, "DistriBuild internal error: %r\n" % (e,), "internal error"
        task.where = where or "local"
        task.reason = reason or ""
        task.eligible = plan is not None
        task.seconds = time.time() - t0
        task.code = code
        try:
            self.emit(task, where, code, text)
        finally:
            self.complete(task, code == 0)  # never leave the graph hanging

    # ---------------------------------------------------------------- scheduling

    def start(self, task):
        with self.state_lock:
            self.pending_threads += 1
        self.executor.submit(self.execute, task)

    def complete(self, task, ok):
        ready = []
        with self.state_lock:
            self.pending_threads -= 1
            if not ok:
                self.failed.append(task)
                if self.stop_on_errors:
                    self.aborted = True
            if ok and not self.aborted:
                for d in task.dependents:
                    d.remaining -= 1
                    if d.remaining == 0:
                        ready.append(d)
            self.state_lock.notify_all()
        for d in ready:
            self.start(d)

    def run(self):
        roots = [t for t in self.tasks if t.remaining == 0]
        if not roots and self.tasks:
            print("DistriBuild: dependency cycle in task file")
            return 1
        workers = self.cfg["max_local"] + self.pool.capacity() + 2
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dbld")
        for t in roots:
            self.start(t)
        with self.state_lock:
            while self.pending_threads > 0:
                self.state_lock.wait(0.5)
        self.executor.shutdown(wait=True)
        skipped = len(self.tasks) - self.finished
        if skipped:
            print("DistriBuild: %d task(s) skipped because a dependency failed" % skipped)
        return 1 if self.failed or skipped else 0


def report_dir(cfg):
    path = cfg.get("report_dir") or os.path.join(os.path.expanduser("~"), ".distribuild")
    os.makedirs(path, exist_ok=True)
    return path


def short_reason(reason):
    """Groups reasons that differ only in details (agent name, flag value) for the summary."""
    return re.sub(r"\s*\(.*\)$", "", reason) if reason.startswith(("remote run on", "network/agent error")) else reason


def print_summary(tasks, title):
    groups = {}
    for t in tasks:
        if t.where == "local":
            key = ("eligible" if t.eligible else "structural", short_reason(t.reason) or "-")
            groups[key] = groups.get(key, 0) + 1
    remote = sum(1 for t in tasks if t.where not in ("", "local"))
    print("DistriBuild: %s" % title)
    print("  remote: %d   local: %d   skipped/not run: %d" % (
        remote, sum(groups.values()), sum(1 for t in tasks if not t.where)))
    for kind, label in (("structural", "cannot be distributed"), ("eligible", "could be distributed but ran locally")):
        items = sorted(((n, r) for (k, r), n in groups.items() if k == kind), reverse=True)
        if items:
            print("  %s:" % label)
            for n, r in items:
                print("    %5d  %s" % (n, r))


def write_report(tasks, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["task", "ran_on", "remote_eligible", "reason_local", "seconds", "exit_code", "tool"])
        for t in tasks:
            w.writerow([t.caption, t.where or "not run", "yes" if t.eligible else "no", t.reason,
                        "%.2f" % t.seconds, "" if t.code is None else t.code, base_name(t.tool_path)])


def analyze(xml_path, cfg):
    """--analyze: classify every task of a task file without building anything."""
    tasks = load_tasks(xml_path)
    builder = Builder(tasks, cfg, None, False)
    for t in tasks:
        plan, reason = builder.plan_remote(t)
        t.eligible = plan is not None
        t.where = "local"
        t.reason = "" if plan else reason
    eligible = sum(1 for t in tasks if t.eligible)
    print("DistriBuild analysis of %s" % xml_path)
    print("  %d tasks, %d can run on agents, %d always run locally" % (len(tasks), eligible, len(tasks) - eligible))
    groups = {}
    for t in tasks:
        if not t.eligible:
            groups.setdefault(t.reason, []).append(t.caption)
    for reason, captions in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print("  %5d  %s" % (len(captions), reason))
        for c in captions[:5]:
            print("           e.g. %s" % c)
    path = os.path.join(report_dir(cfg), "analysis.csv")
    write_report(tasks, path)
    print("  per-task list: %s" % path)
    return 0


def installed_msvc_compilers():
    vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                           "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if not os.path.isfile(vswhere):
        return []
    try:
        out = subprocess.run([vswhere, "-all", "-products", "*", "-property", "installationPath"],
                             stdout=subprocess.PIPE, timeout=30).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for install in filter(None, map(str.strip, out.splitlines())):
        found += glob.glob(os.path.join(install, "VC", "Tools", "MSVC", "*", "bin", "Hostx64", "x64", "cl.exe"))
    return found


def doctor(cfg):
    """--doctor: the conditions UBT's XGE.IsAvailable checks, then what the agents offer."""
    problems = []

    def check(ok, text, fix=""):
        print("  [%s] %s" % ("ok" if ok else "!!", text))
        if not ok:
            problems.append(fix or text)

    print("UnrealBuildTool:")
    if IS_WINDOWS:
        import winreg
        real_folder = None
        for key in (r"SOFTWARE\Xoreax\IncrediBuild\Builder", r"SOFTWARE\WOW6432Node\Xoreax\IncrediBuild\Builder"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    real_folder = winreg.QueryValueEx(k, "Folder")[0]
            except OSError:
                pass
        check(not real_folder, "no real Incredibuild registered" if not real_folder else "Incredibuild is registered at %s" % real_folder,
              "UBT will run the real Incredibuild. Uninstall it to use DistriBuild.")
        sc = subprocess.run(["sc", "query", "Incredibuild Agent"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        running = sc.returncode == 0 and re.search(rb":\s*4\s+RUNNING", sc.stdout) is not None
        check(running, "service 'Incredibuild Agent' is running",
              "UBT will not use its XGE executor without this service. Run install_client.bat as administrator.")
    on_path = shutil.which("xgConsole")
    me = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else "")
    check(bool(on_path), "xgConsole on PATH: %s" % on_path, "Put xgConsole.exe in a folder on PATH (install_client.bat does this).")
    if on_path and getattr(sys, "frozen", False):
        check(os.path.normcase(os.path.abspath(on_path)) == os.path.normcase(me), "PATH resolves to this DistriBuild xgConsole",
              "Another xgConsole.exe comes first on PATH: %s" % on_path)
    for path in (os.path.join(os.environ.get("APPDATA", ""), "Unreal Engine", "UnrealBuildTool", "BuildConfiguration.xml"),
                 os.path.join(os.path.expanduser("~"), "Documents", "Unreal Engine", "UnrealBuildTool", "BuildConfiguration.xml")):
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                off = re.search(r"<bAllowXGE>\s*false", f.read(), re.I) is not None
            check(not off, "%s allows XGE" % path, "Remove <bAllowXGE>false</bAllowXGE> from %s" % path)

    print("Agents:")
    pool = AgentPool(cfg)
    check(pool.capacity() > 0, "%d remote slots [%s]" % (pool.capacity(), pool.describe()),
          "No agents answered. Start agent.py on the helper PCs, or list them in client.json; check firewall TCP 31890, UDP 31891.")
    print("Compilers on this PC:")
    for path in installed_msvc_compilers():
        cid = compiler_id(path)
        slots = pool.capacity(cid) if cid else 0
        check(slots > 0 or pool.capacity() == 0, "%s: %d remote slots" % (cid, slots),
              "No agent has %s. Builds using it will run locally; install that MSVC version on an agent." % cid)

    print()
    if problems:
        print("Problems:")
        for p in problems:
            print("  - " + p)
        return 1
    print("Everything looks right. A UBT build should print 'DistriBuild: N actions, ...'.")
    return 0


def main(argv):
    # UBT decodes this pipe with the console code page, whatever PYTHONIOENCODING says. Not every
    # character exists in that code page, and a failed write must not stop the build.
    encoding = None
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32
        encoding = "cp%d" % (kernel32.GetConsoleOutputCP() or kernel32.GetOEMCP())
    sys.stdout.reconfigure(encoding=encoding, errors="replace")
    sys.stderr.reconfigure(encoding=encoding, errors="replace")
    xml_path = None
    stop_on_errors = False
    analyze_only = False
    for a in argv[1:]:
        low = a.lower()
        if low in ("--doctor", "/doctor"):
            return doctor(load_config())
        if low == "/stoponerrors":
            stop_on_errors = True
        elif low in ("--analyze", "/analyze"):
            analyze_only = True
        elif low.endswith(".xml") or (not a.startswith("/") and os.path.isfile(a)):
            xml_path = a
        elif low in ("/?", "/help", "--help"):
            print(__doc__)
            return 0
    if not xml_path:
        print("usage: xgConsole <TaskFile.xml> [/StopOnErrors] [other XGE flags are ignored]")
        print("       xgConsole --analyze <TaskFile.xml>   (what can / cannot be distributed, no build)")
        print("       xgConsole --doctor                   (will UBT pick DistriBuild? can agents take the jobs?)")
        return 1

    cfg = load_config()
    if analyze_only:
        return analyze(xml_path, cfg)

    out_dir = report_dir(cfg)
    try:
        shutil.copyfile(xml_path, os.path.join(out_dir, "last_tasks.xml"))
    except OSError:
        pass
    tasks = load_tasks(xml_path)
    t0 = time.time()
    pool = AgentPool(cfg)
    remote = pool.capacity()
    print("DistriBuild: %d actions, %d local slots, %d remote slots%s" % (
        len(tasks), cfg["max_local"], remote, (" [%s]" % pool.describe()) if remote else ""))
    sys.stdout.flush()
    builder = Builder(tasks, cfg, pool, stop_on_errors)
    code = builder.run()
    print_summary(tasks, "finished in %.1fs" % (time.time() - t0))
    report = os.path.join(out_dir, "last_build.csv")
    try:
        write_report(tasks, report)
        print("  per-task report: %s" % report)
    except OSError as e:
        print("  could not write report %s: %s" % (report, e))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
