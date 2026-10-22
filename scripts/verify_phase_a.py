"""End-to-end verification of the multi-agent investigation.

Injects real faults one at a time, waits for the incident, and records what the Orchestrator planned,
which agents ran or were skipped, and how long each took. Results are appended to
docs/measurements/phase-a-runs.jsonl.

Usage: python verify_phase_a.py [scenario ...]      (default: high-cpu, payment-failure, service-down)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "docs", "measurements", "phase-a-runs.jsonl")
DEFAULT = ["high-cpu", "payment-failure", "service-down"]
ALERT_TIMEOUT = 420
INVESTIGATION_TIMEOUT = 180


def ps(script: str, *args: str) -> None:
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(HERE, script), *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in (r.stdout or "").splitlines():
        if any(k in line for k in ("VERIFIED", "[OK]", "[FAIL]")):
            print("   ", line.strip())


def incidents() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:8000/incidents?limit=30", timeout=15) as r:
        return json.load(r)


def firing_ids() -> set[int]:
    return {i["id"] for i in incidents() if i["status"] == "firing"}


def wait_for_new_incident(before: set[int], timeout: int) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = [i for i in incidents() if i["id"] not in before and i["status"] == "firing"]
        if new:
            return new
        time.sleep(4)
    return []


def wait_for_investigation(incident_id: int, timeout: int) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        inc = next((i for i in incidents() if i["id"] == incident_id), None)
        agents = (inc or {}).get("agents", {}).get("agents", [])
        if agents and all(a["status"] in ("done", "error", "skipped") for a in agents):
            return inc
        time.sleep(3)
    return None


def wait_quiet(services: set[str], timeout: int = 420) -> bool:
    """Wait until nothing is firing for the services this scenario will affect.

    If an alert for the same fault is still active, Alertmanager treats a re-injection as the same
    ongoing alert - same fingerprint and start time - so no new incident is created and this script
    would wait forever for one. That is correct product behaviour; the harness just has to start
    from a quiet state.

    Only the scenario's own services are checked. An unrelated alert elsewhere must not block the
    run: grafana spikes its CPU spontaneously on this machine, and waiting for the whole system to
    go silent would hang on something that has nothing to do with the fault being tested.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://localhost:9090/api/v1/alerts", timeout=10) as r:
                firing = {a["labels"].get("service_name") for a in json.load(r)["data"]["alerts"]}
            if not (firing & services):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(10)
    return False


def run(scenario: str) -> dict:
    print(f"\n=== {scenario}")
    with open(os.path.join(HERE, "scenarios.json"), encoding="utf-8") as f:
        sc = json.load(f)[scenario]
    # Services this fault alerts on, plus the ones it cascades into: a payment fault also trips
    # checkout, and a lingering checkout alert would block a clean re-injection just as much.
    services = {e[1] for e in sc.get("expect", [])}
    if "payment" in scenario:
        services |= {"payment", "checkout"}
    if services and not wait_quiet(services):
        print(f"    alerts still firing for {', '.join(sorted(services))}; results would be unreliable")
        return {"scenario": scenario, "ok": False, "reason": "alerts still firing before injection"}
    before = firing_ids()
    t0 = time.time()
    ps("inject-fault.ps1", "-Scenario", scenario)

    new = wait_for_new_incident(before, ALERT_TIMEOUT)
    if not new:
        print("    NO INCIDENT within timeout")
        ps("clear-fault.ps1", "-Scenario", scenario)
        return {"scenario": scenario, "ok": False, "reason": "no incident"}
    detect_s = round(time.time() - t0)
    first = new[0]
    print(f"    incident #{first['id']} {first['alertname']}/{first['service']} after {detect_s}s")

    inc = wait_for_investigation(first["id"], INVESTIGATION_TIMEOUT)
    ps("clear-fault.ps1", "-Scenario", scenario)
    if not inc:
        print("    investigation did not finish")
        return {"scenario": scenario, "ok": False, "reason": "investigation timeout", "incident": first["id"]}

    agents = inc["agents"]["agents"]
    result = {
        "scenario": scenario,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "incident": inc["id"], "alert": inc["alertname"], "service": inc["service"],
        "detect_s": detect_s,
        "total_agent_s": inc["agents"]["total_runtime_s"],
        "agents": [{"agent": a["agent"], "status": a["status"], "runtime_s": a["runtime_s"],
                    "attempts": a["attempts"], "skip_reason": a["skip_reason"]} for a in agents],
        "cause_found": [f.get("statement") for a in agents if a.get("output")
                        for f in (a["output"].get("findings") or []) if f.get("likely_cause")],
        "ok": all(a["status"] in ("done", "skipped") for a in agents),
    }
    for a in agents:
        mark = "skipped" if a["status"] == "skipped" else f"{a['runtime_s']}s"
        extra = f"  ({a['skip_reason']})" if a["skip_reason"] else ""
        print(f"      {a['agent']:9} {a['status']:8} {mark}{extra}")
    print(f"    total agent time {result['total_agent_s']}s | likely cause: {result['cause_found'] or 'none identified'}")
    return result


def main(argv):
    scenarios = argv[1:] or DEFAULT
    results = [run(s) for s in scenarios]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n{ok}/{len(results)} scenarios completed with every planned agent finishing")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
