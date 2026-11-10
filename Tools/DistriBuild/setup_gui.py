#!/usr/bin/env python3
"""DistriBuild setup: a Qt window that configures helper PCs and developer PCs.

    pip install PySide6-Essentials
    python setup_gui.py

build_setup_gui.bat turns this into dist\\DistriBuildSetup.exe, which needs no Python.

It edits the same files the command line tools read (agent.json, ~/.distribuild/client.json) and
runs the existing install_client.bat, so everything stays usable without the GUI.
"""

import ctypes
import glob
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    # A windowed program has no console to share, so every console child would flash its own window.
    class _NoWindowPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen

try:
    from PySide6.QtCore import QEvent, QObject, QProcess, QProcessEnvironment, Qt, Signal, Slot
    from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
    from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QDoubleSpinBox,
                                   QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                                   QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                                   QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                                   QTabWidget, QVBoxLayout, QWidget)
except ImportError:
    sys.exit("PySide6 is required:  pip install PySide6-Essentials")

from agent import detect_compilers, detect_engines
from common import (DEFAULT_PORT, DISCOVERY_PORT, compiler_family, compiler_id, decode_output, discover_agents,
                    parse_agent_address, request)

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".distribuild")
DEFAULT_BIN = r"C:\DistriBuild\bin"
SERVICE = "Incredibuild Agent"
SHIM = "DistriBuildXgeShim"
FIREWALL_TCP = "DistriBuild Agent TCP"
FIREWALL_UDP = "DistriBuild Agent UDP"
PACKAGE_EXES = ("DistriBuildSetup.exe", "DistriBuildAgent.exe")
# A cross compiler also needs the host toolset's DLLs, so only x64-hosted x64 copies work on their own.
PACKAGEABLE = re.compile(r"^msvc-.*-x64$")
GOOD, BAD, NEUTRAL = "#2e7d32", "#c62828", "#808080"


# --------------------------------------------------------------------------- paths and files

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


# --------------------------------------------------------------------------- helper PC work

def agent_command(config_path):
    """How to start the agent, or None. From source this runs agent.py, so it can never be a stale exe."""
    if not FROZEN:
        return [sys.executable, "-u", os.path.join(APP_DIR, "agent.py"), "--config", config_path]
    exe = os.path.join(APP_DIR, "DistriBuildAgent.exe")
    return [exe, "--config", config_path] if os.path.isfile(exe) else None


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
        rows.append({"kind": "compiler", "id": cid, "path": path, "manual": True})
    for root in engines:
        found = detect_engines([root], False)
        key = next(iter(found), None)
        seen.add(key)
        rows.append({"kind": "engine", "id": key, "path": root, "manual": True})
    if auto:
        for kind, found in (("compiler", detect_compilers([], True)), ("engine", detect_engines([], True))):
            rows += [{"kind": kind, "id": key, "path": path, "manual": False}
                     for key, path in sorted(found.items()) if key not in seen]
    info, _ = probe_agent("127.0.0.1", port, token, timeout=0.5)
    listening = info is not None or port_in_use(port)
    return {"rows": rows, "listening": listening, "firewall": firewall_is_open(port, discovery_port)}


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
        status["xgconsole_built"] = os.path.isfile(os.path.join(tools, "dist", "xgConsole.exe"))
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
    for text in addresses:
        host, port = parse_agent_address(text)
        info, error = probe_agent(host, port, token)
        rows.append({"address": text, "pinned": True, "info": info, "error": error})
        if info:
            seen.add((info.get("name"), port))
    # An agent on this PC answers on its LAN address and on loopback; keep the one other PCs could use too.
    found = sorted(discover_agents(discovery_port), key=lambda item: item[0].startswith("127."))
    for host, info in found:
        port = int(info.get("port", DEFAULT_PORT))
        if (info.get("name"), port) in seen:
            continue
        seen.add((info.get("name"), port))
        checked, error = probe_agent(host, port, token)
        rows.append({"address": "%s:%d" % (host, port), "pinned": False, "info": checked or info, "error": error})
    return rows


