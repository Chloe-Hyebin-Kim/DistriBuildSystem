#!/usr/bin/env python3
"""DistriBuild setup: a Qt window that configures helper PCs and developer PCs.

    pip install PySide6-Essentials
    python setup_gui.py

build.bat turns it, with the agent and xgConsole, into dist\\DistriBuild.exe, which needs no Python
(see distribuild.py).

It edits the same files the command line tools read (agent.json, ~/.distribuild/client.json) and
runs the existing install_client.bat, so everything stays usable without the GUI.
"""

import base64
import ctypes
import html
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    # A windowed program has no console to share, so every console child would flash its own window.
    class _NoWindowPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen

try:
    from PySide6.QtCore import QEvent, QObject, QProcess, QProcessEnvironment, Qt, QTimer, Signal, Slot
    from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
    from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QDoubleSpinBox,
                                   QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                                   QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                                   QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                                   QTabWidget, QVBoxLayout, QWidget)
except ImportError:
    sys.exit("PySide6 is required:  pip install PySide6-Essentials")

from agent import detect_compilers, detect_engines, package_compilers
from common import (DEFAULT_PORT, DISCOVERY_PORT, STALE_AFTER_DAYS, compiler_family, compiler_id, contact_note,
                    decode_output, discover_agents, load_agent_contacts, machine_id, parse_agent_address,
                    record_agent_contacts, request)

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".distribuild")
DEFAULT_BIN = r"C:\DistriBuild\bin"
SERVICE = "Incredibuild Agent"
SHIM = "DistriBuildXgeShim"
FIREWALL_TCP = "DistriBuild Agent TCP"
FIREWALL_UDP = "DistriBuild Agent UDP"
AUTOSTART_TASK = "DistriBuild Agent"
# The one program: this window, the agent ("DistriBuild.exe agent") and, copied as xgConsole.exe, the
# build front end. See distribuild.py.
PROGRAM = "DistriBuild.exe"
PACKAGE_EXES = (PROGRAM,)
# What install_client.bat needs, so a PC set up from a package can be a developer PC too.
PACKAGE_DEVELOPER_FILES = ("install_client.bat", "uninstall_client.bat", os.path.join("service", "DistriBuildXgeShim.cs"))
# The manual for whoever gets the package, as .txt so it opens with a double click. A package carries it on.
MANUAL = "사용설명서.txt"
# A cross compiler also needs the host toolset's DLLs, so only x64-hosted x64 copies work on their own.
PACKAGEABLE = re.compile(r"^msvc-.*-x64$")
GOOD, BAD, NEUTRAL = "#2e7d32", "#c62828", "#808080"


# --------------------------------------------------------------------------- paths and files

def program_path():
    """DistriBuild.exe: beside this window (a package, or the exe itself), else built in the tools' dist."""
    tools = find_tools_dir()
    for folder in [APP_DIR] + ([os.path.join(tools, "dist")] if tools else []):
        if os.path.isfile(os.path.join(folder, PROGRAM)):
            return os.path.join(folder, PROGRAM)
    return None


def find_tools_dir():
    """The folder holding install_client.bat. A built exe sits in its dist subfolder."""
    for folder in (APP_DIR, os.path.dirname(APP_DIR)):
        if os.path.isfile(os.path.join(folder, "install_client.bat")):
            return folder
    return None


def helper_config_path():
    beside = os.path.join(APP_DIR, "agent.json")
    return beside if os.path.isfile(beside) else os.path.join(CONFIG_DIR, "agent.json")


def client_config_path():
    return os.environ.get("DISTRIBUILD_CONFIG") or os.path.join(CONFIG_DIR, "client.json")


def read_config(path, report):
    """The file's settings, or {} if it is missing. A damaged file is kept as .bak before it can be replaced."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data
    except (OSError, ValueError) as e:
        shutil.copyfile(path, path + ".bak")
        report("[!!] %s 파일을 읽지 못해 기본값을 표시합니다 (%s). 원본은 .bak으로 보관했습니다." % (path, e))
        return {}


def write_config(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(temp, path)


def whole_if_integer(value):
    return int(value) if float(value).is_integer() else value


def fresh_path():
    """PATH as a newly opened terminal would see it: installing changes the registry, not this process."""
    if not IS_WINDOWS:
        return os.environ.get("PATH", "")
    import winreg
    parts = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        except OSError:
            pass
    return ";".join(parts) or os.environ.get("PATH", "")


# --------------------------------------------------------------------------- elevation (Windows)

def run_elevated(command, hwnd=0, verb="runas"):
    """Runs a cmd.exe command with a UAC prompt. Returns (exit code, output); code None means the user declined."""
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", wintypes.LPVOID), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS, SEE_MASK_NOASYNC, SW_HIDE, INFINITE = 0x40, 0x100, 0, 0xFFFFFFFF
    COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE = 0x2, 0x4
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle, log_path = tempfile.mkstemp(prefix="distribuild_", suffix=".log")
    os.close(handle)
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.hwnd = hwnd or None
    info.lpVerb = verb
    info.lpFile = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "cmd.exe")
    # /s strips the outer quotes and keeps the rest exactly; the group makes the redirect cover every step.
    info.lpParameters = '/d /s /c "(%s) > "%s" 2>&1"' % (command, log_path)
    info.nShow = SW_HIDE
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error == 1223:  # ERROR_CANCELLED: "No" on the UAC prompt
                return None, ""
            raise ctypes.WinError(error)
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        kernel32.CloseHandle(info.hProcess)
        with open(log_path, "rb") as f:
            return code.value, decode_output(f.read())
    finally:
        ole32.CoUninitialize()
        try:
            os.remove(log_path)
        except OSError:
            pass


def firewall_open_command(tcp_port, udp_port):
    steps = []
    for name, protocol, port in ((FIREWALL_TCP, "TCP", tcp_port), (FIREWALL_UDP, "UDP", udp_port)):
        steps.append('(netsh advfirewall firewall delete rule name="%s" >nul 2>&1 & '
                     'netsh advfirewall firewall add rule name="%s" dir=in action=allow protocol=%s localport=%d '
                     'profile=domain,private)' % (name, name, protocol, port))
    return " && ".join(steps)


def firewall_close_command():
    return " & ".join('netsh advfirewall firewall delete rule name="%s"' % n for n in (FIREWALL_TCP, FIREWALL_UDP))


def firewall_is_open(tcp_port, udp_port):
    if not IS_WINDOWS:
        return None
    for name, port in ((FIREWALL_TCP, tcp_port), (FIREWALL_UDP, udp_port)):
        result = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=%s" % name],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0 or not re.search(r"\b%d\b" % port, decode_output(result.stdout)):
            return False
    return True


# --------------------------------------------------------------------------- start with Windows

def autostart_log_path(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "agent-autostart.log")


def autostart_command(config_path):
    """The agent command the startup task runs. Its --log path also identifies its processes."""
    command = agent_command(config_path)
    return command and command + ["--log", autostart_log_path(config_path)]


def autostart_task_xml(command, working_dir):
    """A Task Scheduler task that runs command as SYSTEM when the PC starts, before anyone signs in.

    The logon trigger only matters if the boot start was missed (a fast-startup boot can skip it);
    IgnoreNew keeps a second copy from starting. Below-normal priority keeps the helper usable.
    """
    from xml.sax.saxutils import escape
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>DistriBuild helper agent. Created by DistriBuild.</Description>
    <SecurityDescriptor>D:AI(A;;FA;;;BA)(A;;FA;;;SY)(A;;FR;;;AU)</SecurityDescriptor>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>%s</Arguments>
      <WorkingDirectory>%s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % (escape(command[0]), escape(subprocess.list2cmdline(command[1:])), escape(working_dir))


def autostart_query():
    """The registered startup task as {"command", "arguments", "log"}, or None if there is none."""
    if not IS_WINDOWS:
        return None
    result = subprocess.run(["schtasks", "/Query", "/TN", AUTOSTART_TASK, "/XML"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        return None
    import xml.etree.ElementTree as ET
    text = decode_output(result.stdout)
    try:
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text))
    except ET.ParseError:
        return {"command": "", "arguments": "", "log": None}
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = root.findtext("t:Actions/t:Exec/t:Command", "", ns)
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", "", ns)
    found = re.search(r'--log\s+(?:"([^"]+)"|(\S+))', arguments)
    return {"command": command, "arguments": arguments, "log": found and (found.group(1) or found.group(2))}


def autostart_matches(task, command):
    return bool(task) and (task["command"], task["arguments"]) == (command[0], subprocess.list2cmdline(command[1:]))


def stop_processes_command(markers):
    """A cmd step that ends every process whose command line holds one of markers, with its children.

    Ending the task alone would leave the agent's compiles, and the real process of a one-file exe, running.
    """
    quoted = ", ".join("'%s'" % m.replace("'", "''") for m in markers)
    script = ("$m = @(%s); Get-CimInstance Win32_Process | Where-Object { $c = $_.CommandLine; "
              "$c -and ($m | Where-Object { $c.Contains($_) }) } | ForEach-Object { "
              "taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }; exit 0" % quoted)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + encoded


def autostart_enable_command(xml_path, markers):
    return ('(%s & schtasks /Create /TN "%s" /XML "%s" /F) && schtasks /Run /TN "%s"'
            % (stop_processes_command(markers), AUTOSTART_TASK, xml_path, AUTOSTART_TASK))


def autostart_disable_command(markers):
    return '%s & schtasks /Delete /TN "%s" /F' % (stop_processes_command(markers), AUTOSTART_TASK)


# --------------------------------------------------------------------------- helper PC work

def agent_command(config_path):
    """How to start the agent, or None. From source this runs agent.py, so it can never be a stale exe."""
    if not FROZEN:
        return [sys.executable, "-u", os.path.join(APP_DIR, "agent.py"), "--config", config_path]
    exe = os.path.join(APP_DIR, PROGRAM)
    return [exe, "agent", "--config", config_path] if os.path.isfile(exe) else None


def probe_agent(host, port, token, timeout=3.0):
    """Asks one agent what it offers. Returns (info or None, Korean reason when None)."""
    try:
        reply, _ = request(host, port, {"type": "info", "token": token}, timeout=timeout)
    except (OSError, ValueError):
        return None, "연결 실패"
    if reply.get("type") == "info":
        return reply, ""
    return None, "토큰이 맞지 않음" if reply.get("reason") == "auth" else "응답 오류: %s" % reply.get("reason")


def scan_helper(compilers, engines, auto, port, discovery_port, token):
    """What an agent with these settings would offer, plus this PC's agent and firewall state."""
    rows = []
    seen = set()
    for path in compilers:
        cid = compiler_id(path) if os.path.isfile(path) and compiler_family(path) else None
        seen.add(cid)
        rows.append({"kind": "compiler", "id": cid, "path": path, "manual": True, "source": "직접 추가"})
    for root in engines:
        found = detect_engines([root], False)
        key = next(iter(found), None)
        seen.add(key)
        rows.append({"kind": "engine", "id": key, "path": root, "manual": True, "source": "직접 추가"})
    if auto:
        for kind, found in (("compiler", detect_compilers([], True)), ("engine", detect_engines([], True))):
            rows += [{"kind": kind, "id": key, "path": path, "manual": False, "source": "자동"}
                     for key, path in sorted(found.items()) if key not in seen]
            seen.update(found)
    # The agent finds these itself, as it runs, so they are shown but never written to the settings.
    for cl in package_compilers(APP_DIR):
        cid = compiler_id(cl, refresh=True)
        if cid not in seen or cid is None:
            seen.add(cid)
            rows.append({"kind": "compiler", "id": cid, "path": cl, "manual": False, "source": "패키지"})
    info, _ = probe_agent("127.0.0.1", port, token, timeout=0.5)
    listening = info is not None or port_in_use(port)
    return {"rows": rows, "listening": listening, "firewall": firewall_is_open(port, discovery_port),
            "autostart": autostart_query()}


