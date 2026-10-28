#!/usr/bin/env python3
"""DistriBuild agent: runs on every helper machine and executes shader / C++ compile jobs.

    python agent.py                     # auto-detect engines + compilers, slots = CPU count
    python agent.py --config agent.json
    python agent.py --print-detected    # show what would be offered

agent.json (all keys optional):
{
  "name": "BUILD-PC-07",
  "port": 31890,
  "discovery_port": 31891,
  "slots": 16,
  "token": "shared-secret",
  "work_dir": "D:/DistriBuildWork",
  "job_timeout": 600,
  "engines": ["C:/Program Files/Epic Games/UE_5.4", "D:/UE5-Source"],
  "compilers": ["C:/Program Files/Microsoft Visual Studio/2022/Professional/VC/Tools/MSVC/14.38.33130/bin/Hostx64/x64/cl.exe"],
  "auto_detect": true
}
"""

import argparse
import glob
import json
import os
import platform
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

from common import (DEFAULT_PORT, DISCOVERY_PORT, DISCOVERY_PROBE, IS_WINDOWS, SAFE_BLOB_NAME,
                    compiler_family, compiler_id, log, recv_frame, send_frame, split_cmdline)

BIN_PLATFORM = {"Windows": "Win64", "Linux": "Linux", "Darwin": "Mac"}.get(platform.system(), "Win64")
SCW_NAME = "ShaderCompileWorker.exe" if IS_WINDOWS else "ShaderCompileWorker"

# Paths a compile job may legitimately mention are all inside the job directory ({WORK}).
# Anything else that looks absolute is refused so a client cannot write outside the sandbox.
WIN_ABS = re.compile(r"(^|[=:,\"])([A-Za-z]:[\\/]|\\\\)")


# --------------------------------------------------------------------------- detection

def engine_key_from_root(root):
    version_file = os.path.join(root, "Engine", "Build", "Build.version")
    try:
        with open(version_file, "r", encoding="utf-8-sig") as f:
            v = json.load(f)
        return "%d.%d.%d" % (v["MajorVersion"], v["MinorVersion"], v["PatchVersion"])
    except (OSError, ValueError, KeyError):
        return None


def detect_engines(roots, auto):
    candidates = list(roots)
    if auto:
        if IS_WINDOWS:
            for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"D:\Program Files"):
                candidates += glob.glob(os.path.join(base, "Epic Games", "UE_*"))
        else:
            candidates += glob.glob(os.path.expanduser("~/UnrealEngine*")) + glob.glob("/opt/UnrealEngine*")
        if os.environ.get("UE_ROOT"):
            candidates.append(os.environ["UE_ROOT"])
    engines = {}
    for root in candidates:
        key = engine_key_from_root(root)
        scw = os.path.join(root, "Engine", "Binaries", BIN_PLATFORM, SCW_NAME)
        if key and os.path.isfile(scw):
            engines.setdefault(key, scw)
    return engines


