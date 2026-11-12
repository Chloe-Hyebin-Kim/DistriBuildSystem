#!/usr/bin/env python3
"""DistriBuild.exe: one program for every role. build.bat turns this file into dist\\DistriBuild.exe.

    DistriBuild.exe                      the setup window (setup_gui.py)
    DistriBuild.exe agent [options]      a helper PC's agent (agent.py)
    DistriBuild.exe xgconsole [args]     the build front end (xgconsole.py)
    xgConsole.exe [args]                 the same front end: install_client.bat installs a copy under
                                         this name, the one UnrealBuildTool runs

From source, `python distribuild.py ...` works the same way.
"""

import os
import subprocess
import sys

FROZEN = getattr(sys, "frozen", False)
ROLES = ("setup", "agent", "xgconsole")
DETACHED = "DISTRIBUILD_DETACHED"
# Set in the window leave_own_console() started, and kept from that window's own children.
RELAUNCHED = bool(os.environ.pop(DETACHED, None))


def pick_role(argv, program):
    """(role, the arguments left for it). A copy named xgConsole is always the build front end, since
    UnrealBuildTool passes it a task file and switches, never a role."""
    if os.path.splitext(os.path.basename(program))[0].lower() == "xgconsole":
        return "xgconsole", argv[1:]
    if len(argv) > 1 and argv[1].lower() in ROLES:
        return argv[1].lower(), argv[2:]
    return "setup", argv[1:]


def leave_own_console():
    """Double-clicked, a console program opens a console window of its own. The setup window has no
    use for it, so this hides it, starts the window again without one and returns True.

    Run from a terminal, it keeps the terminal, as any program would, and returns False.
    """
    if os.name != "nt" or not FROZEN or RELAUNCHED:
        return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    ids = (ctypes.c_uint * 16)()
    # A one-file exe runs as two processes, its unpacker and Python, both attached to this console.
    # A terminal adds its shell, so more than two means somebody started this from one.
    if kernel32.GetConsoleProcessList(ids, len(ids)) > 2:
        return False
    window = kernel32.GetConsoleWindow()
    if window:
        ctypes.windll.user32.ShowWindow(window, 0)
    # A console without a window, which the unpacked Python inherits. (A detached process would leave
    # that Python to open a console window of its own.) The unpacker also keeps Python in a job that
    # ends with it, and children join that job: the new window leaves it, or it would close with this one.
    no_window, new_group, breakaway = 0x08000000, 0x00000200, 0x01000000
    try:
        subprocess.Popen([sys.executable] + sys.argv[1:], env=dict(os.environ, **{DETACHED: "1"}),
                         creationflags=no_window | new_group | breakaway, close_fds=True)
    except OSError:
        return False
    return True


def main():
    if FROZEN:
        # Without this, a DistriBuild.exe (or xgConsole.exe) started from here takes itself for this one's
        # unpacked Python and runs from its temporary folder, which goes away when this process ends.
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    role, rest = pick_role(sys.argv, sys.executable if FROZEN else sys.argv[0])
    if role == "xgconsole":
        import xgconsole
        return xgconsole.main([sys.argv[0]] + rest)
    sys.argv = [sys.argv[0]] + rest
    if role == "agent":
        import agent
        return agent.main()
    if leave_own_console():
        return 0
    if RELAUNCHED or sys.stderr is None:
        # Without a visible console, an error would otherwise vanish without a trace.
        import faulthandler
        import tempfile
        sys.stdout = sys.stderr = open(os.path.join(tempfile.gettempdir(), "DistriBuild-window.log"), "w",
                                       encoding="utf-8", buffering=1)
        faulthandler.enable(sys.stderr)
    import setup_gui
    return setup_gui.main()


if __name__ == "__main__":
    sys.exit(main())
