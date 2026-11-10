#!/usr/bin/env python3
"""Drives the Qt setup window against a real agent on a private port, with a throwaway home folder.

    pip install PySide6-Essentials
    python check_setup_gui.py [--screens <folder>]

--screens uses the real display driver (no window is shown) and saves a picture of each tab.
Nothing here needs administrator rights: the UAC path is exercised with an unelevated verb.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "DistriBuild"))
PORT, DISCOVERY = 31990, 31991
TOKEN = "gui-test-token"

parser = argparse.ArgumentParser()
parser.add_argument("--screens")
args = parser.parse_args()

work = tempfile.mkdtemp(prefix="dbgui")
os.environ["USERPROFILE"] = os.environ["HOME"] = work
os.environ.pop("DISTRIBUILD_CONFIG", None)
if not args.screens:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, TOOLS)

import setup_gui as gui  # noqa: E402  (the environment above must be set first)
import xgconsole  # noqa: E402
from common import compiler_id  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failures = []


def check(ok, text):
    print("  [%s] %s" % ("ok" if ok else "!!", text))
    if not ok:
        failures.append(text)


app = QApplication([])


def pump(condition, timeout=90):
    end = time.time() + timeout
    while not condition() and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return condition()


def settled(button):
    return button.isEnabled()


def table_rows(table):
    return [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]


def main():
    config_dir = os.path.join(work, ".distribuild")
    os.makedirs(config_dir)
    agent_json = os.path.join(config_dir, "agent.json")
    with open(agent_json, "w", encoding="utf-8") as f:
        json.dump({"custom_key": 1, "port": PORT, "discovery_port": DISCOVERY}, f)
    damaged = os.path.join(config_dir, "client.json")
    with open(damaged, "w", encoding="utf-8") as f:
        f.write("{ not json")

    window = gui.MainWindow()
    helper, developer = window.helper, window.developer
    # Lays the window out as a shown one, scroll bars included, without putting it on the user's screen.
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.show()

    print("Damaged settings:")
    check(os.path.isfile(damaged + ".bak"), "a damaged client.json is kept as .bak")

    print("Helper PC page:")
    check(pump(lambda: helper.offer_table.rowCount() > 0 and settled(helper.rescan_button)), "installed compilers are listed")
    rows = table_rows(helper.offer_table)
    check(any(r[1].startswith("msvc-") for r in rows), "an MSVC compiler id is shown")
    helper.name.setText("gui-test")
    helper.slots.setValue(3)
    helper.token.setText(TOKEN)
    helper.pch_gb.setValue(12.5)
    helper.compilers.append(os.path.join(work, "missing", "cl.exe"))
    helper.scan()
    check(pump(lambda: any(r[1] == "인식 실패" for r in table_rows(helper.offer_table))), "a bad compiler path is flagged")
    helper.offer_table.selectRow(0)
    helper.remove_selected()
    check(helper.compilers == [], "the manual entry can be removed")
    pump(lambda: settled(helper.rescan_button))
    helper.save()
    with open(agent_json, encoding="utf-8") as f:
        saved = json.load(f)
    check(saved.get("custom_key") == 1, "settings the window does not show are preserved")
    check((saved["name"], saved["slots"], saved["token"], saved["pch_cache_gb"]) == ("gui-test", 3, TOKEN, 12.5), "values are written")
    detected = subprocess.run([sys.executable, os.path.join(TOOLS, "agent.py"), "--config", agent_json, "--print-detected"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    check(detected.returncode == 0 and b"gui-test" in detected.stdout, "agent.py accepts the saved file")

    print("Running the agent:")
    helper.start_agent()
    check(pump(lambda: gui.port_in_use(PORT), 60), "the agent starts and listens")
    info, error = gui.probe_agent("127.0.0.1", PORT, TOKEN)
    check(bool(info) and info["name"] == "gui-test" and info["slots"] == 3, "it runs with the saved settings (%s)" % (error or "reply received"))
    check(helper.is_running() and not helper.start_button.isEnabled() and helper.stop_button.isEnabled(), "start/stop buttons follow its state")

    print("Developer PC page:")
    check(pump(lambda: bool(developer.compiler_paths)), "this PC's compilers are listed")
    check(all(cid.endswith("-x64") for cid in developer.compiler_paths), "only x64 compilers are offered for packaging")
    developer.discovery_port.setValue(DISCOVERY)
    developer.token.setText("wrong")
    developer.find_agents()
    pump(lambda: any(r["info"] and r["info"].get("name") == "gui-test" for r in developer.agent_rows) and settled(developer.find_button))
    rows = table_rows(developer.agent_table)
    check(any(r[1] == "gui-test" and r[3] == "토큰이 맞지 않음" for r in rows), "a wrong token is reported")
    developer.token.setText(TOKEN)
    developer.find_agents()
    pump(lambda: any(not r["error"] for r in developer.agent_rows) and settled(developer.find_button))
    rows = table_rows(developer.agent_table)
    found = [r for r in rows if r[1] == "gui-test"]
    check(len(found) == 1, "one PC is listed once, though it answers on several addresses")
    check(bool(found) and found[0][3] == "연결됨" and found[0][4] not in ("-", "없음") and found[0][5] == "자동 검색", "the agent is found with matching compilers")
    developer.agent_table.selectRow(0)
    developer.pin_selected()
    pump(lambda: bool(developer.addresses) and settled(developer.find_button) and any(r["pinned"] for r in developer.agent_rows))
    check(len(developer.addresses) == 1, "a found agent can be pinned")
    developer.address.setText("host:notaport")
    developer.add_address()
    check(len(developer.addresses) == 1, "a malformed address is refused")
    developer.local_first.setChecked(False)
    developer.save()
    os.remove(damaged + ".bak")
    cfg = xgconsole.load_config()
    check(cfg["agents"] == developer.addresses and cfg["token"] == TOKEN and cfg["local_first"] is False and cfg["discovery_port"] == DISCOVERY,
          "xgConsole reads the saved file")
    pool = xgconsole.AgentPool(cfg)
    check(pool.capacity() == 3, "xgConsole's agent pool sees the agent once (%d slots)" % pool.capacity())

    print("Pictures:")
    if args.screens:
        os.makedirs(args.screens, exist_ok=True)
        for index, name in enumerate(("helper", "developer")):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
            window.grab().save(os.path.join(args.screens, name + ".png"))
        check(True, "saved to %s" % args.screens)
    else:
        print("  (skipped)")

    print("Helper package:")
    cid, cl = next(iter(developer.compiler_paths.items()))
    for index in range(1, developer.package_list.count()):
        developer.package_list.item(index).setCheckState(Qt.CheckState.Unchecked)
    package = os.path.join(work, "package folder")
    developer.package_dest.setText(package)
    developer.make_package()
    check(pump(lambda: settled(developer.package_button), 180), "the package finishes")
    copied = os.path.join(package, "msvc", cid, "cl.exe")
    check(os.path.isfile(copied) and compiler_id(copied) == cid, "the packaged compiler keeps its id")
    check(os.path.isfile(os.path.join(package, "DistriBuildAgent.exe")), "the agent program is included")
    original_app_dir = gui.APP_DIR
    gui.APP_DIR = package
    helper.compilers = []
    helper.pick_up_packaged_compilers()
    gui.APP_DIR = original_app_dir
    check(helper.compilers == [copied], "a helper picks up the package's compilers on its own")

    print("Running with a UAC prompt (unelevated verb):")
    spaced = os.path.join(work, "a b (x86)")
    os.makedirs(spaced)
    script = os.path.join(spaced, "tool.bat")
    with open(script, "w", encoding="ascii") as f:
        f.write('@echo off\r\necho first\r\necho arg=%~1\r\nexit /b 3\r\n')
    code, output = gui.run_elevated('call "%s" "C:\\Some Folder"' % script, verb="open")
    check(code == 3, "the exit code comes back (%s)" % code)
    check("first" in output and "arg=C:\\Some Folder" in output, "the output comes back, and spaced arguments arrive whole")
    code, output = gui.run_elevated("(echo one & cmd /c exit 0) && (echo two & cmd /c exit 5)", verb="open")
    check(code == 5 and "one" in output and "two" in output, "a later step's failure is reported and every step is captured")

    print("Doctor:")
    if shutil.which("xgConsole", path=gui.fresh_path()) or os.path.isfile(os.path.join(gui.DEFAULT_BIN, "xgConsole.exe")):
        before = window.log_view.toPlainText().count("진단:")
        developer.run_doctor()
        pump(lambda: window.log_view.toPlainText().count("진단:") > before, 90)
        text = window.log_view.toPlainText()
        check("UnrealBuildTool:" in text and "진단:" in text, "the tool's output reaches the log")
        check("3 remote slots" in text, "it sees the saved agent")
    else:
        print("  (skipped: xgConsole is not installed)")

    print("Stopping:")
    pump(lambda: settled(developer.refresh_button))
    helper.stop_agent()
    check(pump(lambda: not gui.port_in_use(PORT), 20), "the agent stops")
    check(not helper.is_running() and helper.start_button.isEnabled(), "the window notices")
    window.close()


try:
    main()
finally:
    if sys.platform == "win32":
        # cl.exe leaves its telemetry helper running, which would keep the copied folder locked.
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-Process vctip -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%s*' } | Stop-Process -Force" % work],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    shutil.rmtree(work, ignore_errors=True)

print()
if failures:
    print("FAILED:")
    for item in failures:
        print("  - " + item)
    sys.exit(1)
print("All setup GUI checks passed.")
