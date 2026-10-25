"""Live end-to-end evaluation: the three things replay cannot measure.

    detection      fault injected -> Prometheus fires -> AIRA holds the incident
    remediation    did it propose the action that actually fixes this fault
    recovery       action executed -> the alert stops firing

Replay cannot answer the last two. The remediation agent validates its proposal against the
system as it is now, and a replay runs against a healed system where every flag is already off -
so a perfectly correct "reset paymentFailure" is refused as unnecessary. The only way to ask
"would you fix this?" honestly is to break something first.

SAFETY. This script executes real actions against the running stack, so what it may approve is
constrained before it starts, not decided as it goes:

  * It approves a proposal ONLY when the action, the target service and the flag all match the
    ground truth frozen in ground_truth.json for the scenario it just injected.
  * Anything else is REJECTED through the same human endpoint, never executed, and recorded as a
    wrong proposal. An arm that wants to restart Kafka does not get to.
  * Every approvable action is reversible by construction - resetting a fault flag that this
    script set, or restarting a container that this script stopped.
  * It goes through POST /actions/{id}/approve, the same endpoint the console's button calls, so
    nothing here bypasses the authorisation path that Phase D built.

    backend\\.venv\\Scripts\\python.exe scripts\\eval\\run_live.py
    backend\\.venv\\Scripts\\python.exe scripts\\eval\\run_live.py --scenarios payment-failure
    backend\\.venv\\Scripts\\python.exe scripts\\eval\\run_live.py --dry-run   (reject everything)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = "http://localhost:8000"
GT_PATH = Path(__file__).resolve().parent / "ground_truth.json"
OUT_DIR = ROOT / "logs" / "eval"

ALERT_TIMEOUT = 420      # a rule with for: 2m plus evaluation slack
INVESTIGATE_TIMEOUT = 420
VERIFY_TIMEOUT = 480     # error-rate rules need 3m rate + 2m keep_firing_for to clear


def api(path: str, method: str = "GET", body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BACKEND}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def ps(script: str, *args: str) -> str:
    """Run one of the project's own PowerShell helpers, so faults are injected the usual way."""
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(ROOT / "scripts" / script), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return (r.stdout or "") + (r.stderr or "")


def now() -> float:
    return time.time()


def wait_for(predicate, timeout: int, poll: int = 10, label: str = ""):
    """Poll until predicate returns something truthy, or give up. Returns (value, seconds)."""
    t0 = now()
    while now() - t0 < timeout:
        try:
            v = predicate()
            if v:
                return v, now() - t0
        except Exception:  # noqa: BLE001 - a transient API blip must not end the run
            pass
        time.sleep(poll)
    return None, now() - t0


# ---------------------------------------------------------------- the run

def find_incident(alertname: str, service: str, after: float) -> dict | None:
    """The incident this injection produced: right alert, right service, started after we began."""
    for row in api("/incidents"):
        if row["alertname"] != alertname or row["service"] != service:
            continue
        try:
            started = datetime.fromisoformat(row["first_received_at"]).timestamp()
        except Exception:  # noqa: BLE001
            continue
        if started >= after - 30:
            return row
    return None


def concluded(incident_id: int) -> list[dict] | None:
    """The findings, once the investigation has written its conclusion row.

    Read from /findings, not from /incidents/{id}: the single-incident endpoint does not nest the
    agent rows - only the list endpoint does - so `inc["agents"]` is always None there. Polling it
    meant waiting out the full timeout on an investigation that had in fact finished in 34s.
    """
    findings = api(f"/incidents/{incident_id}/findings")
    if any(f["agent"] == "conclusion" and f["status"] == "done" for f in findings):
        return findings
    return None


def pending_action(incident_id: int) -> dict | None:
    for a in api(f"/incidents/{incident_id}/actions"):
        if a["status"] == "proposed":
            return a
    return None


