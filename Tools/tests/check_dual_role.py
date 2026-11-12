#!/usr/bin/env python3
"""One PC as developer and helper: a build leaves this PC's agent out and pauses it for other PCs.

    python check_dual_role.py

Starts two agents on private ports: "own" as this PC's agent, "other" standing for another PC.
Needs no compiler or administrator rights.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "DistriBuild"))
sys.path.insert(0, TOOLS)

import xgconsole  # noqa: E402
from common import machine_id, recv_frame, request, send_frame  # noqa: E402

OWN_PORT, OTHER_PORT = 31970, 31972
failures = []


def check(ok, text):
    print("  [%s] %s" % ("ok" if ok else "!!", text))
    if not ok:
        failures.append(text)


def wait_for_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def info(port):
    return request("127.0.0.1", port, {"type": "info", "token": ""})[0]


def reserve(port):
    """The reply to a reserve. A granted slot is given back by closing the connection."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        send_frame(s, {"type": "reserve", "token": "", "kind": "compile"})
        return recv_frame(s)[0]


def hold(port, holder, seconds, machine=None):
    return request("127.0.0.1", port, {"type": "hold", "token": "", "machine": machine or machine_id(),
                                       "holder": holder, "seconds": seconds})[0]


def start_agent(work, name, port, machine):
    cfg = os.path.join(work, name + ".json")
    with open(cfg, "w", encoding="utf-8") as f:
        # A fake compiler id is enough: nothing is compiled, and acquire() only matches ids.
        json.dump({"name": name, "port": port, "discovery_port": port + 1, "auto_detect": False,
                   "work_dir": os.path.join(work, name)}, f)
    env = dict(os.environ)
    env.pop("DISTRIBUILD_AGENT_MACHINE", None)
    if machine:
        env["DISTRIBUILD_AGENT_MACHINE"] = machine
    log = open(os.path.join(work, name + ".log"), "w")
    return subprocess.Popen([sys.executable, "-u", os.path.join(TOOLS, "agent.py"), "--config", cfg],
                            stdout=log, stderr=subprocess.STDOUT, env=env), log


def main():
    work = tempfile.mkdtemp(prefix="dbdual")
    own, own_log = start_agent(work, "own-pc", OWN_PORT, None)
    other, other_log = start_agent(work, "other-pc", OTHER_PORT, "another-pc")
    try:
        check(wait_for_port(OWN_PORT) and wait_for_port(OTHER_PORT), "both agents start")
        check(info(OWN_PORT).get("machine") == machine_id(), "an agent reports this PC's id")
        check(info(OTHER_PORT).get("machine") == "another-pc", "the other one reports its own")

        print("A build on this PC:")
        cfg = xgconsole.load_config()
        cfg.update(agents=["127.0.0.1:%d" % OWN_PORT, "127.0.0.1:%d" % OTHER_PORT], discovery=False, token="")
        pool = xgconsole.AgentPool(cfg)
        check(pool.own_names() == ["own-pc"] and "own-pc" not in pool.describe() and "other-pc" in pool.describe(),
              "leaves this PC's agent out of its helpers (%s; own: %s)" % (pool.describe(), pool.own_names()))
        with xgconsole.OwnAgentHold(pool, cfg):
            state = info(OWN_PORT)
            check(state.get("owner_building") and state.get("free") == 0, "pauses this PC's agent while it runs")
            reply = reserve(OWN_PORT)
            check(not reply.get("ok") and reply.get("reason") == "busy" and reply.get("owner_building"),
                  "which then refuses other PCs' jobs, as 'busy' that plugins understand (%s)" % reply)
            check(reserve(OTHER_PORT).get("ok"), "the other PC's agent is not affected")
            check(not hold(OTHER_PORT, "x", 30).get("ok"), "a PC cannot pause another PC's agent")
            state = info(OTHER_PORT)
            check(not state.get("owner_building"), "so that one keeps taking jobs")

            print("Another PC's build meanwhile:")
            real_id = xgconsole.machine_id
            xgconsole.machine_id = lambda: "developer-elsewhere"
            try:
                elsewhere = xgconsole.AgentPool(dict(cfg, agents=["127.0.0.1:%d" % OWN_PORT]))
            finally:
                xgconsole.machine_id = real_id
            helper = next(iter(elsewhere.agents.values()), None)
            check(helper is not None and helper.skip_until > time.time() + 5,
                  "sees the paused helper and leaves it alone for a while")
            if helper:
                helper.compilers = {"msvc-test"}
                helper.skip_until = 0
                got, why = elsewhere.acquire("msvc-test")
                check(got is None and why == "helpers were busy with their own builds",
                      "and if it asks anyway, it is told why (%s)" % why)
        state = info(OWN_PORT)
        check(not state.get("owner_building") and state.get("free") == state.get("slots"),
              "after the build, this PC's agent takes jobs again")
        check(reserve(OWN_PORT).get("ok"), "and grants them")

        print("A build that dies:")
        hold(OWN_PORT, "crashed-build", 1)
        check(info(OWN_PORT).get("owner_building"), "a hold pauses the agent")
        time.sleep(1.5)
        check(not info(OWN_PORT).get("owner_building"), "and runs out on its own if it is not renewed")
        hold(OWN_PORT, "a", 30)
        hold(OWN_PORT, "b", 30)
        hold(OWN_PORT, "a", 0)
        check(info(OWN_PORT).get("owner_building"), "two builds at once: the agent waits for both")
        hold(OWN_PORT, "b", 0)
        check(not info(OWN_PORT).get("owner_building"), "and resumes when the last one ends")
    finally:
        for p in (own, other):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)] if os.name == "nt" else ["kill", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(10)
        own_log.close()
        other_log.close()
    with open(os.path.join(work, "own-pc.log"), encoding="utf-8", errors="replace") as f:
        text = f.read()
    check("this PC is building" in text and "taking jobs from other PCs again" in text, "the agent logs when it pauses and resumes")

    print()
    if failures:
        print("FAILED:")
        for item in failures:
            print("  - " + item)
        return 1
    print("All dual-role checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