def detect_compilers(paths, auto):
    candidates = list(paths)
    if auto:
        if IS_WINDOWS:
            vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                                   "Microsoft Visual Studio", "Installer", "vswhere.exe")
            installs = []
            if os.path.isfile(vswhere):
                try:
                    out = subprocess.run([vswhere, "-all", "-products", "*", "-property", "installationPath"],
                                         stdout=subprocess.PIPE, timeout=30).stdout.decode("utf-8", "replace")
                    installs = [l.strip() for l in out.splitlines() if l.strip()]
                except (OSError, subprocess.SubprocessError):
                    pass
            for inst in installs:
                candidates += glob.glob(os.path.join(inst, "VC", "Tools", "MSVC", "*", "bin", "Host*", "*", "cl.exe"))
                candidates += glob.glob(os.path.join(inst, "VC", "Tools", "Llvm", "x64", "bin", "clang-cl.exe"))
        for name in ("clang", "clang++", "gcc", "g++", "clang-cl"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    compilers = {}
    for path in candidates:
        if not os.path.isfile(path) or not compiler_family(path):
            continue
        cid = compiler_id(path)
        if cid:
            compilers.setdefault(cid, path)
    return compilers


# --------------------------------------------------------------------------- agent

class Agent:
    def __init__(self, cfg):
        self.name = cfg.get("name") or socket.gethostname()
        self.port = int(cfg.get("port", DEFAULT_PORT))
        self.discovery_port = int(cfg.get("discovery_port", DISCOVERY_PORT))
        self.slots = max(1, int(cfg.get("slots") or os.cpu_count() or 4))
        self.token = cfg.get("token", "")
        self.job_timeout = float(cfg.get("job_timeout", 600))
        self.work_root = cfg.get("work_dir") or os.path.join(tempfile.gettempdir(), "DistriBuildAgent")
        os.makedirs(self.work_root, exist_ok=True)
        auto = cfg.get("auto_detect", True)
        self.engines = detect_engines(cfg.get("engines", []), auto)
        self.compilers = detect_compilers(cfg.get("compilers", []), auto)
        self.lock = threading.Lock()
        self.active = 0
        self.completed = 0
        self.failed = 0
        self.started = time.time()

    def info(self):
        with self.lock:
            return {
                "type": "info", "protocol": 1, "name": self.name, "port": self.port,
                "platform": platform.system(), "slots": self.slots, "active": self.active,
                "free": self.slots - self.active, "engines": sorted(self.engines),
                "compilers": sorted(self.compilers), "completed": self.completed,
                "failed": self.failed, "uptime": int(time.time() - self.started),
            }

    def try_reserve(self):
        with self.lock:
            if self.active >= self.slots:
                return False
            self.active += 1
            return True

    def release(self, ok):
        with self.lock:
            self.active -= 1
            if ok:
                self.completed += 1
            else:
                self.failed += 1

    # ------------------------------------------------------------------ job execution

    def _check_compile_args(self, family, argv):
        for a in argv:
            if a.startswith("@"):
                return "response files are not accepted"
            check = a.replace("{WORK}", "")
            if ".." in re.split(r"[\\/=:,]", check):
                return "parent directory reference in %r" % a
            if WIN_ABS.search(check):
                return "absolute path in %r" % a
            if family == "gnu":
                value = check.split("=", 1)[1] if check.startswith("-") and "=" in check else check
                if value.startswith("/") and "{WORK}" not in a:
                    return "absolute path in %r" % a
        return None

    def execute(self, hdr, blobs):
        kind = hdr.get("kind")
        work = tempfile.mkdtemp(prefix="job_", dir=self.work_root)
        try:
            for name, data in blobs:
                if not SAFE_BLOB_NAME.match(name):
                    return {"ok": False, "reason": "bad file name %r" % name, "exit_code": -1}, []
                with open(os.path.join(work, name), "wb") as f:
                    f.write(data)

            outputs = hdr.get("outputs", [])
            if kind == "shader":
                scw = self.engines.get(hdr.get("engine", ""))
                if not scw:
                    return {"ok": False, "reason": "engine %s not installed" % hdr.get("engine"), "exit_code": -1}, []
                inp, out = hdr.get("input", ""), hdr.get("output", "")
                if not (SAFE_BLOB_NAME.match(inp) and SAFE_BLOB_NAME.match(out)):
                    return {"ok": False, "reason": "bad shader file names", "exit_code": -1}, []
                extra = split_cmdline(hdr.get("args", ""), windows=IS_WINDOWS)
                # Parent PID = this agent: SCW quits when its "parent" dies, so it lives as long as we do.
                argv = [scw, work + os.sep, str(os.getpid()), "0", inp, out] + extra
                outputs = [out]
            elif kind == "compile":
                cid = hdr.get("compiler", "")
                exe = self.compilers.get(cid)
                if not exe:
                    return {"ok": False, "reason": "compiler not installed: %s" % cid, "exit_code": -1}, []
                raw = [str(a) for a in hdr.get("argv", [])]
                problem = self._check_compile_args(compiler_family(exe), raw)
                if problem:
                    return {"ok": False, "reason": problem, "exit_code": -1}, []
                argv = [exe] + [a.replace("{WORK}", work) for a in raw]
            else:
                return {"ok": False, "reason": "unknown job kind %r" % kind, "exit_code": -1}, []

            if not all(SAFE_BLOB_NAME.match(o) for o in outputs):
                return {"ok": False, "reason": "bad output name", "exit_code": -1}, []

            env = dict(os.environ, VSLANG="1033")
            t0 = time.time()
            try:
                p = subprocess.run(argv, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=self.job_timeout, env=env)
            except subprocess.TimeoutExpired:
                return {"ok": False, "reason": "timeout", "exit_code": -1}, []
            out_blobs = []
            for o in outputs:
                path = os.path.join(work, o)
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        out_blobs.append((o, f.read()))
            text = p.stdout.decode("utf-8", "replace").replace(work + os.sep, "").replace(work, ".")
            return {"ok": True, "exit_code": p.returncode, "stdout": text[-65536:],
                    "elapsed": round(time.time() - t0, 3), "agent": self.name}, out_blobs
        finally:
            shutil.rmtree(work, ignore_errors=True)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        agent = self.server.agent
        sock = self.request
        sock.settimeout(60)
        try:
            hdr, _ = recv_frame(sock)
            if agent.token and hdr.get("token") != agent.token:
                send_frame(sock, {"ok": False, "reason": "auth"})
                return
            kind = hdr.get("type")
            if kind == "info":
                send_frame(sock, agent.info())
                return
            if kind != "reserve":
                send_frame(sock, {"ok": False, "reason": "expected reserve"})
                return
            if not agent.try_reserve():
                send_frame(sock, {"ok": False, "reason": "busy"})
                return
            ok = False
            try:
                send_frame(sock, {"ok": True, "name": agent.name})
                sock.settimeout(agent.job_timeout + 60)
                run_hdr, run_blobs = recv_frame(sock)
                if run_hdr.get("type") != "run":
                    return
                resp, out = agent.execute(run_hdr, run_blobs)
                ok = resp.get("ok") and resp.get("exit_code") == 0
                send_frame(sock, resp, out)
                log("[%s] %-7s %-40.40s exit=%s %.2fs" % (time.strftime("%H:%M:%S"), run_hdr.get("kind"),
                    run_hdr.get("caption", ""), resp.get("exit_code"), resp.get("elapsed", 0)))
                if not resp.get("ok"):
                    log("    rejected: %s" % resp.get("reason"))
            finally:
                agent.release(ok)
        except (OSError, ValueError, ConnectionError) as e:
            log("connection from %s failed: %s" % (self.client_address[0], e))


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def discovery_loop(agent):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", agent.discovery_port))
    except OSError as e:
        log("discovery disabled (cannot bind UDP %d): %s" % (agent.discovery_port, e))
        return
    while True:
        try:
            data, addr = s.recvfrom(2048)
            if data.startswith(DISCOVERY_PROBE):
                s.sendto(json.dumps(agent.info()).encode("utf-8"), addr)
        except OSError:
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description="DistriBuild agent")
    ap.add_argument("--config", help="agent JSON config")
    ap.add_argument("--port", type=int)
    ap.add_argument("--slots", type=int)
    ap.add_argument("--token")
    ap.add_argument("--engine", action="append", default=[], help="engine root (repeatable)")
    ap.add_argument("--compiler", action="append", default=[], help="compiler executable (repeatable)")
    ap.add_argument("--no-auto-detect", action="store_true")
    ap.add_argument("--print-detected", action="store_true")
    args = ap.parse_args()

    cfg = {}
    if args.config:
        with open(args.config, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    if args.port:
        cfg["port"] = args.port
    if args.slots:
        cfg["slots"] = args.slots
    if args.token is not None:
        cfg["token"] = args.token
    cfg["engines"] = cfg.get("engines", []) + args.engine
    cfg["compilers"] = cfg.get("compilers", []) + args.compiler
    if args.no_auto_detect:
        cfg["auto_detect"] = False

    agent = Agent(cfg)
    log("DistriBuild agent '%s'  port=%d  slots=%d" % (agent.name, agent.port, agent.slots))
    for k, v in sorted(agent.engines.items()):
        log("  engine   %-10s %s" % (k, v))
    for k, v in sorted(agent.compilers.items()):
        log("  compiler %s\n           -> %s" % (k, v))
    if not agent.engines and not agent.compilers:
        log("  WARNING: no engines or compilers detected; this agent cannot run any job.")
    if args.print_detected:
        return

    threading.Thread(target=discovery_loop, args=(agent,), daemon=True).start()
    server = Server(("", agent.port), Handler)
    server.agent = agent
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
