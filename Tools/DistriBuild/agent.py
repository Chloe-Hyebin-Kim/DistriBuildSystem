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
  "pch_cache_gb": 40,   # precompiled headers kept for C++ jobs; one UE PCH is 1-3 GB
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
import struct
import subprocess
import sys
import tempfile
import threading
import time

from common import (DEFAULT_PORT, DISCOVERY_PORT, DISCOVERY_PROBE, IS_WINDOWS, SAFE_BLOB_NAME, SAFE_BUNDLE_HASH,
                    compiler_family, compiler_id, decode_output, log, recv_file_stream, recv_frame,
                    remap_shader_source_dirs, send_frame, split_cmdline)

BIN_PLATFORM = {"Windows": "Win64", "Linux": "Linux", "Darwin": "Mac"}.get(platform.system(), "Win64")
SCW_NAME = "ShaderCompileWorker.exe" if IS_WINDOWS else "ShaderCompileWorker"
KEEP_SOURCE_BUNDLES = 6
# Helper PCs are often someone's workstation; builds must not make them unresponsive.
JOB_CREATION_FLAGS = subprocess.BELOW_NORMAL_PRIORITY_CLASS if IS_WINDOWS else 0

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
        self.bundle_root = os.path.join(self.work_root, "source_bundles")
        os.makedirs(self.bundle_root, exist_ok=True)
        self.pch_root = os.path.join(self.work_root, "pch_cache")
        os.makedirs(self.pch_root, exist_ok=True)
        self.pch_cache_bytes = int(float(cfg.get("pch_cache_gb", 40)) * 1024 ** 3)
        self.uploading = set()
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

    # ------------------------------------------------------------------ UE 4.26 shader sources

    def bundle_path(self, bundle):
        return os.path.join(self.bundle_root, bundle.lower())

    def pch_path(self, key):
        return os.path.join(self.pch_root, key.lower() + ".pch")

    def claim_upload(self, key, cached_path):
        """'have' = cached, 'upload' = this client should send it, 'wait' = another client is sending it."""
        with self.lock:
            if os.path.exists(cached_path):
                return "have"
            if key in self.uploading:
                return "wait"
            self.uploading.add(key)
            return "upload"

    def end_upload(self, key):
        with self.lock:
            self.uploading.discard(key)

    def receive_pch(self, sock, key):
        """Stores an uploaded PCH, then drops the least recently used ones over the size limit."""
        tmp = self.pch_path(key) + ".incoming"
        try:
            recv_file_stream(sock, tmp)
            os.replace(tmp, self.pch_path(key))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        cached = [os.path.join(self.pch_root, f) for f in os.listdir(self.pch_root) if f.endswith(".pch")]
        cached.sort(key=os.path.getmtime, reverse=True)
        total = 0
        for path in cached:
            total += os.path.getsize(path)
            if total > self.pch_cache_bytes and path != self.pch_path(key):
                try:
                    os.remove(path)
                except OSError:  # a compile has it open
                    pass

    def install_bundle(self, bundle, dirs, files, blobs):
        """Unpacks a source bundle next to the others; the final rename makes it visible atomically."""
        tmp = tempfile.mkdtemp(prefix="incoming_", dir=self.bundle_root)
        try:
            for index in dirs.values():
                os.makedirs(os.path.join(tmp, str(int(index))), exist_ok=True)
            for entry in files:
                parts = str(entry["path"]).replace("\\", "/").split("/")
                if any(part in ("", ".", "..") or ":" in part for part in parts):
                    raise ValueError("bad source path %r" % entry["path"])
                path = os.path.join(tmp, str(int(entry["dir"])), *parts)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(blobs[entry["blob"]])
            try:
                os.rename(tmp, self.bundle_path(bundle))
            except OSError:
                if not os.path.isdir(self.bundle_path(bundle)):
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        cached = [os.path.join(self.bundle_root, d) for d in os.listdir(self.bundle_root) if SAFE_BUNDLE_HASH.match(d)]
        cached.sort(key=os.path.getmtime, reverse=True)
        for old in cached[KEEP_SOURCE_BUNDLES:]:
            shutil.rmtree(old, ignore_errors=True)

    def apply_source_bundle(self, hdr, job_path):
        """Points a 4.26 job's shader directory mappings at the cached bundle. Returns an error or None."""
        bundle = hdr["source_bundle"]
        if not SAFE_BUNDLE_HASH.match(bundle):
            return "bad source bundle name"
        root = self.bundle_path(bundle)
        if not os.path.isdir(root):
            return "source bundle %s is not cached" % bundle
        os.utime(root)
        dirs = hdr.get("source_dirs", {})
        with open(job_path, "rb") as f:
            job = f.read()
        try:
            job = remap_shader_source_dirs(job, lambda virtual: os.path.join(root, str(int(dirs[virtual]))).replace("\\", "/"))
        except KeyError as e:
            return "shader directory %s is not in the source bundle" % e
        except (struct.error, ValueError, UnicodeDecodeError) as e:
            return "cannot read job file: %s" % e
        with open(job_path, "wb") as f:
            f.write(job)
        return None

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

    def _pch_args(self, hdr, key):
        """Arguments that use a cached PCH, or a string saying why they cannot be built."""
        if not SAFE_BUNDLE_HASH.match(key) or not os.path.isfile(self.pch_path(key)):
            return "precompiled header %s is not cached" % key
        os.utime(self.pch_path(key))
        # The header name only has to match the one the PCH was created with. The compiler restores
        # the PCH instead of opening that file, so this client path need not exist here. UBT writes
        # it relative to Engine/Source for engine and plugin modules, so ".." is normal.
        header = str(hdr.get("pch_header", ""))
        if not header or header.startswith("@"):
            return "bad precompiled header name"
        defines = [str(d) for d in hdr.get("defines", [])]
        if not all(d.startswith(("/D", "/U", "-D", "-U")) for d in defines):
            return "bad define"
        return defines + ["/Yu" + header, "/FI" + header, "/Fp" + self.pch_path(key)]

    def execute(self, hdr, blobs):
        kind = hdr.get("kind")
        work = tempfile.mkdtemp(prefix="job_", dir=self.work_root)
        try:
            source_files = hdr.get("source_files")
            if source_files:
                if not SAFE_BUNDLE_HASH.match(hdr.get("source_bundle", "")):
                    return {"ok": False, "reason": "bad source bundle name", "exit_code": -1}, []
                try:
                    self.install_bundle(hdr["source_bundle"], hdr.get("source_dirs", {}), source_files, dict(blobs))
                except (KeyError, ValueError, OSError) as e:
                    return {"ok": False, "reason": "cannot install source bundle: %s" % e, "exit_code": -1}, []
                bundled = set(entry["blob"] for entry in source_files)
                blobs = [b for b in blobs if b[0] not in bundled]

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
                if hdr.get("source_bundle"):
                    problem = self.apply_source_bundle(hdr, os.path.join(work, inp))
                    if problem:
                        return {"ok": False, "reason": problem, "exit_code": -1}, []
                extra = [a for a in split_cmdline(hdr.get("args", ""), windows=IS_WINDOWS) if a != "-xge_int"]
                # Parent PID = this agent: SCW quits when its "parent" dies, so it lives as long as we do.
                # -xge_int must be the sixth argument: it makes SCW exit after this job instead of waiting for more.
                argv = [scw, work.replace("\\", "/") + "/", str(os.getpid()), "0", inp, out, "-xge_int"] + extra
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
                pch = hdr.get("pch")
                if pch:
                    problem = self._pch_args(hdr, pch)
                    if isinstance(problem, str):
                        return {"ok": False, "reason": problem, "exit_code": -1}, []
                    argv += problem
            else:
                return {"ok": False, "reason": "unknown job kind %r" % kind, "exit_code": -1}, []

            if not all(SAFE_BLOB_NAME.match(o) for o in outputs):
                return {"ok": False, "reason": "bad output name", "exit_code": -1}, []

            t0 = time.time()
            try:
                p = subprocess.run(argv, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=self.job_timeout,
                                   creationflags=JOB_CREATION_FLAGS)
            except subprocess.TimeoutExpired:
                return {"ok": False, "reason": "timeout", "exit_code": -1}, []
            out_blobs = []
            for o in outputs:
                path = os.path.join(work, o)
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        out_blobs.append((o, f.read()))
            text = decode_output(p.stdout).replace(work + os.sep, "").replace(work, ".")
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
            bundle, pch = hdr.get("source_bundle"), hdr.get("pch")
            upload_key = None
            try:
                key = bundle or pch
                if key:
                    if not SAFE_BUNDLE_HASH.match(key):
                        send_frame(sock, {"ok": False, "reason": "bad upload key"})
                        return
                    state = agent.claim_upload(key, agent.bundle_path(key) if bundle else agent.pch_path(key))
                    if state == "wait":  # the client tries another agent, or this job runs on its own machine
                        send_frame(sock, {"ok": False, "reason": "busy" if bundle else "uploading"})
                        return
                    if state == "upload":
                        upload_key = key
                send_frame(sock, {"ok": True, "name": agent.name, "need_bundle": bool(upload_key and bundle),
                                  "need_pch": bool(upload_key and pch)})
                sock.settimeout(agent.job_timeout + 60)
                if upload_key and pch:
                    put, _ = recv_frame(sock)
                    if put.get("type") != "pch_put":
                        return
                    agent.receive_pch(sock, pch)
                    send_frame(sock, {"ok": True})
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
                if upload_key:
                    agent.end_upload(upload_key)
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
