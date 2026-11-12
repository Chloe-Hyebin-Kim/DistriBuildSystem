#!/usr/bin/env python3
"""Checks DistriBuild against an installed engine, with the engine's own tools.

  C++      UnrealBuildTool -XGEExport writes the real task file and response files. xgConsole runs
           them through an agent on this machine. Then UBT itself must report the target as up to
           date, and rebuild exactly one file after a header it depends on changes.
  Shaders  The editor cooks a small project with the plugin enabled. Its ShaderCompileWorker jobs go
           through the agent. The project has its own cache graph, so nothing reaches the real cache.

The project goes into a new folder under --root; keep that path short (UE has path length limits).

    python real_engine_check.py --engine "C:\\Program Files\\Epic Games\\UE_5.8" --version 5.8 --root C:\\t [--shaders-minutes 20]
"""

import argparse
import csv
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "DistriBuild"))
# The agent here stands for another PC; builds would otherwise leave this PC's own agent out.
os.environ["DISTRIBUILD_AGENT_MACHINE"] = "real-check-helper"
FAILED = []


def check(ok, text, detail=""):
    print("  %s %s" % ("PASS" if ok else "FAIL", text), flush=True)
    if not ok:
        FAILED.append(text)
        if detail:
            print("       " + str(detail).replace("\n", "\n       ")[-4000:], flush=True)
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_ubt(engine, target, project, extra, log_path):
    env = {k: v for k, v in os.environ.items() if k != "NoDefaultCurrentDirectoryInExePath"}
    bat = os.path.join(engine, "Engine", "Build", "BatchFiles", "Build.bat")
    cmd = 'call "%s" %s Win64 Development -Project="%s" -WaitMutex %s' % (bat, target, project, extra)
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        p = subprocess.run(cmd, shell=True, stdout=log, stderr=subprocess.STDOUT, env=env)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        return p.returncode, f.read()


def export_tasks(engine, target, project, dest, log_path):
    """Runs UBT in export mode and moves the task file out of the engine folder."""
    out_dir = os.path.join(engine, "Engine", "Intermediate", "Build")
    before = set(glob.glob(os.path.join(out_dir, "UBTExport.*.xge.xml")))
    code, text = run_ubt(engine, target, project, "-XGEExport", log_path)
    new = sorted(set(glob.glob(os.path.join(out_dir, "UBTExport.*.xge.xml"))) - before)
    if code != 0 or not new:
        return None, text
    shutil.move(new[-1], dest)
    return dest, text


