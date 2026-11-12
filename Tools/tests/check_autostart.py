#!/usr/bin/env python3
"""Checks the helper's "start with Windows" task without administrator rights.

    python check_autostart.py [--exe]

--exe runs dist/DistriBuild.exe agent (build it first) instead of agent.py, as a copied package does.

Registers a real Task Scheduler task under a test name, but for the current user instead of SYSTEM
and without the boot trigger (both need elevation). Everything else is the XML the setup window
registers: the task starts the agent, the agent writes its log, and the stop step ends it with its
children. The task is deleted afterwards. Windows only.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "DistriBuild"))
sys.path.insert(0, TOOLS)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import setup_gui as gui  # noqa: E402

PORT, DISCOVERY = 31992, 31993
TASK = "DistriBuild Agent check %d" % os.getpid()
failures = []


def check(ok, text):
    print("  [%s] %s" % ("ok" if ok else "!!", text))
    if not ok:
        failures.append(text)


def wait(condition, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.25)
    return condition()


def as_test_task(xml):
    """The real task, changed only where an unelevated user may not register it."""
    xml = re.sub(r"\s*<SecurityDescriptor>.*?</SecurityDescriptor>", "", xml)
    xml = re.sub(r"\s*<BootTrigger>.*?</BootTrigger>", "", xml)
    xml = re.sub(r"\s*<LogonTrigger>.*?</LogonTrigger>", "", xml)
    return xml.replace("<UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>",
                       "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>")


def schtasks(*args):
    result = subprocess.run(["schtasks"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode, gui.decode_output(result.stdout)


def main():
    work = tempfile.mkdtemp(prefix="dbauto a ")  # a space, as in a real "Program Files" style path
    gui.AUTOSTART_TASK = TASK
    if "--exe" in sys.argv:
        gui.FROZEN, gui.APP_DIR = True, os.path.join(TOOLS, "dist")
        if not os.path.isfile(os.path.join(gui.APP_DIR, gui.PROGRAM)):
            sys.exit("Build dist/DistriBuild.exe first (build.bat).")
    try:
        config = os.path.join(work, "agent.json")
        with open(config, "w", encoding="utf-8") as f:
            json.dump({"name": "autostart-test", "port": PORT, "discovery_port": DISCOVERY, "token": "t",
                       "auto_detect": False, "work_dir": os.path.join(work, "work")}, f)
        command = gui.autostart_command(config)
        log_path = gui.autostart_log_path(config)

        print("Task definition:")
        xml = gui.autostart_task_xml(command, gui.APP_DIR)
        check("<BootTrigger>" in xml and "S-1-5-18" in xml and "<ExecutionTimeLimit>PT0S" in xml,
              "it runs as SYSTEM at startup, with no time limit")
        xml_path = os.path.join(work, "task.xml")
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(as_test_task(xml))
        code, output = schtasks("/Create", "/TN", TASK, "/XML", xml_path, "/F")
        check(code == 0, "Task Scheduler accepts it (%s)" % output.strip())

        task = gui.autostart_query()
        check(task is not None and gui.autostart_matches(task, command), "the window reads it back as its own")
        check(task is not None and os.path.normcase(task["log"] or "") == os.path.normcase(log_path),
              "the log path is found in its arguments")
        moved = command[:1] + ["--config", os.path.join(work, "elsewhere.json")] + command[3:]
        check(not gui.autostart_matches(task, moved), "a task for another folder is told apart")

        print("Running it:")
        code, output = schtasks("/Run", "/TN", TASK)
        check(code == 0, "it starts on demand")
        check(wait(lambda: gui.port_in_use(PORT), 60), "the agent listens")
        info, error = gui.probe_agent("127.0.0.1", PORT, "t")
        check(bool(info) and info["name"] == "autostart-test", "it uses the saved settings (%s)" % (error or "reply received"))
        check(wait(lambda: os.path.isfile(log_path) and "autostart-test" in open(log_path, encoding="utf-8").read(), 10),
              "it writes its log file")

        print("A second agent on the same port:")
        second = subprocess.run(command[:-2] + ["--log", log_path + ".second"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=120)
        text = open(log_path + ".second", encoding="utf-8").read() if os.path.isfile(log_path + ".second") else ""
        check(second.returncode == 2 and "cannot listen on port %d" % PORT in text, "is refused, and says why")

        print("Stopping it:")
        stop = subprocess.run(["cmd", "/d", "/s", "/c", gui.stop_processes_command([log_path])],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        check(stop.returncode == 0, "the stop step succeeds")
        check(wait(lambda: not gui.port_in_use(PORT), 20), "the agent is gone")
        left = subprocess.run(["powershell", "-NoProfile", "-Command",
                               "@(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*%s*' }).Count"
                               % log_path.replace("'", "''")], stdout=subprocess.PIPE).stdout.decode().strip()
        check(left == "0", "no process of it is left (%s)" % left)
    finally:
        schtasks("/Delete", "/TN", TASK, "/F")
        check(gui.autostart_query() is None, "the test task is deleted")
        time.sleep(0.5)
        shutil.rmtree(work, ignore_errors=True)


if sys.platform != "win32":
    sys.exit("Windows only.")
main()
print()
if failures:
    print("FAILED:")
    for item in failures:
        print("  - " + item)
    sys.exit(1)
print("All auto-start checks passed.")
