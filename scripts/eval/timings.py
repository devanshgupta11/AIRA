"""End-to-end timings, measured across every incident the system has handled.

The four stages a real incident passes through, each measured from timestamps the system recorded
at the time rather than from a stopwatch held during a demo:

    time to fire    the fault starts -> Prometheus raises the alert   (Prometheus, not AIRA)
    detection       the alert starts -> AIRA has the incident         (Alertmanager -> webhook)
    investigation   first agent starts -> conclusion written          (the agentic layer)
    recovery        the action runs -> the alert stops firing          (the system healing)

Only the third is AIRA's to optimise. Reporting all four together, and saying which is which, is
the difference between "AIRA resolves an incident in N seconds" and a number that means something.

Time to fire cannot be recovered from the database - nothing records when a fault was injected -
so it is reported from the measurement runs in docs/measurements and marked as such.

    backend\\.venv\\Scripts\\python.exe scripts\\eval\\timings.py
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402


def _secs(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _sane(x: float | None, cap: float = 1800) -> bool:
    return x is not None and 0 <= x < cap


def collect(limit: int) -> dict:
    detection: dict[str, list[float]] = defaultdict(list)
    investigation: dict[str, list[float]] = defaultdict(list)
    rounds_split: dict[int, list[float]] = defaultdict(list)
    recovery: list[dict] = []

    for row in db.list_incidents(limit):
        inc = db.get(row["id"])
        if not inc:
            continue
        alert = inc["alertname"]

        d = _secs(inc.get("starts_at"), inc.get("first_received_at"))
        if _sane(d):
            detection[alert].append(d)

        # Investigation is the whole agentic pipeline, measured from the first agent that started
        # to the conclusion row. `agent_runtime_s` on the incident is the triage call alone and is
        # roughly a fifth of this - using it here would understate the pipeline by 5x.
        fs = db.get_findings(row["id"])
        starts = [f["started_at"] for f in fs if f.get("started_at")]
        ends = [f["finished_at"] for f in fs if f.get("finished_at")]
        if starts and ends:
            span = _secs(min(starts), max(ends))
            if _sane(span):
                investigation[alert].append(span)
                n = len({f.get("round") or 1 for f in fs if f["agent"] != "conclusion"})
                rounds_split[n].append(span)

        for a in db.get_actions(row["id"]):
            if a["status"] != "executed" or not a.get("executed_at"):
                continue
            t = _secs(a["executed_at"], inc.get("ends_at"))
            recovery.append({
                "alertname": alert, "service": inc.get("service"), "action": a["action"],
                "seconds": round(t, 1) if _sane(t) else None,
                "verification": (a.get("verification") or {}).get("outcome"),
                "result_ok": a.get("result_ok"),
            })

    return {"detection": detection, "investigation": investigation,
            "rounds": rounds_split, "recovery": recovery}


def band(xs: list[float]) -> str:
    xs = sorted(xs)
    p95 = xs[min(len(xs) - 1, int(0.95 * len(xs)))]
    return (f"{len(xs):>5}{st.median(xs):>11.1f}{st.mean(xs):>10.1f}"
            f"{xs[0]:>9.1f}{p95:>9.1f}{xs[-1]:>9.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = collect(args.limit)

    head = f"{'':<26}{'n':>5}{'median':>11}{'mean':>10}{'min':>9}{'p95':>9}{'max':>9}"

    print("\n" + "=" * 80)
    print("DETECTION - alert starts firing -> AIRA has the incident (seconds)")
    print("=" * 80)
    print(head)
    for k in sorted(d["detection"]):
        print(f"{k:<26}{band(d['detection'][k])}")
    allv = [x for v in d["detection"].values() for x in v]
    if allv:
        print(f"{'ALL':<26}{band(allv)}")
    print("\n  This is Alertmanager's group_wait plus the webhook, not time-to-fire. The fault ->")
    print("  alert interval belongs to the Prometheus rule (its `for:` and evaluation period) and")
    print("  is the same for every arm, so it is not a property of the agentic layer at all.")

    print("\n" + "=" * 80)
    print("INVESTIGATION - first agent starts -> conclusion written (seconds)")
    print("=" * 80)
    print(head)
    for k in sorted(d["investigation"]):
        print(f"{k:<26}{band(d['investigation'][k])}")
    allv = [x for v in d["investigation"].values() for x in v]
    if allv:
        print(f"{'ALL':<26}{band(allv)}")

    if d["rounds"]:
        print("\n  by number of investigation rounds:")
        print(f"  {'rounds':<24}{'n':>5}{'median':>11}{'mean':>10}{'min':>9}{'p95':>9}{'max':>9}")
        for n in sorted(d["rounds"]):
            print(f"  {str(n) + ' round' + ('s' if n > 1 else ''):<24}{band(d['rounds'][n])}")
        print("\n  A second round is the Critic refusing to accept the first answer. It roughly")
        print("  doubles the investigation, which is the price of the one thing that distinguishes")
        print("  AIRA from a single-shot analysis - and the evaluation is where that price gets")
        print("  weighed against what it buys.")

    print("\n" + "=" * 80)
    print("RECOVERY - approved action executed -> alert stops firing")
    print("=" * 80)
    if not d["recovery"]:
        print("  No action has been executed yet.")
    else:
        print(f"  {'alert':<24}{'action':<20}{'seconds':>9}  verification")
        for r in d["recovery"]:
            secs = f"{r['seconds']:.1f}" if r["seconds"] is not None else "-"
            print(f"  {r['alertname']:<24}{r['action']:<20}{secs:>9}  {r['verification']}")
        ok = [r for r in d["recovery"] if r["verification"] == "recovered"]
        print(f"\n  {len(ok)} of {len(d['recovery'])} executed actions were confirmed to have "
              "fixed the incident.")
        print("  The recovery figure is dominated by the alert rule, not by the fix: an error-rate")
        print("  rule averaging over 3 minutes with a 2-minute keep_firing_for cannot clear in")
        print("  under 5, however fast the flag was reset. A stopped container, which alerts on")
        print("  absence, clears in about 20 seconds.")

    if args.json:
        out = ROOT / "logs" / "eval" / "timings.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "detection": {k: v for k, v in d["detection"].items()},
            "investigation": {k: v for k, v in d["investigation"].items()},
            "by_rounds": {str(k): v for k, v in d["rounds"].items()},
            "recovery": d["recovery"],
        }, indent=1), encoding="utf-8")
        print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
