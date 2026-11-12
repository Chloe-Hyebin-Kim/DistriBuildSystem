#!/usr/bin/env python3
"""Saves the setup window pictures used in README.md, with a throwaway home folder and a real agent.

    pip install PySide6-Essentials
    python capture_screens.py [--out <folder>]

Uses the default ports (31890, 31891), so stop any agent running on this PC first. Nothing is shown
on screen and nothing needs administrator rights. Folders, this PC's name and its addresses are
replaced with the usual ones (C:\\DistriBuild, %USERPROFILE%, 192.168.0.x) before each picture.
"""

import argparse
import json
import os
import re
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "DistriBuild"))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
TOKEN = "team-token"
HELPER = "BUILD-PC-07"
GONE = "192.168.0.23:31890"

parser = argparse.ArgumentParser()
parser.add_argument("--out", default=os.path.join(REPO, "docs", "images"))
args = parser.parse_args()

work = tempfile.mkdtemp(prefix="dbshots")
real_home = os.path.expanduser("~")
os.environ["USERPROFILE"] = os.environ["HOME"] = work
os.environ.pop("DISTRIBUILD_CONFIG", None)
# The agent in the pictures stands for another PC, not this one.
os.environ["DISTRIBUILD_AGENT_MACHINE"] = "screens-helper"
sys.path.insert(0, TOOLS)

import setup_gui as gui  # noqa: E402  (the environment above must be set first)
from common import contacts_path  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QTableWidget  # noqa: E402

app = QApplication([])


def pump(condition, timeout=90):
    end = time.time() + timeout
    while not condition() and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return condition()


def local_addresses():
    found = {"127.0.0.1"}
    try:
        found.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return found


def scrubber():
    """Turns this PC's folders, name and addresses into the ones the README talks about."""
    config_dir = os.path.join(work, ".distribuild")
    pairs = [(os.path.join(config_dir, "agent.json"), r"C:\DistriBuild\agent.json"),
             (config_dir, r"%USERPROFILE%\.distribuild"), (work, "%USERPROFILE%"), (real_home, "%USERPROFILE%"),
             (REPO, "<저장소>"), (socket.gethostname(), "DEV-PC")]
    for address in sorted(local_addresses()):
        pairs.append((address + ":", "192.168.0.21:"))

    def scrub(text):
        for old, new in pairs:
            text = re.sub(re.escape(old), lambda _m: new, text, flags=re.IGNORECASE)
        return text
    return scrub


def scrub_window(window, scrub):
    for label in window.findChildren(QLabel):
        label.setText(scrub(label.text()))
    for edit in window.findChildren(QLineEdit):
        edit.setPlaceholderText(scrub(edit.placeholderText()))
        if edit.echoMode() == QLineEdit.EchoMode.Normal:
            edit.setText(scrub(edit.text()))
    for table in window.findChildren(QTableWidget):
        for r in range(table.rowCount()):
            for c in range(table.columnCount()):
                item = table.item(r, c)
                if item:
                    item.setText(scrub(item.text()))
                    item.setToolTip(scrub(item.toolTip()))


def rewrite_log(window, scrub, keep):
    """Scrubs the log and drops repeated lines and ones keep() refuses, keeping the window's colours."""
    lines = window.log_view.toPlainText().splitlines()
    window.log_view.clear()
    shown = []
    for line in lines:
        line = scrub(line)
        if line.strip() and line not in shown and keep(line) and not line.startswith("connection from"):
            shown.append(line)
            window.append_log(line)