def run_xgconsole(xgconsole, xml, agent_port, report_dir, log_path):
    os.makedirs(report_dir, exist_ok=True)
    cfg = os.path.join(report_dir, "client.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"agents": ["127.0.0.1:%d" % agent_port], "discovery": False, "max_local": os.cpu_count(), "local_first": False,
                   "report_dir": report_dir, "job_timeout": 1800}, f)
    args = ([sys.executable, xgconsole] if xgconsole.endswith(".py") else [xgconsole]) + [
        xml, "/Rebuild", "/NoWait", "/NoLogo", "/ShowAgent", "/ShowTime", "/Title=UnrealBuildTool Compile", "/UseIdeMonitor"]
    with open(log_path, "wb") as log:
        p = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, DISTRIBUILD_CONFIG=cfg))
    with open(log_path, "rb") as f:
        text = f.read().decode("mbcs", "replace")
    rows = []
    report = os.path.join(report_dir, "last_build.csv")
    if os.path.isfile(report):
        with open(report, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    return p.returncode, text, rows


def summarize(rows):
    remote = [r for r in rows if r["ran_on"] not in ("local", "not run")]
    local = [r for r in rows if r["ran_on"] == "local"]
    reasons = {}
    for r in local:
        key = r["reason_local"][:70]
        reasons[key] = reasons.get(key, 0) + 1
    print("    %d remote, %d local" % (len(remote), len(local)))
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("      %4d  %s" % (n, reason))
    if remote:
        seconds = sorted(float(r["seconds"]) for r in remote)
        print("    remote compile times: median %.1fs, max %.1fs" % (seconds[len(seconds) // 2], seconds[-1]))
    return remote, local


def test_cpp(args, project, name, agent_port, root):
    print("[C++ through UBT's real task file]", flush=True)
    target = name + "Editor"
    xml, text = export_tasks(args.engine, target, project, os.path.join(root, "tasks1.xml"), os.path.join(root, "ubt_export1.log"))
    if not check(xml is not None, "UBT -XGEExport wrote a task file", text):
        return False
    with open(xml, encoding="utf-8", errors="replace") as f:
        print("    task file: %d tools" % f.read().count("<Tool "), flush=True)

    code, text, rows = run_xgconsole(args.xgconsole, xml, agent_port, os.path.join(root, "report1"), os.path.join(root, "xg1.log"))
    remote, _ = summarize(rows)
    check(code == 0, "xgConsole built the whole target", text)
    check(len(remote) > 0, "compiles ran on the agent")
    dlls = glob.glob(os.path.join(root, "Binaries", "Win64", "*%s*.dll" % name))
    check(len(dlls) > 0, "the project module linked", repr(dlls))
    plugin_dlls = glob.glob(os.path.join(root, "Plugins", "DistriBuild", "Binaries", "Win64", "*DistriBuild.dll"))
    check(len(plugin_dlls) > 0, "the plugin itself linked", repr(plugin_dlls))
    if code != 0:
        return False

    code, text = run_ubt(args.engine, target, project, "", os.path.join(root, "ubt_after.log"))
    check(code == 0 and "up to date" in text.lower(), "UBT reports the target up to date afterwards (dependency files are valid)", text[-3000:])

    header = os.path.join(root, "Source", name, "TestActor0.cpp")
    with open(header, "a", encoding="utf-8") as f:
        f.write("\n// touched\n")
    xml, text = export_tasks(args.engine, target, project, os.path.join(root, "tasks2.xml"), os.path.join(root, "ubt_export2.log"))
    if check(xml is not None, "UBT exports the incremental build", text):
        with open(xml, encoding="utf-8", errors="replace") as f:
            tasks = f.read()
        compiled = re.findall(r'Caption="([^"]*\.cpp)"', tasks) or re.findall(r'OutputPrefix="(?:@action(?:_\d+ )?)?([^"]*\.cpp)"', tasks)
        check(len(compiled) == 1 and "TestActor0" in compiled[0], "a changed source rebuilds only that file", repr(compiled))

    # All sources changed: the PCH is cached on the agent now, so this measures the normal case.
    for path in glob.glob(os.path.join(root, "Source", name, "TestActor*.cpp")):
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n// touched again\n")
    xml, text = export_tasks(args.engine, target, project, os.path.join(root, "tasks3.xml"), os.path.join(root, "ubt_export3.log"))
    if check(xml is not None, "UBT exports the rebuild", text):
        t0 = time.time()
        code, text, rows = run_xgconsole(args.xgconsole, xml, agent_port, os.path.join(root, "report3"), os.path.join(root, "xg3.log"))
        print("    rebuild took %.0fs" % (time.time() - t0), flush=True)
        remote, _ = summarize(rows)
        check(code == 0, "the rebuild succeeds", text)
        check("sent " not in text, "no PCH had to be sent again", text[:2000])
        sources = [r for r in rows if r["task"].endswith(".cpp")]
        check(all(r in remote for r in sources) and sources, "every source ran on the agent", repr(rows))
        code, text = run_ubt(args.engine, target, project, "", os.path.join(root, "ubt_after3.log"))
        check(code == 0 and "up to date" in text.lower(), "UBT reports the target up to date again", text[-3000:])
    return True


def test_shaders(args, project, name, agent_port, root):
    print("[shaders through the real editor, up to %d minutes]" % args.shaders_minutes, flush=True)
    ue5 = args.version.startswith("5")
    exe = os.path.join(args.engine, "Engine", "Binaries", "Win64", "UnrealEditor-Cmd.exe" if ue5 else "UE4Editor-Cmd.exe")
    cmd = [exe, project, "-run=Cook", "-TargetPlatform=" + ("Windows" if ue5 else "WindowsNoEditor"), "-unattended", "-nopause", "-nosplash",
           "-stdout", "-UTF8Output", "-NoLogTimes", "-DistriBuildAgents=127.0.0.1:%d" % agent_port, "-DDC=DistriBuildTestDDC"]
    log_path = os.path.join(root, "cook.log")
    start = time.time()
    with open(log_path, "wb") as log:
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = p.wait(timeout=args.shaders_minutes * 60)
            finished = True
        except subprocess.TimeoutExpired:
            # A cook of engine content can take hours. Enough jobs have been checked by then; end it cleanly-ish.
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            code = p.wait()
            finished = False
    with open(log_path, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    print("    %s after %.0fs, exit %s" % ("finished" if finished else "stopped at the time limit", time.time() - start, code), flush=True)

    using = re.search(r"Using (\S+) for [Ss]hader [Cc]ompilation", text)
    check(using is not None and using.group(1) == "DistriBuild", "the engine picked the DistriBuild controller", using.group(0) if using else text[-2000:])
    check("remote shader slots" in text, "the plugin found the agent's slots")
    for line in re.findall(r".*(?:DistriBuild: \d+ remote shader slots|shader source bundle|DistriBuild: shader jobs).*", text):
        print("    " + line.strip()[:200])
    agent_log = os.path.join(root, "agent.log")
    with open(agent_log, "rb") as f:
        agent_text = f.read().decode("mbcs", "replace")
    lines = [l for l in agent_text.splitlines() if " shader " in l and "exit=" in l]
    bad = [l for l in lines if "exit=0" not in l]
    print("    agent ran %d shader batches, %d failed" % (len(lines), len(bad)), flush=True)
    check(len(lines) > 0, "the agent ran real ShaderCompileWorker batches", agent_text[-2000:])
    check(not bad, "every batch exited 0", "\n".join(bad[:10]) + "\n" + "\n".join(l for l in agent_text.splitlines() if "rejected" in l)[:1000])
    if finished:
        check(code == 0, "the cook succeeded", text[-3000:])
    errors = re.findall(r".*(?:Shader debug info|Failed to compile|Rescheduling shader compilation|Error: .*[Ss]hader).*", text)
    check(not errors, "no shader compile errors or local rescheduling in the log", "\n".join(errors[:15]))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--version", required=True, choices=["4.26", "5.8"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--xgconsole", default=os.path.join(TOOLS, "xgconsole.py"))
    ap.add_argument("--shaders-minutes", type=int, default=0)
    ap.add_argument("--skip-cpp", action="store_true", help="reuse a project already built under --root")
    args = ap.parse_args()

    name = "P" + args.version.replace(".", "")
    root = os.path.join(args.root, name)
    project = os.path.join(root, name + ".uproject")
    if not args.skip_cpp:
        shutil.rmtree(root, ignore_errors=True)
        subprocess.run([sys.executable, os.path.join(HERE, "make_test_project.py"), root, name, args.version], check=True)

    port = free_port()
    cfg = os.path.join(root, "agent.json")
    with open(cfg, "w") as f:
        json.dump({"name": "real-check", "port": port, "discovery_port": free_port(), "engines": [args.engine], "auto_detect": True,
                   "work_dir": os.path.join(args.root, "w"), "job_timeout": 1800}, f)
    # The agent gets none of the build environment, as on a helper PC.
    agent_env = {k: v for k, v in os.environ.items() if k not in ("INCLUDE", "LIB", "LIBPATH")}
    agent_log = open(os.path.join(root, "agent.log"), "wb")
    agent = subprocess.Popen([sys.executable, "-u", os.path.join(TOOLS, "agent.py"), "--config", cfg], stdout=agent_log, stderr=subprocess.STDOUT, env=agent_env)
    try:
        for _ in range(300):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        ok = True
        if not args.skip_cpp:
            ok = test_cpp(args, project, name, port, root)
        if ok and args.shaders_minutes:
            test_shaders(args, project, name, port, root)
    finally:
        agent.kill()
        agent.wait()
        agent_log.close()

    print()
    if FAILED:
        print("%d FAILED:" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