def build_package(dest, compilers, report):
    """Fills dest with what a helper PC needs. Returns the program names that could not be included."""
    os.makedirs(dest, exist_ok=True)
    tools = find_tools_dir()
    folders = [APP_DIR] + ([os.path.join(tools, "dist")] if tools else [])
    missing = []
    for name in PACKAGE_EXES:
        source = next((os.path.join(f, name) for f in folders if os.path.isfile(os.path.join(f, name))), None)
        if source is None:
            missing.append(name)
            continue
        report("복사 중: %s" % name)
        target = os.path.join(dest, name)
        if os.path.normcase(os.path.abspath(source)) != os.path.normcase(os.path.abspath(target)):
            shutil.copy2(source, target)
    for cid, cl in compilers.items():
        report("복사 중: %s" % cid)
        target = os.path.join(dest, "msvc", cid)
        shutil.copytree(os.path.dirname(cl), target, dirs_exist_ok=True)
        copied = compiler_id(os.path.join(target, "cl.exe"))
        if copied != cid:
            raise RuntimeError("복사한 컴파일러의 ID가 다릅니다: %s, 원본 %s" % (copied, cid))
    return missing


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
        self.pick_up_packaged_compilers()
        self.build_ui()
        self.scan()

    def pick_up_packaged_compilers(self):
        """A package copied to this PC brings its compilers in a msvc folder beside the program."""
        packaged = sorted(glob.glob(os.path.join(APP_DIR, "msvc", "*", "cl.exe")))
        added = [p for p in packaged if p not in self.compilers]
        if added:
            self.compilers += added
            self.log("패키지의 컴파일러 %d개를 목록에 추가했습니다." % len(added))

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
        note = QLabel("이 창을 닫으면 여기서 시작한 에이전트도 중지됩니다.")
        note.setStyleSheet("color: %s" % NEUTRAL)
        run_layout.addWidget(note)
        run_layout.addLayout(button_row(self.start_button, self.stop_button))
        run_group = QGroupBox("실행")
        run_group.setLayout(run_layout)

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
        data = self.current_config()
        write_config(self.config_path, data)
        self.loaded = data
        self.log("[ok] 저장했습니다: %s" % self.config_path)
        if self.is_running():
            self.log("실행 중인 에이전트에는 다시 시작해야 적용됩니다.")

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
            add_table_row(table, [labels[row["kind"]], identifier, row["path"], "직접 추가" if row["manual"] else "자동"],
                          row, color=None if row["id"] else (1, BAD), tooltip=row["path"])
        self.listening = result["listening"]
        self.update_run_state()
        state = result["firewall"]
        text = {True: "TCP %d, UDP %d 허용됨", False: "TCP %d, UDP %d 규칙 없음 (다른 PC에서 접속하려면 필요)",
                None: "TCP %d, UDP %d: 이 운영체제에서는 직접 확인하세요"}[state]
        self.firewall_status.setText(status_html(state, text % (self.port.value(), self.discovery_port.value())))

    def is_running(self):
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def update_run_state(self):
        running = self.is_running()
        if running:
            self.agent_status.setText(status_html(True, "실행 중"))
        elif self.listening:
            self.agent_status.setText(status_html(True, "이 PC에서 이미 실행 중 (다른 프로그램에서 시작함)"))
        else:
            self.agent_status.setText(status_html(False, "중지됨"))
        self.start_button.setEnabled(not running and not self.listening)
        self.stop_button.setEnabled(running)

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
            self.log("[!!] DistriBuildAgent.exe를 이 프로그램과 같은 폴더에 두어야 합니다.")
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
        path, _ = QFileDialog.getOpenFileName(self, "cl.exe 선택", os.path.join(APP_DIR, "msvc"), "cl.exe (cl.exe)")
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
        if any(not row["manual"] for row in chosen):
            self.log("자동으로 찾은 항목은 제거할 수 없습니다. 자동 찾기를 끄면 목록에서 빠집니다.")
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
        self.build_button = QPushButton("xgConsole 빌드")
        self.install_button = QPushButton("설치 / 업데이트 (관리자)")
        self.uninstall_button = QPushButton("제거 (관리자)")
        self.build_button.clicked.connect(self.build_xgconsole)
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
        options = QFormLayout()
        options.addRow("토큰", self.token)
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
        self.find_button.clicked.connect(self.find_agents)
        pin.clicked.connect(self.pin_selected)
        remove.clicked.connect(self.remove_selected)
        agents_layout = QVBoxLayout()
        agents_layout.addWidget(self.agent_table, 1)
        agents_layout.addLayout(browse_row(self.address, add))
        agents_layout.addLayout(button_row(self.find_button, pin, remove))
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
        hint = QLabel("만든 폴더의 내용을 헬퍼 PC의 C:\\DistriBuild에 복사하고 DistriBuildSetup.exe를 실행하면 "
                      "컴파일러가 자동으로 등록됩니다.")
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

    def install_folder(self):
        return self.bin_dir.text().strip().rstrip("\\/")

    def refresh_status(self):
        folder = self.install_folder()
        self.bus.start(lambda: developer_status(folder), self.show_status, self.refresh_button)

    def show_status(self, s):
        built = s["xgconsole_built"]
        self.state_labels["built"].setText(status_html(built, "xgConsole.exe 빌드됨" if built else "xgConsole.exe가 없음 (빌드 필요)"))
        real = s["real_incredibuild"]
        self.state_labels["incredibuild"].setText(
            status_html(not real, "실제 Incredibuild 없음" if not real else "실제 Incredibuild가 설치됨: %s (제거 필요)" % real))
        service = {"running": (True, "서비스 실행 중"), "stopped": (False, "서비스가 중지됨"),
                   "missing": (False, "서비스가 설치되지 않음"), "foreign": (False, "같은 이름의 다른 서비스가 있음"),
                   "unsupported": (None, "Windows에서만 필요")}[s["service"]]
        self.state_labels["service"].setText(status_html(*service))
        placed = s["installed"] and s["on_path"]
        self.state_labels["path"].setText(status_html(placed, "xgConsole이 설치되어 PATH에 있음" if placed else "xgConsole이 설치 폴더나 PATH에 없음"))
        self.build_button.setEnabled(bool(self.tools))
        self.install_button.setEnabled(bool(self.tools) and built and not real and s["service"] != "foreign")
        self.uninstall_button.setEnabled(bool(self.tools) and s["service"] != "foreign")
        if not self.tools:
            self.install_button.setToolTip("install_client.bat이 있는 폴더에서 실행해야 합니다.")

    def build_xgconsole(self):
        self.window_.run_process("cmd.exe", ["/c", os.path.join(self.tools, "build_xgconsole.bat")], self.build_button,
                                 cwd=self.tools, done=lambda _code: self.refresh_status())

    def install(self):
        command = 'call "%s" "%s"' % (os.path.join(self.tools, "install_client.bat"), self.install_folder())

        def finished():
            self.log("PATH 변경은 새로 여는 터미널과 다시 시작한 Visual Studio부터 적용됩니다.")
            self.refresh_status()

        self.elevate(command, self.install_button, "설치", finished)

    def uninstall(self):
        command = 'call "%s" "%s"' % (os.path.join(self.tools, "uninstall_client.bat"), self.install_folder())
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
            add_table_row(table, [row["address"], info.get("name", "-"), info.get("slots", "-"),
                                  row["error"] or "연결됨", match, kind], row, color=color, tooltip=missing)
        self.log("헬퍼 PC %d대를 확인했습니다." % len(rows))

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

        def finished(missing):
            if missing:
                self.log("[!!] 포함하지 못한 프로그램: %s (build_xgconsole.bat, build_setup_gui.bat으로 먼저 빌드하세요)" % ", ".join(missing))
            self.log("[ok] 패키지를 만들었습니다: %s" % dest)
            self.log("이 폴더의 내용을 헬퍼 PC의 C:\\DistriBuild에 복사하고 DistriBuildSetup.exe를 실행하세요.")

        self.log("패키지를 만드는 중입니다...")
        self.bus.start(lambda: build_package(dest, chosen, self.bus.line.emit), finished, self.package_button)


# --------------------------------------------------------------------------- window

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

        self.helper = HelperPage(self)
        self.developer = DeveloperPage(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.helper, "헬퍼 PC")
        self.tabs.addTab(self.developer, "개발자 PC")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.log_view)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([740, 130])
        self.setCentralWidget(splitter)

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
