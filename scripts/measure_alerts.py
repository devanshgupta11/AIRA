"""Inject real faults one at a time and measure how long AIRA's alerts take to fire and resolve.

For each scenario (scripts/scenarios.json):
  1. wait until none of its expected alerts are active,
  2. inject the fault (flag via flagctl.py, or docker stop),
  3. poll Prometheus (/api/v1/alerts) and Alertmanager (/api/v2/alerts) every 2s and record
     when each expected alert becomes pending, firing, and visible in Alertmanager,
  4. clear the fault and record when the expected alerts disappear from Prometheus.
Every other AIRA alert seen during the run is listed as a side effect (e.g. cascades).

Usage: python measure_alerts.py <scenario> [<scenario> ...]
Results are appended as JSON lines to docs/measurements/alert-timings.jsonl.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "docs", "measurements", "alert-timings.jsonl")
FIRE_TIMEOUT = 420
RESOLVE_TIMEOUT = 420


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def prom_alerts():
    return {(a["labels"]["alertname"], a["labels"].get("service_name", "")): a["state"]
            for a in get("http://localhost:9090/api/v1/alerts")["data"]["alerts"]}


def am_alerts():
    return {(a["labels"]["alertname"], a["labels"].get("service_name", ""))
            for a in get("http://localhost:9093/api/v2/alerts?active=true&silenced=false&inhibited=false")}


def now():
    return datetime.now().strftime("%H:%M:%S")


def run(cmd):
    print("  $", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("   ", (r.stdout + r.stderr).strip().replace("\n", "\n    "), flush=True)
    if r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}")


def inject(sc):
    if "flag" in sc:
        run([sys.executable, os.path.join(HERE, "flagctl.py"), "set", sc["flag"], sc["on"]])
    else:
        run(["docker", "stop", sc["container"]])


def clear(sc):
    if "flag" in sc:
        run([sys.executable, os.path.join(HERE, "flagctl.py"), "set", sc["flag"], "off"])
    else:
        run(["docker", "start", sc["container"]])


def measure(name, sc):
    expect = [tuple(e) for e in sc["expect"]]
    print(f"\n=== {name}: {sc['description']}", flush=True)

    t_wait = time.time()
    while any(k in prom_alerts() for k in expect):
        if time.time() - t_wait > 300:
            raise SystemExit("expected alerts still active from a previous run; aborting")
        time.sleep(5)

    before = set(prom_alerts())
    t0 = time.time()
    inject(sc)
    print(f"  injected at {now()}", flush=True)

    pending, firing, in_am, side = {}, {}, {}, {}
    while time.time() - t0 < FIRE_TIMEOUT:
        p, a = prom_alerts(), am_alerts()
        el = round(time.time() - t0)
        for k, state in p.items():
            if k in expect:
                if k not in pending:
                    pending[k] = el
                if state == "firing" and k not in firing:
                    firing[k] = el
                    print(f"  FIRING {k[0]}/{k[1]} after {el}s ({now()})", flush=True)
            elif k not in before and k not in side:
                side[k] = (state, el)
                print(f"  side effect: {k[0]}/{k[1]} {state} at {el}s", flush=True)
            elif k in side and state == "firing" and side[k][0] != "firing":
                side[k] = ("firing", el)
        for k in a:
            if k in expect and k not in in_am:
                in_am[k] = el
        if expect and all(k in firing and k in in_am for k in expect):
            break
        if not expect and el >= 240:
            break
        time.sleep(2)

    t1 = time.time()
    clear(sc)
    print(f"  cleared at {now()}", flush=True)
    resolved = {}
    while time.time() - t1 < RESOLVE_TIMEOUT and len(resolved) < len([k for k in expect if k in firing]):
        p = prom_alerts()
        for k in expect:
            if k in firing and k not in p and k not in resolved:
                resolved[k] = round(time.time() - t1)
                print(f"  RESOLVED {k[0]}/{k[1]} {resolved[k]}s after clearing ({now()})", flush=True)
        time.sleep(2)

    result = {
        "scenario": name,
        "date": datetime.now().isoformat(timespec="seconds"),
        "expected": [
            {
                "alert": k[0], "service": k[1],
                "pending_s": pending.get(k), "firing_s": firing.get(k),
                "in_alertmanager_s": in_am.get(k), "resolved_after_clear_s": resolved.get(k),
            } for k in expect
        ],
        "side_effects": [{"alert": k[0], "service": k[1], "state": v[0], "at_s": v[1]} for k, v in side.items()],
        "fired_all_expected": all(k in firing for k in expect),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")
    print("  result:", json.dumps(result), flush=True)
    return result["fired_all_expected"]


def main(argv):
    with open(os.path.join(HERE, "scenarios.json"), encoding="utf-8") as f:
        scenarios = json.load(f)
    names = argv[1:] or [n for n, s in scenarios.items() if s["expect"]]
    ok = True
    for n in names:
        ok &= measure(n, scenarios[n])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
