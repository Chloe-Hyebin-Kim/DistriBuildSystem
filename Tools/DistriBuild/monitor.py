#!/usr/bin/env python3
"""Lists DistriBuild agents on the network.  python monitor.py [host:port ...] [--watch]"""

import sys
import time

from common import discover_agents, parse_agent_address, request


def collect(static):
    rows = {}
    for host, info in discover_agents():
        rows[(info.get("name") or host, info.get("port"))] = dict(info, host=host)
    for addr in static:
        host, port = parse_agent_address(addr)
        try:
            info, _ = request(host, port, {"type": "info"}, timeout=2)
            rows[(info.get("name") or host, port)] = dict(info, host=host)
        except (OSError, ValueError) as e:
            rows[(host, port)] = {"name": "?", "host": host, "error": str(e)}
    return rows


def show(rows):
    print("%-18s %-22s %-8s %6s %6s %8s %7s  %s" % ("NAME", "ADDRESS", "OS", "SLOTS", "BUSY", "DONE", "FAILED", "ENGINES"))
    for (_, port), i in sorted(rows.items()):
        host = i["host"]
        if "error" in i:
            print("%-18s %-22s offline: %s" % (i["name"], "%s:%s" % (host, port), i["error"]))
            continue
        print("%-18s %-22s %-8s %6d %6d %8d %7d  %s" % (i["name"], "%s:%s" % (host, port), i.get("platform", ""),
              i.get("slots", 0), i.get("active", 0), i.get("completed", 0), i.get("failed", 0),
              ",".join(i.get("engines", [])) or "-"))
    print("total slots: %d" % sum(i.get("slots", 0) for i in rows.values()))


if __name__ == "__main__":
    watch = "--watch" in sys.argv
    static = [a for a in sys.argv[1:] if not a.startswith("--")]
    while True:
        rows = collect(static)
        if watch:
            print("\033[2J\033[H", end="")
        show(rows)
        if not watch:
            break
        time.sleep(2)
