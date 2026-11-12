#!/usr/bin/env python3
"""Drives the Qt setup window against a real agent on a private port, with a throwaway home folder.

    pip install PySide6-Essentials
    python check_setup_gui.py [--screens <folder>]

--screens uses the real display driver (no window is shown) and saves a picture of each tab.
Nothing here needs administrator rights: the UAC path is exercised with an unelevated verb.
"""

import argparse
import base64
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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "DistriBuild"))
PORT, DISCOVERY = 31990, 31991
PACKAGE_PORT = 31996
FIXED_PORT = 31997
TOKEN = "gui-test-token"

parser = argparse.ArgumentParser()
parser.add_argument("--screens")
args = parser.parse_args()

work = tempfile.mkdtemp(prefix="dbgui")
os.environ["USERPROFILE"] = os.environ["HOME"] = work
os.environ.pop("DISTRIBUILD_CONFIG", None)
# The agents started here stand for other PCs; one section takes this off to test this PC's own agent.
os.environ["DISTRIBUILD_AGENT_MACHINE"] = "gui-test-helper"
if not args.screens:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, TOOLS)

import setup_gui as gui  # noqa: E402  (the environment above must be set first)
import xgconsole  # noqa: E402
from common import compiler_id, contacts_path  # noqa: E402
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

    print("Changing every helper's token:")
    fixed = subprocess.Popen([sys.executable, os.path.join(TOOLS, "agent.py"), "--port", str(FIXED_PORT), "--token", TOKEN,
                              "--no-auto-detect"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        check(pump(lambda: gui.port_in_use(FIXED_PORT), 60), "a second helper, started with --token, is running")
        developer.addresses.append("127.0.0.1:%d" % FIXED_PORT)
        new_token = "changed-token-1"
        developer.change_tokens(new_token)
        check(pump(lambda: developer.token_box is not None and settled(developer.token_button), 60), "the change finishes")
        check(gui.probe_agent("127.0.0.1", PORT, new_token)[0] is not None and gui.probe_agent("127.0.0.1", PORT, TOKEN)[0] is None,
              "the running helper takes the new token at once, without a restart")
        with open(agent_json, encoding="utf-8") as f:
            saved = json.load(f)
        check(saved.get("token") == new_token and saved.get("custom_key") == 1, "it saves it to its agent.json, keeping the rest")
        check(xgconsole.load_config()["token"] == new_token, "this PC saves the new token too")
        box = developer.token_box.text() if developer.token_box else ""
        check("2대 중 1대" in box and new_token in box and "--token으로 실행 중" in box,
              "the summary shows the new token and the helper that could not change")
        developer.token_box.close()
        helper.save()
        with open(agent_json, encoding="utf-8") as f:
            check(json.load(f).get("token") == new_token and helper.token.text() == new_token,
                  "saving on the helper does not put the old token back")
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(fixed.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        fixed.wait(10)
    developer.addresses.remove("127.0.0.1:%d" % FIXED_PORT)
    developer.save()

    print("Pinned helpers that do not answer:")
    hung = []
    for _ in range(3):  # accept the connection but never answer: each costs the whole connect_timeout
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        hung.append(s)
    hung_addresses = ["127.0.0.1:%d" % s.getsockname()[1] for s in hung]
    cfg = xgconsole.load_config()
    cfg["agents"] = cfg["agents"] + hung_addresses
    started = time.time()
    pool = xgconsole.AgentPool(cfg)
    took = time.time() - started
    check(pool.capacity() == 3 and took < cfg["connect_timeout"] + 2,
          "three hung helpers cost one wait, not three (%.1fs, connect_timeout %ss)" % (took, cfg["connect_timeout"]))
    contacts = gui.load_agent_contacts()
    check(all("failing_since" in contacts.get(a, {}) for a in hung_addresses) and "last_ok" in contacts.get(cfg["agents"][0], {}),
          "each build records which pinned addresses answered")
    for s in hung:
        s.close()

    gone = socket.socket()
    gone.bind(("127.0.0.1", 0))
    gone_address = "127.0.0.1:%d" % gone.getsockname()[1]
    gone.close()  # nothing listens there any more
    recent = "127.0.0.1:1"
    contacts[gone_address] = {"last_ok": time.time() - 20 * 86400, "failing_since": time.time() - 10 * 86400}
    contacts[recent] = {"last_ok": time.time() - 2 * 86400, "failing_since": time.time() - 3600}
    with open(contacts_path(), "w", encoding="utf-8") as f:
        json.dump(contacts, f)
    developer.addresses += [gone_address, recent]
    developer.find_agents()
    pump(lambda: settled(developer.find_button) and any(r["address"] == gone_address for r in developer.agent_rows))
    rows = {r[0]: r for r in table_rows(developer.agent_table)}
    check("10일째 연결 안 됨" in rows.get(gone_address, [""] * 4)[3], "an address down for 10 days says so")
    check("마지막 연결 2일 전" in rows.get(recent, [""] * 4)[3], "a recently lost one shows when it last answered")
    check(developer.remove_stale_button.isEnabled(), "the clean-up button is offered")
    developer.remove_stale()
    pump(lambda: settled(developer.find_button))
    check(gone_address not in developer.addresses and recent in developer.addresses,
          "only the long-gone address is removed")
    check(not developer.remove_stale_button.isEnabled(), "the button goes away with it")
    developer.addresses.remove(recent)
    developer.save()

    print("This PC's own agent:")
    own_env = dict(os.environ)
    own_env.pop("DISTRIBUILD_AGENT_MACHINE")  # this one is this PC's agent
    own_port = FIXED_PORT + 1
    own_token = developer.token.text()
    own = subprocess.Popen([sys.executable, os.path.join(TOOLS, "agent.py"), "--port", str(own_port), "--token", own_token,
                            "--no-auto-detect"], env=own_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        check(pump(lambda: gui.port_in_use(own_port), 60), "an agent of this PC is running")
        own_address = "127.0.0.1:%d" % own_port
        developer.addresses.append(own_address)
        developer.find_agents()
        pump(lambda: settled(developer.find_button) and any(r["address"] == own_address for r in developer.agent_rows))
        rows = {r[0]: r for r in table_rows(developer.agent_table)}
        check(rows.get(own_address, [""] * 6)[5] == "이 PC", "the helper list marks it as this PC")
        request = gui.request
        request("127.0.0.1", own_port, {"type": "hold", "token": developer.token.text(), "machine": gui.machine_id(),
                                        "holder": "gui-test", "seconds": 30})
        developer.find_agents()
        pump(lambda: settled(developer.find_button) and any(r["address"] == own_address and (r["info"] or {}).get("owner_building")
                                                            for r in developer.agent_rows))
        rows = {r[0]: r for r in table_rows(developer.agent_table)}
        check("자기 빌드 중이라 쉼" in rows.get(own_address, [""] * 6)[3], "and shows when it pauses for its own build")
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(own.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        own.wait(10)
    developer.addresses.remove(own_address)
    developer.save()

    print("Pictures:")
    if args.screens:
        os.makedirs(args.screens, exist_ok=True)
        for page, name in ((window.both, "both"), (helper, "helper"), (developer, "developer")):
            window.tabs.setCurrentIndex(window.tabs.indexOf(page))
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
    check(os.path.isfile(os.path.join(package, gui.PROGRAM)), "the one program is included")
    check(all(os.path.isfile(os.path.join(package, name)) for name in gui.PACKAGE_DEVELOPER_FILES),
          "so is what makes it a developer PC too (installer, service source)")
    with open(os.path.join(package, "agent.json"), encoding="utf-8") as f:
        check(json.load(f) == {}, "a new package has its own empty settings file")
    with open(os.path.join(package, gui.MANUAL), encoding="utf-8") as f:
        check("보내는 사람" in f.read(), "and the manual for whoever gets it")
    original_app_dir = gui.APP_DIR
    gui.APP_DIR = package
    again = os.path.join(work, "package from a package")
    gui.build_package(again, {}, lambda text: None)
    check(os.path.isfile(os.path.join(again, gui.MANUAL)), "a package made from a package carries the manual on")
    check(gui.helper_config_path() == os.path.join(package, "agent.json"), "so the helper window run from it keeps its settings there")
    helper.compilers = [copied]
    helper.pick_up_packaged_compilers()
    check(helper.compilers == [], "a package compiler an older version saved is dropped from the settings")
    # This PC has the same compiler installed; like the agent, the window would prefer that copy.
    pump(lambda: settled(helper.rescan_button))
    helper.auto_detect.setChecked(False)
    pump(lambda: settled(helper.rescan_button) and any(r[3] == "패키지" for r in table_rows(helper.offer_table)))
    check(any(r[1] == cid and r[3] == "패키지" for r in table_rows(helper.offer_table)), "the window lists it as coming from the package")
    gui.APP_DIR = original_app_dir
    helper.auto_detect.setChecked(True)
    pump(lambda: settled(helper.rescan_button))

    print("The agent finds package compilers itself:")
    agent_cfg = os.path.join(work, "package agent.json")
    with open(agent_cfg, "w", encoding="utf-8") as f:
        json.dump({"auto_detect": False, "package_dir": package, "work_dir": os.path.join(work, "agent work")}, f)
    detected = subprocess.run([sys.executable, os.path.join(TOOLS, "agent.py"), "--config", agent_cfg, "--print-detected"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    check(cid.encode() in detected.stdout, "with nothing in its settings, it offers the package's compiler")

    import agent as agent_module
    later = os.path.join(work, "later")
    agent = agent_module.Agent({"auto_detect": False, "package_dir": later, "work_dir": os.path.join(work, "agent work")})
    previous = {}
    agent.rescan_package(previous)
    check(agent.compilers == {}, "an agent with an empty package starts with no compilers")
    check(agent.set_token("lock-out", "test") == {"ok": False, "reason": "no_token"},
          "a helper without a token cannot be locked by someone setting one remotely")
    target = os.path.join(later, "msvc", cid)
    os.makedirs(target)
    shutil.copy2(cl, target)  # cl.exe alone: the rest of the folder is still "being copied"
    agent.rescan_package(previous)
    check(cid not in agent.compilers, "a folder is not used while files are still arriving")
    shutil.copytree(os.path.dirname(cl), target, dirs_exist_ok=True)
    agent.rescan_package(previous)
    check(cid not in agent.compilers, "nor right after the last file")
    agent.rescan_package(previous)
    check(agent.compilers.get(cid) == os.path.join(target, "cl.exe") and cid in agent.info()["compilers"],
          "once the folder stops changing, the running agent offers it")
    broken = os.path.join(later, "msvc", "broken")
    os.makedirs(broken)
    with open(os.path.join(broken, "cl.exe"), "wb") as f:
        f.write(b"not a program")
    calls = []
    real_id = agent_module.compiler_id
    agent_module.compiler_id = lambda path, refresh=False: calls.append(path) or real_id(path, refresh)
    try:
        for _ in range(4):
            agent.rescan_package(previous)
    finally:
        agent_module.compiler_id = real_id
    check(len(calls) == 1 and len(agent.compilers) == 1, "a broken copy is tried once, not on every scan")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-Process vctip -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%s*' } | Stop-Process -Force" % later],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    shutil.rmtree(target)
    agent.rescan_package(previous)
    check(cid not in agent.compilers, "a deleted folder stops being offered")

    print("The packaged agent, and adding to a helper's folder while it runs:")
    packaged_exe = os.path.join(package, gui.PROGRAM)
    exe_cfg = os.path.join(work, "exe agent.json")
    with open(exe_cfg, "w", encoding="utf-8") as f:
        json.dump({"auto_detect": False, "port": PACKAGE_PORT, "discovery_port": PACKAGE_PORT + 1,
                   "work_dir": os.path.join(work, "exe work")}, f)
    running = subprocess.Popen([packaged_exe, "agent", "--config", exe_cfg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        check(pump(lambda: gui.port_in_use(PACKAGE_PORT), 60), "the packaged agent starts")
        info, error = gui.probe_agent("127.0.0.1", PACKAGE_PORT, "")
        check(bool(info) and cid in info["compilers"], "it offers the package's compiler with nothing in its settings (%s)"
              % (error or "reply received"))
        lines = []
        os.remove(os.path.join(package, "agent.json"))  # like a helper set up before packages had one
        result = gui.build_package(package, {cid: cl}, lines.append)
        check(not os.path.exists(os.path.join(package, "agent.json")),
              "a helper's folder in use gets no settings file, so one kept in the profile stays in use")
        check(result["busy"] == [gui.PROGRAM], "the running agent's program is left alone, not an error")
        check(("이미 있음: %s" % cid) in lines, "a compiler already there is not copied again")
        check(not [n for n in os.listdir(os.path.join(package, "msvc")) if n.startswith(".")], "no staging folder is left")
        others = [(other, path) for other, path in developer.compiler_paths.items() if other != cid]
        if others:
            other, other_cl = others[0]
            gui.build_package(package, {other: other_cl}, lambda line: None)
            seen = pump(lambda: other in (gui.probe_agent("127.0.0.1", PACKAGE_PORT, "")[0] or {}).get("compilers", []),
                        3 * agent_module.PACKAGE_SCAN_SECONDS)
            check(seen, "a compiler added to its folder is offered within a minute, without a restart")
        else:
            print("  (skipped live pickup: this PC has only one x64 compiler)")
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(running.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        running.wait(10)

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

    print("Start with Windows (the UAC step is recorded, not run; check_autostart.py runs a real task):")
    pump(lambda: settled(helper.rescan_button))
    check(helper.autostart is None and "꺼짐" in helper.autostart_status.text() and not helper.autostart_off_button.isEnabled(),
          "it shows as off")
    recorded = []
    real_run_elevated, real_query = gui.run_elevated, gui.autostart_query

    def fake_run_elevated(command, hwnd=0, verb="runas"):
        xml_file = re.search(r'/XML "([^"]+)"', command)
        if xml_file:
            with open(xml_file.group(1), encoding="utf-16") as f:
                recorded.append((command, f.read()))
        else:
            recorded.append((command, ""))
        return 0, ""

    gui.run_elevated = fake_run_elevated
    try:
        helper.enable_autostart()
        check(pump(lambda: bool(recorded) and settled(helper.autostart_on_button), 30), "turning it on asks for UAC once")
        command, xml = recorded[0] if recorded else ("", "")
        expected = gui.autostart_command(helper.config_path)
        check('schtasks /Create /TN "%s"' % gui.AUTOSTART_TASK in command and command.rstrip().endswith('/Run /TN "%s"' % gui.AUTOSTART_TASK),
              "it registers the task and starts it")
        check("<BootTrigger>" in xml and html.escape(expected[0], quote=False) in xml and "--log" in xml,
              "the task runs this agent as SYSTEM at startup")
        check(not glob.glob(os.path.join(tempfile.gettempdir(), "distribuild_task_*.xml")), "the task file is removed afterwards")
        gui.autostart_query = lambda: {"command": expected[0], "arguments": subprocess.list2cmdline(expected[1:]),
                                       "log": expected[-1]}
        helper.scan()
        check(pump(lambda: helper.autostart is not None and settled(helper.rescan_button)), "a registered task is noticed")
        check("켜짐" in helper.autostart_status.text() and helper.autostart_on_button.text().startswith("설정 적용해 다시 시작")
              and helper.autostart_off_button.isEnabled(), "it shows as on, with restart and off buttons")
        gui.autostart_query = lambda: {"command": r"D:\old\DistriBuildAgent.exe", "arguments": "--config x", "log": r"D:\old\a.log"}
        helper.scan()
        pump(lambda: helper.autostart["command"].startswith("D:") and settled(helper.rescan_button))
        check(helper.autostart_on_button.text().startswith("이 위치로 다시 등록"), "a task left by a moved package is pointed out")
        recorded.clear()
        helper.disable_autostart()
        check(pump(lambda: bool(recorded) and settled(helper.autostart_off_button), 30), "turning it off asks for UAC once")
        command = recorded[0][0] if recorded else ""
        script = base64.b64decode(command.split("-EncodedCommand ")[-1].split()[0]).decode("utf-16-le") if recorded else ""
        check('schtasks /Delete /TN "%s"' % gui.AUTOSTART_TASK in command and r"D:\old\a.log" in script
              and expected[-1] in script, "it deletes the task and ends both the old and the current agent")

        print("Developer and helper on one PC, in one step:")
        both = window.both
        gui.autostart_query = lambda: None
        helper.scan()
        pump(lambda: helper.autostart is None and settled(helper.rescan_button))
        recorded.clear()
        both.token.setText("team-token-2")
        both.set_up()
        check(pump(lambda: bool(recorded) and settled(both.setup_button), 30), "everything asks for UAC once")
        command = recorded[0][0] if recorded else ""
        check("install_client.bat" in command and "netsh advfirewall firewall add rule" in command
              and 'schtasks /Create /TN "%s"' % gui.AUTOSTART_TASK in command, "that one prompt installs, opens the firewall and adds the task")
        check(command.index("install_client.bat") < command.index("netsh") < command.index("schtasks /Create") if recorded else False,
              "in that order, each step only if the one before worked")
        check(gui.read_config(helper.config_path, lambda line: None).get("token") == "team-token-2"
              and xgconsole.load_config()["token"] == "team-token-2", "the token is saved for both roles")
        gui.autostart_query = lambda: {"command": expected[0], "arguments": subprocess.list2cmdline(expected[1:]),
                                       "log": expected[-1]}
        helper.scan()
        pump(lambda: helper.autostart is not None and settled(helper.rescan_button))
        check("자동 시작" in both.state_labels["autostart"].text() and "꺼짐" not in both.state_labels["autostart"].text(),
              "the page shows what is set up")
        recorded.clear()
        both.remove()
        check(pump(lambda: bool(recorded) and settled(both.remove_button), 30), "removing asks for UAC once")
        command = recorded[0][0] if recorded else ""
        check("uninstall_client.bat" in command and "schtasks /Delete" in command and "netsh advfirewall firewall delete rule" in command,
              "and undoes all three")
    finally:
        gui.run_elevated, gui.autostart_query = real_run_elevated, real_query
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
