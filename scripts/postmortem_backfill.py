"""Write post-incident reports for incidents that concluded before the feature existed.

The report is written automatically from now on, at every point an incident ends. This fills in
the ones that are already over, newest first, so the console has something to show for the work
that was already done.

    backend\\.venv\\Scripts\\python.exe scripts\\postmortem_backfill.py --limit 5
    backend\\.venv\\Scripts\\python.exe scripts\\postmortem_backfill.py --limit 5 --dry-run

Run it with the backend up: it writes through the same orchestrator path the live system uses,
and each report costs one local model call of a few seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import db, postmortem  # noqa: E402
from app.agents import orchestrator  # noqa: E402


def candidates(limit: int) -> list[dict]:
    """Concluded incidents with no report yet, newest first.

    "Concluded" means an investigation actually reached a conclusion. An incident that errored
    out or never ran has nothing to write a postmortem about, and inventing one would be the
    opposite of the point.
    """
    out = []
    for row in db.list_incidents(400):
        findings = db.get_findings(row["id"])
        if not any(f["agent"] == "conclusion" and f["status"] == "done" for f in findings):
            continue
        if any(f["agent"] == postmortem.AGENT_NAME and f["status"] == "done" for f in findings):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


async def main_async(args) -> int:
    rows = candidates(args.limit)
    if not rows:
        print("Every concluded incident already has a report.")
        return 0

    print(f"{len(rows)} concluded incident(s) without a report:\n")
    for r in rows:
        print(f"  #{r['id']:<5} {r['alertname']:<22} {r['service']}")
    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    print()
    written = failed = 0
    for r in rows:
        print(f"  #{r['id']} {r['alertname']}/{r['service']} ... ", end="", flush=True)
        try:
            result = await orchestrator.write_postmortem(r["id"], trigger="backfill")
        except Exception as e:  # noqa: BLE001
            result = None
            print(f"error: {type(e).__name__}: {e}")
        if result:
            written += 1
            print(f"ok  {result.get('title', '')[:60]}")
        elif result is None:
            failed += 1
            print("failed")
    print(f"\n{written} written, {failed} failed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
