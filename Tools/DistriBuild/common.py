"""DistriBuild shared code: wire protocol, command-line helpers, compiler identification.

Wire format (identical in the Python tools and the UE plugin):

    "DBLD" | u32 version | u32 header_len | header JSON (UTF-8) | blob0 | blob1 | ...

header["files"] lists the blobs in order: {"name", "size", "csize", "codec": "zlib"|"raw"}.
All integers are little-endian.
"""

import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import zlib

MAGIC = b"DBLD"
PROTOCOL_VERSION = 1
DEFAULT_PORT = 31890
DISCOVERY_PORT = 31891
DISCOVERY_PROBE = b"DBLD?"

MAX_HEADER_BYTES = 16 * 1024 * 1024
MAX_BLOB_BYTES = 1024 * 1024 * 1024

IS_WINDOWS = os.name == "nt"
SAFE_BLOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# --------------------------------------------------------------------------- framing

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("connection closed by peer")
        buf += chunk
    return bytes(buf)


def send_frame(sock, header, blobs=()):
    """Sends a header dict plus (name, bytes) blobs. Returns bytes put on the wire."""
    header = dict(header)
    files, payloads = [], []
    for name, data in blobs:
        packed = zlib.compress(data, 1) if len(data) > 256 else data
        if len(packed) < len(data):
            files.append({"name": name, "size": len(data), "csize": len(packed), "codec": "zlib"})
            payloads.append(packed)
        else:
            files.append({"name": name, "size": len(data), "csize": len(data), "codec": "raw"})
            payloads.append(data)
    header["files"] = files
    raw_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
    sock.sendall(MAGIC + struct.pack("<II", PROTOCOL_VERSION, len(raw_header)) + raw_header)
    total = 12 + len(raw_header)
    for p in payloads:
        sock.sendall(p)
        total += len(p)
    return total


def recv_frame(sock):
    """Returns (header dict, [(name, bytes)])."""
    if recv_exact(sock, 4) != MAGIC:
        raise ValueError("bad magic")
    version, header_len = struct.unpack("<II", recv_exact(sock, 8))
    if version != PROTOCOL_VERSION:
        raise ValueError("protocol version mismatch: %d" % version)
    if header_len > MAX_HEADER_BYTES:
        raise ValueError("header too large")
    header = json.loads(recv_exact(sock, header_len).decode("utf-8"))
    blobs = []
    for f in header.get("files", []):
        size, csize = int(f["size"]), int(f["csize"])
        if size > MAX_BLOB_BYTES or csize > MAX_BLOB_BYTES:
            raise ValueError("blob too large")
        data = recv_exact(sock, csize)
        if f.get("codec") == "zlib":
            data = zlib.decompress(data)
        if len(data) != size:
            raise ValueError("blob size mismatch for %s" % f.get("name"))
        blobs.append((f["name"], data))
    return header, blobs


def request(host, port, header, blobs=(), timeout=5.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        send_frame(s, header, blobs)
        return recv_frame(s)


def parse_agent_address(text):
    text = text.strip()
    if not text:
        return None
    host, _, port = text.rpartition(":")
    if not host:
        return text, DEFAULT_PORT
    return host, int(port)


def discover_agents(port=DISCOVERY_PORT, wait=0.7):
    """UDP broadcast probe. Returns list of (host, info dict)."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.1)
        for target in ("255.255.255.255", "127.0.0.1"):
            try:
                s.sendto(DISCOVERY_PROBE, (target, port))
            except OSError:
                pass
        import time
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            found[(addr[0], int(info.get("port", DEFAULT_PORT)))] = info
    finally:
        s.close()
    return [(host, info) for (host, _), info in found.items()]


# --------------------------------------------------------------------------- command lines

def split_windows_cmdline(s):
    """Splits a command line using the CommandLineToArgvW rules MSVC uses."""
    args, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        cur, in_quotes = [], False
        while i < n:
            c = s[i]
            if c == "\\":
                j = i
                while j < n and s[j] == "\\":
                    j += 1
                count = j - i
                if j < n and s[j] == '"':
                    cur.append("\\" * (count // 2))
                    if count % 2:
                        cur.append('"')
                        i = j + 1
                    else:
                        i = j
                else:
                    cur.append("\\" * count)
                    i = j
                continue
            if c == '"':
                if in_quotes and i + 1 < n and s[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_quotes = not in_quotes
                i += 1
                continue
            if not in_quotes and c in " \t\r\n":
                break
            cur.append(c)
            i += 1
        args.append("".join(cur))
    return args


def split_cmdline(s, windows=IS_WINDOWS):
    return split_windows_cmdline(s) if windows else shlex.split(s)


def join_cmdline(argv, windows=IS_WINDOWS):
    return subprocess.list2cmdline(argv) if windows else shlex.join(argv)


def read_text_any(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if IS_WINDOWS else "latin-1")


def expand_response_files(argv, windows, cwd, depth=0):
    out = []
    for a in argv:
        if a.startswith("@") and len(a) > 1 and depth < 8:
            path = a[1:]
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            out.extend(expand_response_files(split_cmdline(read_text_any(path), windows), windows, cwd, depth + 1))
        else:
            out.append(a)
    return out


# --------------------------------------------------------------------------- compilers

def base_name(path):
    """File name of a Windows or POSIX path, whatever OS this runs on."""
    return re.split(r"[\\/]", path)[-1]


def compiler_family(path):
    """'msvc' for cl/clang-cl style drivers, 'gnu' for gcc/clang, None otherwise."""
    name = base_name(path).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in ("cl", "clang-cl") or name.endswith("-clang-cl"):
        return "msvc"
    if re.match(r"^(.*-)?(clang|clang\+\+|gcc|g\+\+|cc|c\+\+)(-\d+(\.\d+)*)?$", name):
        return "gnu"
    return None


_id_cache = {}
_id_lock = threading.Lock()


def compiler_id(path):
    """Stable identifier for a compiler build, computed the same way on client and agent so
    a job only goes to an agent with the exact same compiler."""
    resolved = shutil.which(path) or path
    with _id_lock:
        if resolved in _id_cache:
            return _id_cache[resolved]
    cid = None
    family = compiler_family(resolved)
    name = os.path.basename(resolved).lower()
    try:
        if family == "msvc" and name.startswith("cl"):
            env = dict(os.environ, VSLANG="1033")
            p = subprocess.run([resolved], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, timeout=30)
            banner = (p.stderr + p.stdout).decode("utf-8", "replace")
            m = re.search(r"Version\s+([\d.]+)\s+for\s+(\S+)", banner)
            if m:
                cid = "msvc-%s-%s" % (m.group(1), m.group(2))
        elif family in ("msvc", "gnu"):
            p = subprocess.run([resolved, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=30)
            lines = p.stdout.decode("utf-8", "replace").splitlines()
            first = lines[0].strip() if lines else ""
            target = next((l.split(":", 1)[1].strip() for l in lines if l.startswith("Target:")), "")
            if not target and family == "gnu":
                q = subprocess.run([resolved, "-dumpmachine"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=30)
                target = q.stdout.decode().strip()
            # The driver name matters for gcc vs g++ (language defaults), so keep it.
            driver = re.sub(r"(\.exe)?$", "", name)
            if first:
                cid = "%s|%s|%s|%s" % (family, driver, first, target)
    except (OSError, subprocess.SubprocessError):
        cid = None
    with _id_lock:
        _id_cache[resolved] = cid
    return cid


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