def shoot(window, page, name, scrub, keep=lambda line: True, width=1180, height=880, log_top=False):
    """Grows the window until the tab holding page no longer scrolls, then saves it."""
    window.tabs.setCurrentIndex(window.tabs.indexOf(page))
    scrub_window(window, scrub)
    rewrite_log(window, scrub, keep)
    window.resize(width, height)
    app.processEvents()
    for _ in range(10):
        bars = [area.verticalScrollBar() for area in page.findChildren(gui.QScrollArea)]
        extra = max([bar.maximum() for bar in bars] + [0])
        if not extra:
            break
        window.resize(window.width(), window.height() + extra + 4)
        app.processEvents()
    bar = window.log_view.verticalScrollBar()
    bar.setValue(0 if log_top else bar.maximum())
    app.processEvents()
    path = os.path.join(args.out, name + ".png")
    window.grab().save(path)
    print("saved", path)


def main():
    if gui.port_in_use(gui.DEFAULT_PORT):
        sys.exit("port %d is in use: stop the agent on this PC first" % gui.DEFAULT_PORT)
    os.makedirs(args.out, exist_ok=True)
    config_dir = os.path.join(work, ".distribuild")
    os.makedirs(config_dir)
    with open(os.path.join(config_dir, "agent.json"), "w", encoding="utf-8") as f:
        json.dump({"name": HELPER, "token": TOKEN}, f)
    with open(os.path.join(config_dir, "client.json"), "w", encoding="utf-8") as f:
        json.dump({"agents": [GONE], "token": TOKEN}, f)
    now = time.time()
    with open(contacts_path(), "w", encoding="utf-8") as f:
        json.dump({GONE: {"last_ok": now - 20 * 86400, "failing_since": now - 10 * 86400}}, f)

    window = gui.MainWindow()
    helper, developer = window.helper, window.developer
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.show()
    # Message boxes would otherwise appear on the user's screen.
    gui.QMessageBox.open = lambda box: (box.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen), box.show())

    pump(lambda: helper.offer_table.rowCount() > 0 and helper.rescan_button.isEnabled())
    # The autostart buttons show what this PC has registered; the pictures show a PC that has none.
    helper.autostart = None
    helper.update_autostart_state()
    scrub = scrubber()
    pump(lambda: developer.status is not None and developer.refresh_button.isEnabled())
    window.both.update_state()
    window.log_view.clear()
    shoot(window, window.both, "setup-both", scrub, height=560)
    helper.start_agent()
    if not pump(lambda: gui.port_in_use(gui.DEFAULT_PORT), 60):
        sys.exit("the agent did not start")
    pump(lambda: False, 2)  # let its first log lines arrive
    shoot(window, helper, "setup-helper", scrub, log_top=True,
          keep=lambda line: "헬퍼 PC" not in line and "고정 주소" not in line and not line.lstrip().startswith("->"))
    window.log_view.clear()

    pump(lambda: bool(developer.compiler_paths))
    developer.find_agents()
    pump(lambda: developer.find_button.isEnabled() and any(r["info"] for r in developer.agent_rows), 30)
    found = [i for i, r in enumerate(developer.agent_rows) if r["info"] and not r["pinned"]]
    if found:
        developer.agent_table.selectRow(found[0])
        developer.pin_selected()
        pump(lambda: developer.find_button.isEnabled() and sum(r["pinned"] for r in developer.agent_rows) == 2, 30)
    developer.agent_table.clearSelection()
    pump(lambda: developer.remove_stale_button.isEnabled(), 10)
    developer.agent_table.resizeColumnToContents(3)

    try:
        shoot(window, developer, "setup-developer", scrub, width=1300)
        developer.change_tokens("new-team-token")
        pump(lambda: developer.token_box is not None and developer.token_button.isEnabled(), 60)
        box = developer.token_box
        if box:
            box.setText(scrub(box.text()))
            app.processEvents()
            path = os.path.join(args.out, "setup-token-result.png")
            box.grab().save(path)
            print("saved", path)
            box.close()
        pump(lambda: developer.find_button.isEnabled(), 30)  # the token change looks for helpers again
    finally:
        helper.stop_agent()
        pump(lambda: not gui.port_in_use(gui.DEFAULT_PORT), 20)
        window.close()


if __name__ == "__main__":
    main()