def matches_ground_truth(action: dict, gt: dict) -> tuple[bool, str]:
    """Is this exactly the action the frozen ground truth says fixes this fault?"""
    if action["action"] != gt["correct_action"]:
        return False, f"action {action['action']!r} != {gt['correct_action']!r}"
    if (action.get("target_service") or "").lower() != gt["correct_target"].lower():
        return False, f"target {action.get('target_service')!r} != {gt['correct_target']!r}"
    want_flag = (gt.get("correct_params") or {}).get("flag")
    if want_flag:
        got = ((action.get("params") or {}).get("flag") or "")
        if got.lower() != want_flag.lower():
            return False, f"flag {got!r} != {want_flag!r}"
    return True, "matches ground truth"


def run_scenario(name: str, sc: dict, dry_run: bool) -> dict:
    gt = sc["ground_truth"]
    alertname, service = sc["expected_alerts"][0]
    rec: dict = {"scenario": name, "alertname": alertname, "service": service,
                 "started_at": datetime.now(timezone.utc).isoformat()}

    print(f"\n{'=' * 90}\n{name}: {sc['description']}\n{'=' * 90}")
    print("  clearing any existing faults ...")
    ps("clear-all-faults.ps1")
    time.sleep(20)

    t_inject = now()
    print(f"  injecting ...")
    out = ps("inject-fault.ps1", name)
    if "fault active" not in out:
        rec["error"] = "injection did not report success"
        print(f"  INJECTION FAILED\n{out[-400:]}")
        return rec

    row, secs = wait_for(lambda: find_incident(alertname, service, t_inject),
                         ALERT_TIMEOUT, 10)
    if not row:
        rec["error"] = f"no {alertname}/{service} incident within {ALERT_TIMEOUT}s"
        print(f"  {rec['error']}")
        ps("clear-all-faults.ps1")
        return rec
    rec["incident_id"] = row["id"]
    rec["detection_s"] = round(secs, 1)
    print(f"  incident #{row['id']} after {secs:.0f}s (fault -> alert -> AIRA)")

    agents, secs = wait_for(lambda: concluded(row["id"]), INVESTIGATE_TIMEOUT, 10)
    rec["investigation_s"] = round(secs, 1)
    if not agents:
        rec["error"] = "investigation did not conclude"
        print(f"  {rec['error']} after {secs:.0f}s")
        ps("clear-all-faults.ps1")
        return rec

    rca = [a for a in agents if a["agent"] == "rca" and a["status"] == "done"]
    hyp = ((rca[-1].get("output") or {}).get("hypotheses") or [{}])[0] if rca else {}
    rec["cause"] = hyp.get("statement", "")
    rec["rounds"] = len({a.get("round") or 1 for a in agents if a["agent"] != "conclusion"})
    prior = (rca[-1].get("output") or {}).get("prior_cases") if rca else None
    rec["prior_cases"] = [c["incident_id"] for c in (prior or [])]
    print(f"  concluded in {secs:.0f}s, {rec['rounds']} round(s), "
          f"memory offered {len(rec['prior_cases'])} case(s)")
    print(f"    cause: {rec['cause'][:100]}")

    act, secs = wait_for(lambda: pending_action(row["id"]), 180, 5)
    if not act:
        rec["proposed"] = None
        rec["action_correct"] = False
        print("  no action proposed")
        ps("clear-all-faults.ps1")
        return rec
    rec["proposed"] = {"action": act["action"], "target": act.get("target_service"),
                       "params": act.get("params"), "risk": act.get("risk")}
    ok, why = matches_ground_truth(act, gt)
    rec["action_correct"] = ok
    rec["match_detail"] = why
    print(f"  proposed: {act['action']} -> {act.get('target_service')} ({act.get('risk')} risk)")
    print(f"    {'MATCHES' if ok else 'DOES NOT MATCH'} ground truth: {why}")

    if not ok or dry_run:
        reason = "does not match ground truth" if not ok else "dry run"
        api(f"/actions/{act['id']}/reject", "POST",
            {"by": f"evaluation harness ({reason})"})
        rec["executed"] = False
        print(f"  REJECTED (not executed): {reason}")
        ps("clear-all-faults.ps1")
        return rec

    print("  approving (matches ground truth; reversible) ...")
    t_exec = now()
    api(f"/actions/{act['id']}/approve", "POST", {"by": "evaluation harness (ground-truth match)"},
        timeout=300)
    rec["executed"] = True

    def done():
        """Wait for a settled verdict, not merely for a verification record to exist.

        The record is written as soon as verification starts, with outcome "not_yet" - so
        returning on its presence measured the time to create a row (about two seconds) and
        reported every recovery as unverified. The outcome has to be terminal.
        """
        a = [x for x in api(f"/incidents/{row['id']}/actions") if x["id"] == act["id"]]
        if not a:
            return None
        v = a[0].get("verification") or {}
        return a[0] if v.get("outcome") in ("recovered", "not_recovered") else None

    final, secs = wait_for(done, VERIFY_TIMEOUT, 15)
    rec["recovery_s"] = round(secs, 1)
    if final:
        v = final.get("verification") or {}
        rec["result_ok"] = final.get("result_ok")
        rec["verification"] = v.get("outcome")
        rec["verification_detail"] = v.get("detail")
        print(f"  executed, verification after {secs:.0f}s: {v.get('outcome')} - "
              f"{(v.get('detail') or '')[:90]}")
    else:
        rec["verification"] = "timed_out"
        print(f"  verification did not settle within {VERIFY_TIMEOUT}s")

    print("  clearing fault ...")
    ps("clear-all-faults.ps1")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", nargs="*")
    ap.add_argument("--dry-run", action="store_true",
                    help="investigate and judge the proposal, but reject everything")
    args = ap.parse_args()

    gt_doc = json.loads(GT_PATH.read_text(encoding="utf-8"))
    scenarios = gt_doc["scenarios"]
    names = args.scenarios or list(scenarios)

    print(f"Live evaluation: {len(names)} scenario(s)")
    print("Only a proposal matching the frozen ground truth will be approved; everything else is")
    print("rejected through the same endpoint the console's button uses.")
    if args.dry_run:
        print("DRY RUN: nothing will be executed.")

    results = []
    t0 = now()
    for n in names:
        try:
            results.append(run_scenario(n, scenarios[n], args.dry_run))
        except Exception as e:  # noqa: BLE001 - one bad scenario must not lose the others
            print(f"  SCENARIO FAILED: {type(e).__name__}: {e}")
            results.append({"scenario": n, "error": f"{type(e).__name__}: {e}"})
            ps("clear-all-faults.ps1")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"live-{stamp}.json"
    out.write_text(json.dumps({"generated_at": stamp, "dry_run": args.dry_run,
                               "elapsed_s": round(now() - t0, 1), "results": results},
                              indent=1), encoding="utf-8")

    print(f"\n{'=' * 104}\nLIVE RESULTS (Full AIRA, end to end)\n{'=' * 104}")
    print(f"{'scenario':<26}{'detect s':>10}{'invest s':>10}{'rounds':>8}"
          f"{'proposed':>22}{'right':>7}{'recovery':>10}{'verified':>16}")
    print("-" * 104)
    for r in results:
        p = r.get("proposed")
        print(f"{r['scenario']:<26}"
              f"{r.get('detection_s', '-'):>10}"
              f"{r.get('investigation_s', '-'):>10}"
              f"{r.get('rounds', '-'):>8}"
              f"{(p['action'] if p else 'nothing'):>22}"
              f"{('yes' if r.get('action_correct') else 'no'):>7}"
              f"{r.get('recovery_s', '-'):>10}"
              f"{str(r.get('verification', '-')):>16}")
    ok = sum(1 for r in results if r.get("action_correct"))
    rec = sum(1 for r in results if r.get("verification") == "recovered")
    print("-" * 104)
    print(f"  correct action proposed: {ok}/{len(results)}")
    print(f"  confirmed recovered:     {rec}/{len(results)}")
    print(f"\nWrote {out}  ({now() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
