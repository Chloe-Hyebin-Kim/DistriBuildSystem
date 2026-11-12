"""DistriBuild shared code: wire protocol, command-line helpers, compiler identification.

Wire format (identical in the Python tools and the UE plugin):

    "DBLD" | u32 version | u32 header_len | header JSON (UTF-8) | blob0 | blob1 | ...

header["files"] lists the blobs in order: {"name", "size", "csize", "codec": "zlib"|"raw"}.
All integers are little-endian.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

MAGIC = b"DBLD"
PROTOCOL_VERSION = 2
DEFAULT_PORT = 31890
DISCOVERY_PORT = 31891
DISCOVERY_PROBE = b"DBLD?"

MAX_HEADER_BYTES = 16 * 1024 * 1024
MAX_BLOB_BYTES = 1024 * 1024 * 1024

IS_WINDOWS = os.name == "nt"
SAFE_BLOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_BUNDLE_HASH = re.compile(r"^[0-9A-Fa-f]{40}$")


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


STREAM_CHUNK_BYTES = 16 * 1024 * 1024


def send_file_stream(sock, path):
    """Sends a file too large to hold in memory (a 2 GB .pch): zlib chunks, each with a u32 size, then 0."""
    sent = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            packed = zlib.compress(chunk, 1)
            sock.sendall(struct.pack("<I", len(packed)))
            sock.sendall(packed)
            sent += len(packed) + 4
    sock.sendall(struct.pack("<I", 0))
    return sent


def recv_file_stream(sock, path):
    with open(path, "wb") as f:
        while True:
            size = struct.unpack("<I", recv_exact(sock, 4))[0]
            if size == 0:
                return
            if size > 2 * STREAM_CHUNK_BYTES:
                raise ValueError("stream chunk too large")
            f.write(zlib.decompress(recv_exact(sock, size)))


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


# --------------------------------------------------------------------------- one PC as developer and helper

# A PC that builds tells its own agent to take no work from other PCs: it holds the agent for
# HOLD_SECONDS and renews every HOLD_RENEW_SECONDS, so a build that dies frees the agent soon after.
HOLD_SECONDS = 30
HOLD_RENEW_SECONDS = 10
_machine = None


def machine_id():
    """The same short id for every DistriBuild program on this PC, so a build can tell its own agent apart.

    It is a hash: the agent's answer to discovery is unauthenticated, and the raw Windows id stays on the PC.
    """
    global _machine
    if _machine is None:
        raw = None
        if IS_WINDOWS:
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                    raw = winreg.QueryValueEx(key, "MachineGuid")[0]
            except OSError:
                pass
        else:
            for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    with open(path, encoding="ascii") as f:
                        raw = f.read().strip() or None
                    break
                except OSError:
                    pass
        _machine = hashlib.sha256(("distribuild:%s" % (raw or socket.gethostname())).encode("utf-8")).hexdigest()[:16]
    return _machine


STALE_AFTER_DAYS = 7


def contacts_path():
    """When each pinned helper address last answered, kept by xgConsole and the setup window."""
    return os.path.join(os.path.expanduser("~"), ".distribuild", "agent_contacts.json")


def load_agent_contacts():
    """{address: {"last_ok": epoch, "failing_since": epoch}}; either key may be missing."""
    try:
        with open(contacts_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_agent_contacts(results):
    """results: {address: answered}. Never fails a build: an unwritable file is simply skipped."""
    if not results:
        return
    data = load_agent_contacts()
    now = int(time.time())
    for address, answered in results.items():
        entry = data.get(address) if isinstance(data.get(address), dict) else {}
        if answered:
            entry["last_ok"] = now
            entry.pop("failing_since", None)
        else:
            entry.setdefault("failing_since", now)
        data[address] = entry
    path = contacts_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = "%s.%d.tmp" % (path, os.getpid())
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp, path)
    except OSError:
        pass


def contact_note(entry, now=None):
    """(Korean note, stale) for a pinned address that did not answer this time."""
    now = now or time.time()
    since = (entry or {}).get("failing_since")
    days = int((now - since) // 86400) if since else 0
    if since and days >= STALE_AFTER_DAYS:
        return "%d일째 연결 안 됨" % days, True
    last = (entry or {}).get("last_ok")
    if last:
        ago = int((now - last) // 86400)
        return "마지막 연결 %s" % ("오늘" if ago == 0 else "%d일 전" % ago), False
    return "", False


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


def decode_output(raw):
    """Tool output: UTF-8 when it is valid, otherwise the console code page cl.exe writes in."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("mbcs" if IS_WINDOWS else "latin-1", "replace")
    return text.replace("\r\n", "\n")


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