def port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- developer PC work

def developer_status(bin_dir):
    status = {"real_incredibuild": None, "service": "unsupported", "on_path": False,
              "xgconsole_built": False, "installed": os.path.isfile(os.path.join(bin_dir, "xgConsole.exe"))}
    tools = find_tools_dir()
    if tools:
        status["xgconsole_built"] = program_path() is not None
    status["on_path"] = os.path.normcase(bin_dir) in [os.path.normcase(p.rstrip("\\/")) for p in fresh_path().split(";")]
    if IS_WINDOWS:
        import winreg
        for key in (r"SOFTWARE\Xoreax\IncrediBuild\Builder", r"SOFTWARE\WOW6432Node\Xoreax\IncrediBuild\Builder"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    status["real_incredibuild"] = winreg.QueryValueEx(k, "Folder")[0]
            except OSError:
                pass
        query = subprocess.run(["sc", "query", SERVICE], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if query.returncode != 0:
            status["service"] = "missing"
        else:
            config = subprocess.run(["sc", "qc", SERVICE], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            ours = SHIM.encode() in config.stdout
            running = re.search(rb":\s*4\s+RUNNING", query.stdout) is not None
            status["service"] = "foreign" if not ours else "running" if running else "stopped"
    return status


def local_compilers():
    """{compiler id: cl.exe} for this PC's compilers that can be copied to a helper on their own."""
    return {cid: path for cid, path in sorted(detect_compilers([], True).items()) if PACKAGEABLE.match(cid)}


def query_agents(addresses, discovery_port, token):
    rows, seen = [], set()
    # All at once: each dead address would otherwise add its own wait.
    with ThreadPoolExecutor(max_workers=max(1, min(32, len(addresses)))) as pool:
        answers = list(pool.map(lambda text: probe_agent(*parse_agent_address(text), token=token), addresses))
    record_agent_contacts({text: error != "연결 실패" for text, (_, error) in zip(addresses, answers)})
    contacts = load_agent_contacts()
    for text, (info, error) in zip(addresses, answers):
        note, stale = contact_note(contacts.get(text)) if error == "연결 실패" else ("", False)
        rows.append({"address": text, "pinned": True, "info": info, "error": error, "note": note, "stale": stale})
        if info:
            seen.add((info.get("name"), parse_agent_address(text)[1]))
    # An agent on this PC answers on its LAN address and on loopback; keep the one other PCs could use too.
    found = sorted(discover_agents(discovery_port), key=lambda item: item[0].startswith("127."))
    for host, info in found:
        port = int(info.get("port", DEFAULT_PORT))
        if (info.get("name"), port) in seen:
            continue
        seen.add((info.get("name"), port))
        checked, error = probe_agent(host, port, token)
        rows.append({"address": "%s:%d" % (host, port), "pinned": False, "info": checked or info, "error": error,
                     "note": "", "stale": False})
    return rows


TOKEN_REASONS = {
    "auth": "현재 토큰이 맞지 않음",
    "no_token": "헬퍼에 토큰이 없음 (그 PC에서 직접 설정)",
    "token_on_command_line": "--token으로 실행 중 (그 PC에서 직접 변경)",
    "no_config": "설정 파일 없이 실행 중 (그 PC에서 직접 변경)",
    "bad_token": "쓸 수 없는 토큰 (공백 없이 256자 이하)",
    "expected reserve": "헬퍼 프로그램이 이전 버전 (새 DistriBuild.exe 필요)",
}


def valid_token(token):
    return bool(token) and len(token) <= 256 and not any(ord(c) < 32 or c.isspace() for c in token)


def change_agent_token(host, port, old, new, timeout=5.0):
    """Asks one agent to switch to a new token. Returns (ok, Korean reason when not)."""
    try:
        reply, _ = request(host, port, {"type": "set_token", "token": old, "new_token": new}, timeout=timeout)
    except (OSError, ValueError):
        return False, "연결 실패"
    if reply.get("ok"):
        return True, ""
    reason = str(reply.get("reason", ""))
    return False, TOKEN_REASONS.get(reason) or ("저장 실패: " + reason[13:] if reason.startswith("save_failed") else reason)


def change_all_tokens(addresses, discovery_port, old, new):
    """Every helper this PC can find, each with (ok, reason) after asking it to switch to new."""
    results = []
    for row in query_agents(addresses, discovery_port, old):
        name = (row["info"] or {}).get("name", "")
        if not row["info"] or row["error"]:
            results.append({"address": row["address"], "name": name, "ok": False, "reason": row["error"] or "응답 없음"})
            continue
        host, port = parse_agent_address(row["address"])
        ok, reason = change_agent_token(host, port, old, new)
        results.append({"address": row["address"], "name": name, "ok": ok, "reason": reason})
    return results


def build_package(dest, compilers, report):
    """Fills dest with what a helper PC needs, and what makes it a developer PC too. dest may also be a
    helper's package folder in use.

    Returns {"missing": programs not found here, "busy": programs left as they were because they are running}.
    """
    os.makedirs(dest, exist_ok=True)
    tools = find_tools_dir()
    folders = [APP_DIR] + ([os.path.join(tools, "dist")] if tools else [])
    result = {"missing": [], "busy": []}
    # A new package gets its own settings file, so the helper keeps its settings and log beside the programs.
    # A helper's folder already in use is left as it is: it may keep its settings in the user's profile.
    config = os.path.join(dest, "agent.json")
    in_use = any(os.path.isfile(os.path.join(dest, name)) for name in (PROGRAM, "DistriBuildAgent.exe"))
    if not in_use and not os.path.isfile(config):
        with open(config, "w", encoding="utf-8") as f:
            f.write("{}\n")
        report("빈 설정 파일을 만들었습니다: agent.json")
    wanted = [(name, next((os.path.join(f, name) for f in folders if os.path.isfile(os.path.join(f, name))), None))
              for name in PACKAGE_EXES]
    wanted += [(name, os.path.join(tools, name) if tools and os.path.isfile(os.path.join(tools, name)) else None)
               for name in PACKAGE_DEVELOPER_FILES]
    manuals = [os.path.join(tools, MANUAL), os.path.join(tools, "..", "..", "docs", "사용설명서.md")] if tools else []
    wanted.append((MANUAL, next((m for m in manuals if os.path.isfile(m)), None)))
    for name, source in wanted:
        if source is None:
            result["missing"].append(name)
            continue
        target = os.path.join(dest, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
            continue
        report("복사 중: %s" % name)
        try:
            shutil.copy2(source, target)
        except PermissionError:
            if not os.path.isfile(target):
                raise
            result["busy"].append(name)
            report("%s: 실행 중이라 기존 파일을 그대로 둡니다." % name)
    for cid, cl in compilers.items():
        target = os.path.join(dest, "msvc", cid)
        if os.path.isfile(os.path.join(target, "cl.exe")) and compiler_id(os.path.join(target, "cl.exe"), refresh=True) == cid:
            report("이미 있음: %s" % cid)
            continue
        report("복사 중: %s" % cid)
        # Copied under another name first, so a running agent never sees a half-copied compiler folder.
        staging = os.path.join(dest, "msvc", ".copying-" + cid)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(os.path.dirname(cl), staging)
        copied = compiler_id(os.path.join(staging, "cl.exe"), refresh=True)
        stop_vctip(staging)
        if copied != cid:
            raise RuntimeError("복사한 컴파일러의 ID가 다릅니다: %s, 원본 %s" % (copied, cid))
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    return result


def stop_vctip(folder):
    """cl.exe leaves its telemetry helper running, which keeps the folder it ran from locked."""
    if IS_WINDOWS:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-Process vctip -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%s*' } | "
                        "Stop-Process -Force" % folder.replace("'", "''")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)


# --------------------------------------------------------------------------- Qt plumbing

class Bus(QObject):
    """Runs work on background threads and delivers results on the GUI thread."""
    finished = Signal(object, object, object)
    line = Signal(str)

    def __init__(self):
        super().__init__()
        self.finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def start(self, work, done=None, button=None):
        if button:
            button.setEnabled(False)

        def body():
            try:
                result, error = work(), None
            except Exception as e:  # shown to the user; a worker thread has nowhere else to report it
                result, error = None, str(e) or type(e).__name__
            self.finished.emit((done, button), result, error)

        threading.Thread(target=body, daemon=True).start()

    @Slot(object, object, object)
    def _deliver(self, context, result, error):
        done, button = context
        if button:
            button.setEnabled(True)
        if error is not None:
            self.line.emit("[!!] 오류: %s" % error)
        elif done:
            done(result)


class LoggedProcess(QProcess):
    """A child process whose combined output arrives one whole line at a time."""

    def __init__(self, parent, on_line):
        super().__init__(parent)
        self.on_line = on_line
        self.pending = b""
        self.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.readyReadStandardOutput.connect(self._read)
        self.finished.connect(self._flush)

    def _read(self):
        self.pending += bytes(self.readAllStandardOutput())
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        for raw in lines:
            self.on_line(decode_output(raw).rstrip("\r"))

    def _flush(self):
        self._read()
        if self.pending:
            self.on_line(decode_output(self.pending).rstrip("\r"))
            self.pending = b""


class WheelGuard(QObject):
    """Stops the mouse wheel changing a number box when the user is only scrolling the page."""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and not watched.hasFocus():
            event.ignore()
            return True
        return False


def status_html(state, text):
    color = {True: GOOD, False: BAD, None: NEUTRAL}[state]
    return '<span style="color:%s">●</span> %s' % (color, html.escape(text))


def make_table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.horizontalHeader().setStretchLastSection(True)
    return table


def add_table_row(table, values, data=None, color=None, tooltip=""):
    row = table.rowCount()
    table.insertRow(row)
    for column, value in enumerate(values):
        item = QTableWidgetItem(str(value))
        if tooltip:
            item.setToolTip(tooltip)
        if color and column == color[0]:
            item.setForeground(QColor(color[1]))
        if column == 0:
            item.setData(Qt.ItemDataRole.UserRole, data)
        table.setItem(row, column, item)


def selected_data(table):
    rows = sorted({i.row() for i in table.selectedItems()})
    return [table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]


def button_row(*widgets):
    row = QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch(1)
    return row


def browse_row(edit, button):
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    row.addWidget(button)
    return row


class Page(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window_ = window
        self.bus = window.bus
        self.log = window.append_log

    def int_box(self, low, high, value, special="", suffix=""):
        box = QSpinBox()
        box.setRange(low, high)
        return self._number_box(box, value, special, suffix)

    def float_box(self, low, high, value, suffix=""):
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(1)
        return self._number_box(box, value, "", suffix)

    def _number_box(self, box, value, special, suffix):
        box.setValue(value)
        box.setSpecialValueText(special)
        box.setSuffix(suffix)
        box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        box.installEventFilter(self.window_.wheel_guard)
        return box

    def elevate(self, command, button, title, done=None):
        """Runs a command behind a UAC prompt and reports how it went."""
        hwnd = int(self.window_.winId())
        self.log("%s: 관리자 권한을 요청합니다..." % title)

        def finished(result):
            code, output = result
            for line in output.splitlines():
                if line.strip():
                    self.log(line)
            if code is None:
                self.log("[!!] %s: 관리자 권한 요청이 취소되었습니다." % title)
            elif code == 0:
                self.log("[ok] %s: 완료" % title)
            else:
                self.log("[!!] %s: 실패 (종료 코드 %d)" % (title, code))
            if done:
                done()

        self.bus.start(lambda: run_elevated(command, hwnd), finished, button)


def scrollable(content):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(content)
    return area


# --------------------------------------------------------------------------- helper PC page

class HelperPage(Page):
    def __init__(self, window):
        super().__init__(window)
        self.config_path = helper_config_path()
        self.loaded = read_config(self.config_path, self.log)
        self.compilers = list(self.loaded.get("compilers", []))
        self.engines = list(self.loaded.get("engines", []))
        self.process = None
        self.listening = False
        self.autostart = None
        self.firewall_state = None
        self.pick_up_packaged_compilers()
        self.build_ui()
        self.scan()

    def pick_up_packaged_compilers(self):
        """The agent finds a package's compilers itself, so entries an older version saved are dropped."""
        packaged = {os.path.normcase(p) for p in package_compilers(APP_DIR)}
        kept = [p for p in self.compilers if os.path.normcase(os.path.abspath(p)) not in packaged]
        if len(kept) != len(self.compilers):
            self.log("패키지 폴더의 컴파일러는 에이전트가 직접 찾으므로 설정 파일에서 뺍니다 (저장하면 적용).")
            self.compilers = kept

    def build_ui(self):
        c = self.loaded
        self.name = QLineEdit(c.get("name", ""))
        self.name.setPlaceholderText(socket.gethostname())
        self.slots = self.int_box(0, 1024, int(c.get("slots") or 0), "자동 (CPU %d개)" % (os.cpu_count() or 4))
        self.port = self.int_box(1, 65535, int(c.get("port", DEFAULT_PORT)))
        self.discovery_port = self.int_box(1, 65535, int(c.get("discovery_port", DISCOVERY_PORT)))
        self.token = QLineEdit(c.get("token", ""))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("비워 두면 인증하지 않음")
        self.token.setToolTip("개발자 PC에 같은 토큰을 입력해야 합니다.")
        self.work_dir = QLineEdit(c.get("work_dir", ""))
        self.work_dir.setPlaceholderText("시스템 임시 폴더")
        browse = QPushButton("찾아보기")
        browse.clicked.connect(self.choose_work_dir)
        self.pch_gb = self.float_box(1, 4096, float(c.get("pch_cache_gb", 40)), " GB")
        self.job_timeout = self.float_box(10, 86400, float(c.get("job_timeout", 600)), " 초")

        form = QFormLayout()
        form.addRow("PC 이름", self.name)
        form.addRow("동시 작업 수", self.slots)
        form.addRow("TCP 포트", self.port)
        form.addRow("UDP 검색 포트", self.discovery_port)
        form.addRow("토큰", self.token)
        form.addRow("작업 폴더", browse_row(self.work_dir, browse))
        form.addRow("PCH 캐시 크기", self.pch_gb)
        form.addRow("작업 제한 시간", self.job_timeout)
        settings = QGroupBox("에이전트 설정")
        settings.setLayout(form)

        self.agent_status = QLabel()
        self.start_button = QPushButton("에이전트 시작")
        self.stop_button = QPushButton("중지")
        self.start_button.clicked.connect(self.start_agent)
        self.stop_button.clicked.connect(self.stop_agent)
        run_layout = QVBoxLayout()
        run_layout.addWidget(self.agent_status)
        note = QLabel("이 창을 닫으면 여기서 시작한 에이전트도 중지됩니다. 자동 시작된 에이전트는 계속 실행됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: %s" % NEUTRAL)
        run_layout.addWidget(note)
        run_layout.addLayout(button_row(self.start_button, self.stop_button))
        run_group = QGroupBox("실행")
        run_group.setLayout(run_layout)

        self.autostart_status = QLabel()
        self.autostart_status.setWordWrap(True)
        self.autostart_on_button = QPushButton("자동 시작 켜기 (관리자)")
        self.autostart_off_button = QPushButton("자동 시작 끄기 (관리자)")
        self.autostart_on_button.clicked.connect(self.enable_autostart)
        self.autostart_off_button.clicked.connect(self.disable_autostart)
        autostart_layout = QVBoxLayout()
        autostart_layout.addWidget(self.autostart_status)
        note = QLabel("PC가 켜지면 로그인하지 않아도 SYSTEM 계정으로 에이전트가 시작됩니다. "
                      "사용자 환경 변수(UE_ROOT, 사용자 PATH)로만 찾은 엔진이나 컴파일러는 직접 추가해 두세요.")
        note.setWordWrap(True)
        note.setStyleSheet("color: %s" % NEUTRAL)
        autostart_layout.addWidget(note)
        autostart_layout.addLayout(button_row(self.autostart_on_button, self.autostart_off_button))
        autostart_group = QGroupBox("PC를 켜면 자동 시작")
        autostart_group.setLayout(autostart_layout)

        self.firewall_status = QLabel()
        self.firewall_open_button = QPushButton("방화벽 열기 (관리자)")
        self.firewall_close_button = QPushButton("규칙 제거 (관리자)")
        self.firewall_open_button.clicked.connect(self.open_firewall)
        self.firewall_close_button.clicked.connect(self.close_firewall)
        firewall_layout = QVBoxLayout()
        firewall_layout.addWidget(self.firewall_status)
        note = QLabel("도메인, 개인 네트워크에서만 엽니다. 공용 네트워크로 설정된 PC는 네트워크 종류를 바꿔 주세요.")
        note.setWordWrap(True)
        note.setStyleSheet("color: %s" % NEUTRAL)
        firewall_layout.addWidget(note)
        firewall_layout.addLayout(button_row(self.firewall_open_button, self.firewall_close_button))
        firewall_group = QGroupBox("방화벽")
        firewall_group.setLayout(firewall_layout)

        self.auto_detect = QCheckBox("이 PC에 설치된 컴파일러와 엔진을 자동으로 찾기")
        self.auto_detect.setChecked(bool(c.get("auto_detect", True)))
        self.auto_detect.toggled.connect(self.scan)
        self.offer_table = make_table(["종류", "식별자", "경로", "추가 방식"])
        self.offer_table.setColumnWidth(0, 70)
        self.offer_table.setColumnWidth(1, 170)
        self.offer_table.setColumnWidth(2, 290)
        self.rescan_button = QPushButton("다시 검색")
        add_compiler = QPushButton("cl.exe 추가")
        add_engine = QPushButton("엔진 폴더 추가")
        remove = QPushButton("선택 항목 제거")
        self.rescan_button.clicked.connect(self.scan)
        add_compiler.clicked.connect(self.add_compiler)
        add_engine.clicked.connect(self.add_engine)
        remove.clicked.connect(self.remove_selected)
        offer_layout = QVBoxLayout()
        offer_layout.addWidget(self.auto_detect)
        offer_layout.addWidget(self.offer_table, 1)
        offer_layout.addLayout(button_row(self.rescan_button, add_compiler, add_engine, remove))
        hint = QLabel("C++ 분산에는 개발자 PC와 빌드 번호까지 같은 컴파일러가, 셰이더 분산에는 같은 버전의 엔진이 필요합니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s" % NEUTRAL)
        offer_layout.addWidget(hint)
        offer_group = QGroupBox("이 PC가 제공하는 컴파일러와 엔진")
        offer_group.setLayout(offer_layout)

        left = QVBoxLayout()
        left.addWidget(run_group)
        left.addWidget(autostart_group)
        left.addWidget(settings)
        left.addWidget(firewall_group)
        left.addStretch(1)
        columns = QHBoxLayout()
        columns.addLayout(left, 2)
        columns.addWidget(offer_group, 3)
        content = QWidget()
        content.setLayout(columns)

        save = QPushButton("저장")
        save.clicked.connect(self.save)
        footer = QHBoxLayout()
        self.path_label = QLabel("설정 파일: %s" % self.config_path)
        self.path_label.setStyleSheet("color: %s" % NEUTRAL)
        footer.addWidget(self.path_label, 1)
        footer.addWidget(save)
        outer = QVBoxLayout(self)
        outer.addWidget(scrollable(content), 1)
        outer.addLayout(footer)
        self.update_run_state()

    def current_config(self):
        data = dict(self.loaded)
        for key, value in (("name", self.name.text().strip()), ("token", self.token.text()),
                           ("work_dir", self.work_dir.text().strip()), ("slots", self.slots.value())):
            if value:
                data[key] = value
            else:
                data.pop(key, None)
        data.update(port=self.port.value(), discovery_port=self.discovery_port.value(),
                    pch_cache_gb=whole_if_integer(self.pch_gb.value()),
                    job_timeout=whole_if_integer(self.job_timeout.value()),
                    auto_detect=self.auto_detect.isChecked(), compilers=self.compilers, engines=self.engines)
        return data

    def save(self):
        # A developer PC may have changed the token remotely since this window read the file.
        on_disk = read_config(self.config_path, self.log).get("token", "")
        if on_disk != self.loaded.get("token", "") and self.token.text() == self.loaded.get("token", ""):
            self.token.setText(on_disk)
            self.loaded["token"] = on_disk
            self.log("토큰이 다른 곳(개발자 PC의 일괄 변경)에서 바뀌어 새 토큰을 유지합니다.")
        data = self.current_config()
        write_config(self.config_path, data)
        self.loaded = data
        self.log("[ok] 저장했습니다: %s" % self.config_path)
        if self.is_running():
            self.log("실행 중인 에이전트에는 다시 시작해야 적용됩니다.")
        if self.autostart:
            self.log("자동 시작 에이전트에 적용하려면 '설정 적용해 다시 시작'을 누르세요.")

    def scan(self):
        compilers, engines, auto = list(self.compilers), list(self.engines), self.auto_detect.isChecked()
        port, discovery_port, token = self.port.value(), self.discovery_port.value(), self.token.text()
        self.bus.start(lambda: scan_helper(compilers, engines, auto, port, discovery_port, token),
                       self.show_scan, self.rescan_button)

    def show_scan(self, result):
        table = self.offer_table
        table.setRowCount(0)
        labels = {"compiler": "컴파일러", "engine": "엔진"}
        for row in result["rows"]:
            identifier = row["id"] or "인식 실패"
            add_table_row(table, [labels[row["kind"]], identifier, row["path"], row["source"]],
                          row, color=None if row["id"] else (1, BAD), tooltip=row["path"])
        self.listening = result["listening"]
        self.autostart = result["autostart"]
        self.update_run_state()
        state = self.firewall_state = result["firewall"]
        text = {True: "TCP %d, UDP %d 허용됨", False: "TCP %d, UDP %d 규칙 없음 (다른 PC에서 접속하려면 필요)",
                None: "TCP %d, UDP %d: 이 운영체제에서는 직접 확인하세요"}[state]
        self.firewall_status.setText(status_html(state, text % (self.port.value(), self.discovery_port.value())))
        self.window_.state_changed()

    def is_running(self):
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def update_run_state(self):
        running = self.is_running()
        if running:
            self.agent_status.setText(status_html(True, "실행 중"))
        elif self.listening and self.autostart:
            self.agent_status.setText(status_html(True, "자동 시작으로 실행 중"))
        elif self.listening:
            self.agent_status.setText(status_html(True, "이 PC에서 이미 실행 중 (다른 프로그램에서 시작함)"))
        else:
            self.agent_status.setText(status_html(False, "중지됨"))
        self.start_button.setEnabled(not running and not self.listening)
        self.stop_button.setEnabled(running)
        self.update_autostart_state()

    def update_autostart_state(self):
        task, command = self.autostart, autostart_command(self.config_path)
        if task is None:
            self.autostart_status.setText(status_html(False, "꺼짐"))
            self.autostart_on_button.setText("자동 시작 켜기 (관리자)")
        elif command and autostart_matches(task, command):
            self.autostart_status.setText(status_html(True, "켜짐 (로그: %s)" % task["log"]))
            self.autostart_on_button.setText("설정 적용해 다시 시작 (관리자)")
        else:
            self.autostart_status.setText(status_html(
                False, "켜져 있지만 다른 위치의 에이전트를 실행합니다: %s %s" % (task["command"], task["arguments"])))
            self.autostart_on_button.setText("이 위치로 다시 등록 (관리자)")
        if command is None:
            self.autostart_on_button.setEnabled(False)
            self.autostart_on_button.setToolTip("이 프로그램의 파일 이름이 DistriBuild.exe여야 합니다.")
        self.autostart_off_button.setEnabled(task is not None)

    def autostart_markers(self):
        markers = {autostart_log_path(self.config_path)}
        if self.autostart and self.autostart["log"]:
            markers.add(self.autostart["log"])
        return sorted(markers)

    def prepare_autostart(self):
        """Saves the settings and writes the task file. Returns (UAC command, task file), or None after logging why not."""
        command = autostart_command(self.config_path)
        if command is None:
            self.log("[!!] 이 프로그램의 파일 이름이 DistriBuild.exe여야 에이전트를 실행할 수 있습니다.")
            return None
        if not self.autostart and not self.is_running() and port_in_use(self.port.value()):
            self.log("[!!] 포트 %d을 다른 프로그램이 쓰고 있습니다. 그 프로그램을 끄거나 포트를 바꾼 뒤 다시 누르세요."
                     % self.port.value())
            return None
        self.save()
        if self.is_running():
            self.log("이 창에서 시작한 에이전트를 멈추고 자동 시작 에이전트로 바꿉니다.")
            self.stop_agent()
        handle, xml_path = tempfile.mkstemp(prefix="distribuild_task_", suffix=".xml")
        with os.fdopen(handle, "w", encoding="utf-16") as f:
            f.write(autostart_task_xml(command, APP_DIR))
        return autostart_enable_command(xml_path, self.autostart_markers()), xml_path

    def autostart_started(self, xml_path):
        try:
            os.remove(xml_path)
        except OSError:
            pass
        # The agent needs a moment to look for compilers before it listens.
        QTimer.singleShot(4000, self.scan)

    def enable_autostart(self):
        prepared = self.prepare_autostart()
        if prepared:
            command, xml_path = prepared
            self.elevate(command, self.autostart_on_button, "자동 시작 켜기", lambda: self.autostart_started(xml_path))

    def disable_autostart(self):
        self.elevate(autostart_disable_command(self.autostart_markers()),
                     self.autostart_off_button, "자동 시작 끄기", self.scan)

    def start_agent(self):
        if self.is_running():
            return
        if port_in_use(self.port.value()):
            self.log("[!!] 포트 %d을 이미 다른 프로그램이 쓰고 있어 시작하지 않았습니다." % self.port.value())
            self.listening = True
            self.update_run_state()
            return
        command = agent_command(self.config_path)
        if command is None:
            self.log("[!!] 이 프로그램의 파일 이름이 DistriBuild.exe여야 에이전트를 실행할 수 있습니다.")
            return
        self.save()
        self.process = LoggedProcess(self, self.log)
        self.process.setWorkingDirectory(APP_DIR)
        self.process.finished.connect(self.agent_finished)
        self.process.errorOccurred.connect(self.agent_error)
        self.process.start(command[0], command[1:])
        self.update_run_state()

    def agent_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.log("[!!] 에이전트를 시작하지 못했습니다: %s" % self.process.errorString())
            self.process = None
            self.update_run_state()

    def agent_finished(self, code, _status):
        self.log("에이전트가 종료되었습니다 (종료 코드 %d)." % code)
        self.listening = False
        self.update_run_state()

    def stop_agent(self):
        """Ends the agent and any compile it is still running."""
        if not self.is_running():
            return
        pid = self.process.processId()
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.process.kill()
        self.process.waitForFinished(5000)
        self.update_run_state()

    def choose_work_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "작업 폴더 선택", self.work_dir.text())
        if folder:
            self.work_dir.setText(os.path.normpath(folder))

    def add_compiler(self):
        path, _ = QFileDialog.getOpenFileName(self, "cl.exe 선택", "", "cl.exe (cl.exe)")
        if path and os.path.normpath(path) not in self.compilers:
            self.compilers.append(os.path.normpath(path))
            self.scan()

    def add_engine(self):
        folder = QFileDialog.getExistingDirectory(self, "엔진 루트 폴더 선택 (Engine 폴더의 상위)")
        if folder and os.path.normpath(folder) not in self.engines:
            self.engines.append(os.path.normpath(folder))
            self.scan()

    def remove_selected(self):
        chosen = selected_data(self.offer_table)
        if any(row["source"] == "자동" for row in chosen):
            self.log("자동으로 찾은 항목은 제거할 수 없습니다. 자동 찾기를 끄면 목록에서 빠집니다.")
        if any(row["source"] == "패키지" for row in chosen):
            self.log("패키지 컴파일러는 패키지 폴더(msvc 아래)에서 그 폴더를 지우면 빠집니다.")
        for row in chosen:
            if row["manual"]:
                (self.compilers if row["kind"] == "compiler" else self.engines).remove(row["path"])
        self.scan()

    def open_firewall(self):
        self.elevate(firewall_open_command(self.port.value(), self.discovery_port.value()),
                     self.firewall_open_button, "방화벽 열기", self.scan)

    def close_firewall(self):
        self.elevate(firewall_close_command(), self.firewall_close_button, "방화벽 규칙 제거", self.scan)


# --------------------------------------------------------------------------- developer PC page

class DeveloperPage(Page):
    def __init__(self, window):
        super().__init__(window)
        self.config_path = client_config_path()
        self.loaded = read_config(self.config_path, self.log)
        self.addresses = list(self.loaded.get("agents", []))
        self.compiler_paths = {}
        self.agent_rows = []
        self.token_box = None
        self.status = None
        self.tools = find_tools_dir()
        self.build_ui()
        self.refresh_status()
        self.bus.start(local_compilers, self.show_compilers)

    def build_ui(self):
        c = self.loaded
        self.state_labels = {key: QLabel() for key in ("built", "incredibuild", "service", "path")}
        state_layout = QVBoxLayout()
        for label in self.state_labels.values():
            label.setWordWrap(True)
            state_layout.addWidget(label)
        self.bin_dir = QLineEdit(DEFAULT_BIN)
        self.bin_dir.editingFinished.connect(self.refresh_status)
        form = QFormLayout()
        form.addRow("설치 폴더", self.bin_dir)
        state_layout.addLayout(form)
        self.build_button = QPushButton("프로그램 빌드")
        self.install_button = QPushButton("설치 / 업데이트 (관리자)")
        self.uninstall_button = QPushButton("제거 (관리자)")
        self.build_button.clicked.connect(self.build_program)
        self.install_button.clicked.connect(self.install)
        self.uninstall_button.clicked.connect(self.uninstall)
        state_layout.addLayout(button_row(self.build_button, self.install_button, self.uninstall_button))
        self.doctor_button = QPushButton("진단 실행")
        self.refresh_button = QPushButton("상태 새로고침")
        self.doctor_button.clicked.connect(self.run_doctor)
        self.refresh_button.clicked.connect(self.refresh_status)
        state_layout.addLayout(button_row(self.doctor_button, self.refresh_button))
        state_group = QGroupBox("UnrealBuildTool 연결")
        state_group.setLayout(state_layout)

        self.token = QLineEdit(c.get("token", ""))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("헬퍼 PC에 설정한 토큰")
        self.max_local = self.int_box(0, 1024, int(c.get("max_local") or 0), "자동 (CPU %d개)" % (os.cpu_count() or 4))
        self.max_preprocess = self.int_box(0, 1024, int(c.get("max_preprocess") or 0), "자동")
        self.discovery_port = self.int_box(1, 65535, int(c.get("discovery_port", DISCOVERY_PORT)))
        self.connect_timeout = self.float_box(0.5, 120, float(c.get("connect_timeout", 3)), " 초")
        self.job_timeout = self.float_box(10, 86400, float(c.get("job_timeout", 600)), " 초")
        self.discovery = QCheckBox("빌드할 때 네트워크에서 헬퍼 PC 자동 검색")
        self.discovery.setChecked(bool(c.get("discovery", True)))
        self.local_first = QCheckBox("내 PC 코어를 먼저 사용")
        self.local_first.setChecked(bool(c.get("local_first", True)))
        self.share_pch = QCheckBox("PCH를 헬퍼 PC와 공유 (권장)")
        self.share_pch.setChecked(bool(c.get("share_pch", True)))
        self.retry_local = QCheckBox("원격 실패 시 로컬에서 다시 실행")
        self.retry_local.setChecked(bool(c.get("retry_remote_failures_locally", True)))
        self.verbose = QCheckBox("로컬로 돌린 이유를 빌드 로그에 출력")
        self.verbose.setChecked(bool(c.get("verbose", False)))
        self.token_button = QPushButton("모든 헬퍼 토큰 바꾸기")
        self.token_button.setToolTip("찾을 수 있는 헬퍼의 토큰을 한 번에 바꾸고 이 PC에도 저장합니다.")
        self.token_button.clicked.connect(lambda: self.change_tokens())
        options = QFormLayout()
        options.addRow("토큰", browse_row(self.token, self.token_button))
        options.addRow("로컬 동시 작업 수", self.max_local)
        options.addRow("동시 전처리 수", self.max_preprocess)
        options.addRow("UDP 검색 포트", self.discovery_port)
        options.addRow("연결 제한 시간", self.connect_timeout)
        options.addRow("작업 제한 시간", self.job_timeout)
        for box in (self.discovery, self.local_first, self.share_pch, self.retry_local, self.verbose):
            options.addRow(box)
        options_group = QGroupBox("빌드 설정")
        options_group.setLayout(options)

        self.agent_table = make_table(["주소", "이름", "슬롯", "상태", "맞는 컴파일러", "구분"])
        for column, width in enumerate((140, 110, 45, 80, 170)):
            self.agent_table.setColumnWidth(column, width)
        self.address = QLineEdit()
        self.address.setPlaceholderText("IP 또는 PC 이름 (예: 192.168.0.21:31890)")
        self.address.returnPressed.connect(self.add_address)
        add = QPushButton("주소 추가")
        add.clicked.connect(self.add_address)
        self.find_button = QPushButton("헬퍼 PC 찾기")
        pin = QPushButton("선택 고정")
        remove = QPushButton("선택 제거")
        self.remove_stale_button = QPushButton("오래 연결 안 된 주소 제거")
        self.remove_stale_button.setToolTip("고정한 주소 중 %d일 넘게 응답이 없는 것을 뺍니다. 빌드마다 연결을 기다리지 않게 됩니다."
                                            % STALE_AFTER_DAYS)
        self.remove_stale_button.setEnabled(False)
        self.find_button.clicked.connect(self.find_agents)
        pin.clicked.connect(self.pin_selected)
        remove.clicked.connect(self.remove_selected)
        self.remove_stale_button.clicked.connect(self.remove_stale)
        agents_layout = QVBoxLayout()
        agents_layout.addWidget(self.agent_table, 1)
        agents_layout.addLayout(browse_row(self.address, add))
        agents_layout.addLayout(button_row(self.find_button, pin, remove, self.remove_stale_button))
        agents_group = QGroupBox("헬퍼 PC")
        agents_group.setLayout(agents_layout)

        self.package_list = QListWidget()
        self.package_list.setMaximumHeight(90)
        self.package_dest = QLineEdit()
        self.package_dest.setPlaceholderText("USB 또는 공유 폴더")
        browse = QPushButton("찾아보기")
        browse.clicked.connect(self.choose_package_dest)
        self.package_button = QPushButton("패키지 만들기")
        self.package_button.clicked.connect(self.make_package)
        package_layout = QVBoxLayout()
        package_layout.addWidget(QLabel("넣을 컴파일러 (빌드 번호까지 같은 복사본이 들어갑니다)"))
        package_layout.addWidget(self.package_list)
        package_layout.addLayout(browse_row(self.package_dest, browse))
        package_layout.addLayout(button_row(self.package_button))
        hint = QLabel("새 헬퍼: 만든 폴더의 내용을 헬퍼 PC의 C:\\DistriBuild에 복사하고 DistriBuild.exe를 실행하세요.\n"
                      "이미 쓰는 헬퍼에 컴파일러 추가: 그 헬퍼의 패키지 폴더(예: \\\\헬퍼PC\\c$\\DistriBuild)를 바로 고르세요. "
                      "실행 중인 에이전트가 1분 안에 새 컴파일러를 찾아 씁니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s" % NEUTRAL)
        package_layout.addWidget(hint)
        package_group = QGroupBox("헬퍼 PC용 패키지")
        package_group.setLayout(package_layout)

        left = QVBoxLayout()
        left.addWidget(state_group)
        left.addWidget(options_group)
        left.addStretch(1)
        right = QVBoxLayout()
        right.addWidget(agents_group, 1)
        right.addWidget(package_group)
        columns = QHBoxLayout()
        columns.addLayout(left, 2)
        columns.addLayout(right, 3)
        content = QWidget()
        content.setLayout(columns)

        save = QPushButton("저장")
        save.clicked.connect(self.save)
        footer = QHBoxLayout()
        path_label = QLabel("설정 파일: %s" % self.config_path)
        path_label.setStyleSheet("color: %s" % NEUTRAL)
        footer.addWidget(path_label, 1)
        footer.addWidget(save)
        outer = QVBoxLayout(self)
        outer.addWidget(scrollable(content), 1)
        outer.addLayout(footer)

    def current_config(self):
        data = dict(self.loaded)
        data.update(agents=self.addresses, discovery=self.discovery.isChecked(), discovery_port=self.discovery_port.value(),
                    token=self.token.text(), max_local=self.max_local.value(), max_preprocess=self.max_preprocess.value(),
                    local_first=self.local_first.isChecked(), share_pch=self.share_pch.isChecked(),
                    retry_remote_failures_locally=self.retry_local.isChecked(),
                    connect_timeout=whole_if_integer(self.connect_timeout.value()),
                    job_timeout=whole_if_integer(self.job_timeout.value()), verbose=self.verbose.isChecked())
        return data

    def save(self):
        data = self.current_config()
        write_config(self.config_path, data)
        self.loaded = data
        self.log("[ok] 저장했습니다: %s" % self.config_path)

    def change_tokens(self, new=None):
        """Switches every helper this PC can reach to a new token, then this PC. new=None asks for it."""
        old = self.token.text()
        if not old:
            self.log("[!!] 이 PC의 토큰이 비어 있습니다. 토큰이 없는 헬퍼는 원격으로 바꿀 수 없으니 헬퍼마다 먼저 설정하세요.")
            return
        if new is None:
            new, ok = QInputDialog.getText(self, "모든 헬퍼 토큰 바꾸기",
                                           "새 토큰 (비워 두면 무작위로 만듭니다)\n지금 토큰을 아는 헬퍼만 바뀝니다. 꺼져 있는 헬퍼는 나중에 직접 바꿔야 합니다.")
            if not ok:
                return
            new = new.strip() or secrets.token_urlsafe(18)
        if not valid_token(new):
            self.log("[!!] " + TOKEN_REASONS["bad_token"])
            return
        if new == old:
            self.log("새 토큰이 지금 토큰과 같습니다.")
            return
        addresses, port = list(self.addresses), self.discovery_port.value()
        self.log("헬퍼 토큰을 바꾸는 중입니다...")
        self.bus.start(lambda: change_all_tokens(addresses, port, old, new),
                       lambda results: self.tokens_changed(results, new), self.token_button)

    def tokens_changed(self, results, new):
        done = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
        for r in results:
            label = "%s (%s)" % (r["name"] or "?", r["address"])
            self.log("[ok] %s: 바꿈" % label if r["ok"] else "[!!] %s: %s" % (label, r["reason"]))
        if not done:
            self.log("[!!] 토큰을 바꾼 헬퍼가 없어 이 PC의 토큰도 그대로 둡니다.")
            return
        self.token.setText(new)
        self.save()
        text = "헬퍼 %d대 중 %d대의 토큰을 바꾸고 이 PC에도 저장했습니다.\n\n새 토큰: %s\n\n다른 개발자 PC의 개발자 탭에도 이 토큰을 넣고 저장하세요." % (
            len(results), len(done), new)
        if failed:
            text += "\n\n바꾸지 못한 헬퍼는 그 PC의 헬퍼 탭에서 직접 바꾸세요:\n" + "\n".join(
                "%s (%s): %s" % (r["name"] or "?", r["address"], r["reason"]) for r in failed)
        self.token_box = QMessageBox(QMessageBox.Icon.Information, "토큰을 바꿨습니다", text, parent=self)
        self.token_box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.token_box.open()
        self.find_agents()

    def install_folder(self):
        return self.bin_dir.text().strip().rstrip("\\/")

    def refresh_status(self):
        folder = self.install_folder()
        self.bus.start(lambda: developer_status(folder), self.show_status, self.refresh_button)

    def show_status(self, s):
        self.status = s
        built = s["xgconsole_built"]
        self.state_labels["built"].setText(status_html(built, "DistriBuild.exe 있음" if built else "DistriBuild.exe가 없음 (빌드 필요)"))
        real = s["real_incredibuild"]
        self.state_labels["incredibuild"].setText(
            status_html(not real, "실제 Incredibuild 없음" if not real else "실제 Incredibuild가 설치됨: %s (제거 필요)" % real))
        service = {"running": (True, "서비스 실행 중"), "stopped": (False, "서비스가 중지됨"),
                   "missing": (False, "서비스가 설치되지 않음"), "foreign": (False, "같은 이름의 다른 서비스가 있음"),
                   "unsupported": (None, "Windows에서만 필요")}[s["service"]]
        self.state_labels["service"].setText(status_html(*service))
        placed = s["installed"] and s["on_path"]
        self.state_labels["path"].setText(status_html(placed, "xgConsole이 설치되어 PATH에 있음" if placed else "xgConsole이 설치 폴더나 PATH에 없음"))
        # A package carries DistriBuild.exe ready-made but not the sources to build it, and the exe this
        # window runs from cannot be built over.
        rebuilding_self = FROZEN and self.tools and os.path.normcase(APP_DIR) == os.path.normcase(os.path.join(self.tools, "dist"))
        self.build_button.setEnabled(bool(self.tools) and os.path.isfile(os.path.join(self.tools, "build.bat"))
                                     and not rebuilding_self)
        if rebuilding_self:
            self.build_button.setToolTip("실행 중인 DistriBuild.exe는 다시 빌드할 수 없습니다. 창을 닫고 build.bat을 실행하세요.")
        self.install_button.setEnabled(bool(self.tools) and built and not real and s["service"] != "foreign")
        self.uninstall_button.setEnabled(bool(self.tools) and s["service"] != "foreign")
        if not self.tools:
            self.install_button.setToolTip("install_client.bat이 있는 폴더에서 실행해야 합니다.")
        self.window_.state_changed()

    def build_program(self):
        self.window_.run_process("cmd.exe", ["/c", os.path.join(self.tools, "build.bat")], self.build_button,
                                 cwd=self.tools, done=lambda _code: self.refresh_status())

    def install_command(self):
        """The UAC command that installs xgConsole, or None after logging why it cannot."""
        if not self.tools:
            self.log("[!!] install_client.bat이 있는 폴더에서 실행해야 합니다.")
            return None
        if not program_path():
            self.log("[!!] DistriBuild.exe가 없습니다. 먼저 프로그램을 빌드하세요.")
            return None
        return 'call "%s" "%s"' % (os.path.join(self.tools, "install_client.bat"), self.install_folder())

    def uninstall_command(self):
        return self.tools and 'call "%s" "%s"' % (os.path.join(self.tools, "uninstall_client.bat"), self.install_folder())

    def installed(self):
        self.log("PATH 변경은 새로 여는 터미널과 다시 시작한 Visual Studio부터 적용됩니다.")
        self.refresh_status()

    def install(self):
        command = self.install_command()
        if command:
            self.elevate(command, self.install_button, "설치", self.installed)

    def uninstall(self):
        command = self.uninstall_command()
        if command:
            self.elevate(command, self.uninstall_button, "제거", self.refresh_status)

    def run_doctor(self):
        path = fresh_path()
        program = shutil.which("xgConsole", path=path) or os.path.join(self.install_folder(), "xgConsole.exe")
        if not os.path.isfile(program):
            self.log("[!!] xgConsole.exe를 찾지 못했습니다. 먼저 설치하세요.")
            return
        self.save()
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PATH", path)

        def finished(code):
            self.log("[ok] 진단: 문제 없음" if code == 0 else "[!!] 진단: 위의 문제를 확인하세요")

        self.window_.run_process(program, ["--doctor"], self.doctor_button, environment=environment, done=finished)

    def add_address(self):
        text = self.address.text().strip()
        if not text:
            return
        try:
            parsed = parse_agent_address(text)
        except ValueError:
            parsed = None
        if not parsed or not 0 < parsed[1] < 65536:
            self.log("[!!] 주소 형식이 올바르지 않습니다: %s" % text)
            return
        if text not in self.addresses:
            self.addresses.append(text)
        self.address.clear()
        self.find_agents()

    def find_agents(self):
        addresses, port, token = list(self.addresses), self.discovery_port.value(), self.token.text()
        self.bus.start(lambda: query_agents(addresses, port, token), self.show_agents, self.find_button)

    def show_agents(self, rows):
        self.agent_rows = rows
        table = self.agent_table
        table.setRowCount(0)
        local_ids = set(self.compiler_paths)
        for row in rows:
            info = row["info"] or {}
            usable = not row["error"]
            offered = set(info.get("compilers", []))
            common_ids = sorted(local_ids & offered)
            if not usable:
                match, missing = "-", ""
            else:
                match = ", ".join(cid.split("-")[1] for cid in common_ids) or "없음"
                missing = "이 PC에 있지만 헬퍼에 없는 컴파일러: " + (", ".join(sorted(local_ids - offered)) or "없음")
            kind = "고정" if row["pinned"] else "자동 검색" if self.discovery.isChecked() else "자동 검색 꺼짐 (고정 필요)"
            color = (3, BAD) if not usable else (4, BAD) if not common_ids and local_ids else None
            state = row["error"] or "연결됨"
            if row.get("note"):
                state += " (%s)" % row["note"]
            if usable and info.get("owner_building"):
                state += " (자기 빌드 중이라 쉼)"
            if info.get("machine") == machine_id():
                kind, color = "이 PC", None
                missing = "이 PC의 빌드에는 쓰지 않습니다. 이 PC가 빌드하는 동안에는 다른 PC의 작업도 받지 않습니다."
            if row.get("stale"):
                missing = "빌드할 때마다 이 주소에 연결을 시도하느라 기다립니다. 더 쓰지 않는 헬퍼면 제거하세요."
            add_table_row(table, [row["address"], info.get("name", "-"), info.get("slots", "-"),
                                  state, match, kind], row, color=color, tooltip=missing)
        stale = [r["address"] for r in rows if r.get("stale")]
        self.remove_stale_button.setEnabled(bool(stale))
        self.log("헬퍼 PC %d대를 확인했습니다." % len(rows))
        if stale:
            self.log("[!!] %d일 넘게 연결되지 않은 고정 주소: %s" % (STALE_AFTER_DAYS, ", ".join(stale)))

    def remove_stale(self):
        stale = [r["address"] for r in self.agent_rows if r.get("stale") and r["address"] in self.addresses]
        for address in stale:
            self.addresses.remove(address)
        if stale:
            self.log("고정 주소에서 뺐습니다: %s (저장해야 빌드에 적용됩니다)" % ", ".join(stale))
        self.find_agents()

    def pin_selected(self):
        for row in selected_data(self.agent_table):
            if row["address"] not in self.addresses:
                self.addresses.append(row["address"])
        self.find_agents()

    def remove_selected(self):
        for row in selected_data(self.agent_table):
            if row["address"] in self.addresses:
                self.addresses.remove(row["address"])
        self.find_agents()

    def show_compilers(self, compilers):
        self.compiler_paths = compilers
        self.package_list.clear()
        for cid, path in compilers.items():
            item = QListWidgetItem(cid)
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, cid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.package_list.addItem(item)
        self.find_agents()

    def choose_package_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "패키지를 만들 폴더 선택", self.package_dest.text())
        if folder:
            self.package_dest.setText(os.path.normpath(folder))

    def make_package(self):
        dest = self.package_dest.text().strip()
        if not dest or not os.path.isabs(dest):
            self.log("[!!] 패키지를 만들 폴더를 전체 경로로 지정하세요.")
            return
        chosen = {}
        for index in range(self.package_list.count()):
            item = self.package_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                cid = item.data(Qt.ItemDataRole.UserRole)
                chosen[cid] = self.compiler_paths[cid]

        def finished(result):
            if result["missing"]:
                self.log("[!!] 포함하지 못한 프로그램: %s (build.bat으로 먼저 빌드하세요)"
                         % ", ".join(result["missing"]))
            if result["busy"]:
                self.log("[!!] 실행 중이라 바꾸지 못한 프로그램: %s (헬퍼에서 에이전트를 멈춘 뒤 다시 만들면 바뀝니다)"
                         % ", ".join(result["busy"]))
            self.log("[ok] 패키지를 만들었습니다: %s" % dest)
            self.log("헬퍼의 패키지 폴더에 바로 만들었다면 실행 중인 에이전트가 1분 안에 새 컴파일러를 씁니다. "
                     "아니면 이 폴더의 내용을 헬퍼 PC의 C:\\DistriBuild에 복사하세요.")

        self.log("패키지를 만드는 중입니다...")
        self.bus.start(lambda: build_package(dest, chosen, self.bus.line.emit), finished, self.package_button)


# --------------------------------------------------------------------------- window

# --------------------------------------------------------------------------- both roles on one PC

class BothPage(Page):
    """Sets this PC up as a developer PC and a helper at once: one token, one UAC prompt.

    Builds on this PC skip its own agent and pause it for other PCs while they run, so every PC of a
    team can be set up the same way and helps whenever it is not building.
    """

    def __init__(self, window):
        super().__init__(window)
        self.helper, self.developer = window.helper, window.developer
        intro = QLabel("이 PC를 개발자 PC이자 헬퍼 PC로 씁니다. 빌드하지 않을 때는 다른 PC의 빌드를 돕고, "
                       "이 PC에서 빌드를 시작하면 끝날 때까지 다른 PC의 작업을 받지 않습니다. "
                       "팀의 모든 PC에서 같은 토큰으로 한 번씩 설정하세요.")
        intro.setWordWrap(True)
        self.token = QLineEdit(self.developer.token.text() or self.helper.token.text())
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("팀이 함께 쓰는 토큰")
        form = QFormLayout()
        form.addRow("토큰", self.token)
        self.state_labels = {key: QLabel() for key in ("developer", "firewall", "autostart")}
        states = QVBoxLayout()
        for label in self.state_labels.values():
            label.setWordWrap(True)
            states.addWidget(label)
        self.setup_button = QPushButton("모두 설정 (관리자)")
        self.setup_button.clicked.connect(self.set_up)
        self.remove_button = QPushButton("모두 해제 (관리자)")
        self.remove_button.clicked.connect(self.remove)
        refresh = QPushButton("상태 새로고침")
        refresh.clicked.connect(self.refresh)
        note = QLabel("모두 설정: 토큰을 두 탭에 저장하고, UAC 한 번으로 xgConsole 설치, 방화벽 열기, PC를 켜면 "
                      "에이전트 자동 시작을 합니다. 세부 설정은 헬퍼 PC 탭과 개발자 PC 탭에서 바꿉니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: %s" % NEUTRAL)
        layout = QVBoxLayout()
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addLayout(states)
        layout.addLayout(button_row(self.setup_button, self.remove_button, refresh))
        layout.addWidget(note)
        group = QGroupBox("이 PC를 개발자 겸 헬퍼로")
        group.setLayout(layout)
        outer = QVBoxLayout(self)
        outer.addWidget(group)
        outer.addStretch(1)
        self.update_state()

    def showEvent(self, event):
        # The other tabs may have changed the token meanwhile.
        if not self.token.hasFocus():
            self.token.setText(self.developer.token.text() or self.helper.token.text())
        super().showEvent(event)

    def refresh(self):
        self.developer.refresh_status()
        self.helper.scan()

    def update_state(self):
        s = self.developer.status
        if s is None:
            developer = (None, "개발자: 확인 중...")
        else:
            ready = s["installed"] and s["on_path"] and s["service"] in ("running", "unsupported")
            developer = (ready, "개발자: UBT가 DistriBuild로 빌드함" if ready else "개발자: 설치 안 됨 (xgConsole, 서비스, PATH)")
        firewall = {True: (True, "헬퍼: 방화벽 열림"), False: (False, "헬퍼: 방화벽 규칙 없음"),
                    None: (None, "헬퍼: 방화벽 확인 중...")}[self.helper.firewall_state]
        task, command = self.helper.autostart, autostart_command(self.helper.config_path)
        if task and command and autostart_matches(task, command):
            autostart = (True, "헬퍼: PC를 켜면 에이전트 자동 시작" + (" (실행 중)" if self.helper.listening else ""))
        elif task:
            autostart = (False, "헬퍼: 자동 시작이 다른 위치의 에이전트를 실행함 (모두 설정으로 다시 등록)")
        else:
            autostart = (False, "헬퍼: 자동 시작 꺼짐")
        for key, value in (("developer", developer), ("firewall", firewall), ("autostart", autostart)):
            self.state_labels[key].setText(status_html(*value))

    def set_up(self):
        token = self.token.text()
        if token and not valid_token(token):
            self.log("[!!] 토큰에는 공백 없이 256자까지 쓸 수 있습니다.")
            return
        if not token:
            self.log("토큰 없이 설정합니다. 같은 네트워크의 누구나 이 PC에 작업을 보낼 수 있습니다.")
        for page in (self.helper, self.developer):
            page.token.setText(token)
        self.developer.save()
        install = self.developer.install_command()
        if not install:
            return
        prepared = self.helper.prepare_autostart()  # saves the helper settings too
        if not prepared:
            return
        autostart, xml_path = prepared
        firewall = firewall_open_command(self.helper.port.value(), self.helper.discovery_port.value())
        command = " && ".join("(%s)" % step for step in (install, firewall, autostart))

        def done():
            self.developer.installed()
            self.helper.autostart_started(xml_path)

        self.elevate(command, self.setup_button, "개발자 겸 헬퍼 설정", done)

    def remove(self):
        steps = [autostart_disable_command(self.helper.autostart_markers()), firewall_close_command()]
        uninstall = self.developer.uninstall_command()
        if uninstall:
            steps.insert(0, uninstall)
        # Every step runs even if an earlier one finds nothing to remove.
        self.elevate(" & ".join("(%s)" % step for step in steps), self.remove_button, "개발자 겸 헬퍼 해제", self.refresh)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DistriBuild 설정")
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1180, available.width()), min(880, available.height()))
        self.bus = Bus()
        self.wheel_guard = WheelGuard(self)
        self.children_running = []

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.bus.line.connect(self.append_log)

        self.both = None
        self.helper = HelperPage(self)
        self.developer = DeveloperPage(self)
        self.both = BothPage(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.both, "개발자 겸 헬퍼")
        self.tabs.addTab(self.helper, "헬퍼 PC")
        self.tabs.addTab(self.developer, "개발자 PC")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.log_view)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([740, 130])
        self.setCentralWidget(splitter)

    def state_changed(self):
        if self.both is not None:
            self.both.update_state()

    @Slot(str)
    def append_log(self, text):
        form = QTextCharFormat()
        if "[!!]" in text:
            form.setForeground(QColor(BAD))
        elif "[ok]" in text:
            form.setForeground(QColor(GOOD))
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", form)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def run_process(self, program, arguments, button, cwd=None, environment=None, done=None):
        """Runs a tool, streaming its output into the log. done receives the exit code, or -1 if it never started."""
        process = LoggedProcess(self, self.append_log)
        if cwd:
            process.setWorkingDirectory(cwd)
        if environment:
            process.setProcessEnvironment(environment)
        button.setEnabled(False)

        def ended(code):
            button.setEnabled(True)
            self.children_running.remove(process)
            process.deleteLater()
            if done:
                done(code)

        def failed(error):
            if error == QProcess.ProcessError.FailedToStart:
                self.append_log("[!!] 실행하지 못했습니다: %s (%s)" % (program, process.errorString()))
                ended(-1)

        process.finished.connect(lambda code, _status: ended(code))
        process.errorOccurred.connect(failed)
        self.children_running.append(process)
        process.start(program, arguments)

    def closeEvent(self, event):
        if self.helper.is_running():
            answer = QMessageBox.question(self, "DistriBuild 설정", "에이전트가 실행 중입니다. 창을 닫으면 에이전트도 중지됩니다. 닫을까요?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.helper.stop_agent()
        for process in list(self.children_running):
            process.kill()
            process.waitForFinished(2000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