def compiler_id(path, refresh=False):
    """Stable identifier for a compiler build, computed the same way on client and agent so
    a job only goes to an agent with the exact same compiler. refresh asks again instead of
    trusting an earlier answer, for a compiler whose files may have been replaced."""
    resolved = shutil.which(path) or path
    with _id_lock:
        if resolved in _id_cache and not refresh:
            return _id_cache[resolved]
    cid = None
    family = compiler_family(resolved)
    name = os.path.basename(resolved).lower()
    try:
        if family == "msvc" and name in ("cl", "cl.exe"):
            p = subprocess.run([resolved], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30)
            # The banner is translated, and VSLANG has no effect without the English language pack:
            #   Microsoft (R) C/C++ Optimizing Compiler Version 19.44.35229 for x64
            #   Microsoft (R) C/C++ ...컴파일러 버전 19.44.35229(x64)
            m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)\s*(?:for\s+|\()(x64|x86|ARM64EC|ARM64|ARM)\b", decode_output(p.stderr + p.stdout))
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


# --------------------------------------------------------------------------- UE 4.26 shader jobs

def _read_fstring(data, pos):
    """FArchive << FString: i32 length including the terminator, negative for UTF-16."""
    size = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    if size == 0:
        return "", pos
    if size > 0:
        end = pos + size
        return data[pos:end - 1].decode("latin-1"), end
    end = pos + -size * 2
    return data[pos:end - 2].decode("utf-16-le"), end


def _write_fstring(text):
    try:
        raw = text.encode("ascii") + b"\0"
        return struct.pack("<i", len(raw)) + raw
    except UnicodeEncodeError:
        raw = text.encode("utf-16-le") + b"\0\0"
        return struct.pack("<i", -(len(raw) // 2)) + raw


def remap_shader_source_dirs(job, target_for_virtual_dir):
    """Rewrites the directory mappings at the head of a UE 4.26 ShaderCompileWorker input file.

    The layout is i32 InputVersion, TMap<FString, uint32> FormatVersions, TMap<FString, FString>
    Mappings (see FShaderCompileUtilities::DoWriteTasks). The engine writes the mappings as
    absolute paths of the machine that created the job; they must point at this machine's copy.
    Raises KeyError for a virtual directory the callable does not know.
    """
    pos = 4
    count = struct.unpack_from("<i", job, pos)[0]
    pos += 4
    for _ in range(count):
        _, pos = _read_fstring(job, pos)
        pos += 4
    head = job[:pos]
    count = struct.unpack_from("<i", job, pos)[0]
    pos += 4
    out = [head, struct.pack("<i", count)]
    for _ in range(count):
        key, pos = _read_fstring(job, pos)
        _, pos = _read_fstring(job, pos)
        out.append(_write_fstring(key))
        out.append(_write_fstring(target_for_virtual_dir(key)))
    out.append(job[pos:])
    return b"".join(out)


_include_prefixes = {}


def msvc_include_prefix(compiler):
    """The text before the path on a /showIncludes line, in whatever language this compiler speaks."""
    with _id_lock:
        if compiler in _include_prefixes:
            return _include_prefixes[compiler]
    prefix = "Note: including file:"
    probe = tempfile.mkdtemp(prefix="dbld_probe_")
    try:
        with open(os.path.join(probe, "dbld_probe.h"), "w") as f:
            f.write("\n")
        with open(os.path.join(probe, "dbld_probe.cpp"), "w") as f:
            f.write('#include "dbld_probe.h"\n')
        p = subprocess.run([compiler, "/nologo", "/showIncludes", "/Zs", "dbld_probe.cpp"], cwd=probe, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        for line in decode_output(p.stdout).splitlines():
            if line.lower().endswith("dbld_probe.h"):
                at = line.lower().find(os.path.normcase(probe)[:3].lower())
                prefix = line[:at].strip() if at > 0 else prefix
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    with _id_lock:
        _include_prefixes[compiler] = prefix
    return prefix


_log_file = None
_log_lock = threading.Lock()
LOG_LIMIT = 5 * 1024 * 1024


def set_log_file(path):
    """Also appends every log line to path. A file over LOG_LIMIT is moved to path.old first."""
    global _log_file
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        if os.path.getsize(path) > LOG_LIMIT:
            os.replace(path, path + ".old")
    except OSError:
        pass
    _log_file = open(path, "a", encoding="utf-8", buffering=1)


def log(msg):
    if _log_file is not None:
        with _log_lock:
            _log_file.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    if sys.stdout is None:  # started without a console
        return
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:  # compiler output can hold characters the console's code page lacks
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.write((msg + "\n").encode(encoding, "replace").decode(encoding))
        sys.stdout.flush()
    except OSError:  # the console it was started from is gone; the log file still has the line
        pass
