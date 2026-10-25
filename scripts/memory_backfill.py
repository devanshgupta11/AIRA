"""Populate incident memory from the incidents already in the database.

Run this with the backend stopped, or before memory is wired into it: Qdrant runs embedded and
takes an exclusive lock on its storage directory, so two processes cannot hold it at once.

    backend\\.venv\\Scripts\\python.exe scripts\\memory_backfill.py
    backend\\.venv\\Scripts\\python.exe scripts\\memory_backfill.py --verify
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import db, memory  # noqa: E402


def backfill(limit: int) -> dict:
    ids = [r["id"] for r in db.list_incidents(limit)]
    stored = skipped = 0
    t0 = time.perf_counter()
    for i in ids:
        if memory.remember(i):
            stored += 1
        else:
            skipped += 1
    took = time.perf_counter() - t0
    return {"seen": len(ids), "stored": stored, "skipped": skipped, "seconds": round(took, 1)}


def verify() -> int:
    """Prove the store works, on real data, before anything depends on it.

    The test that matters is not "does it return rows" but "does a known incident find itself, and
    does it find the right neighbours" - a store that returns the wrong cases confidently is worse
    than no store at all.
    """
    from qdrant_client import models

    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))

    st = memory.stats()
    print(f"\nStore: {st['stored']} incidents, {st['retrievable']} retrievable "
          f"(Critic-accepted), {st['model']} at {st['dim']} dims")
    print(f"Path:  {st['path']}\n")
    check("the store is not empty", st["stored"] > 0)
    check("some cases are retrievable", st["retrievable"] > 0)

    client = memory._get_client()
    points = client.scroll(COLLECTION := memory.COLLECTION, limit=400, with_payload=True)[0]
    accepted = [p for p in points if (p.payload or {}).get("verdict") == "accept"]
    if not accepted:
        print("  FAIL  no accepted case to test retrieval with")
        return failures + 1

    # 1. A stored incident must retrieve itself by its own symptom text. If this fails, the index
    #    and the query are not speaking the same language.
    target = accepted[0]
    inc = db.get(target.payload["incident_id"])
    res = client.query_points(
        COLLECTION, query=memory.embed([memory.symptom_key(inc)])[0], limit=1, with_payload=True
    )
    top = res.points[0]
    check("a stored incident retrieves itself by its own symptom",
          top.payload["incident_id"] == target.payload["incident_id"] and top.score > 0.98,
          f"got #{top.payload['incident_id']} at {top.score:.4f}")

    # 2. recall() must exclude the incident itself, or an incident investigated twice would cite
    #    its own earlier conclusion back at itself as corroboration.
    neighbours = memory.recall(inc, limit=3)
    check("recall excludes the incident itself",
          all(n["incident_id"] != inc["id"] for n in neighbours),
          f"{len(neighbours)} neighbours")

    # 3. Everything recall returns must be Critic-accepted. The rejected cases are on record but
    #    must never influence a new investigation.
    check("recall returns only Critic-accepted cases",
          all(n["verdict"] == "accept" for n in neighbours))

    # 4. The retrieval has to be better than chance at finding the same fault. This is the real
    #    test: for every accepted case, does its nearest neighbour share its alert type?
    same = diff = 0
    for p in accepted:
        src = db.get(p.payload["incident_id"])
        hits = memory.recall(src, limit=1, min_score=0.0)
        if not hits:
            continue
        if hits[0]["alertname"] == src["alertname"]:
            same += 1
        else:
            diff += 1
    total = same + diff
    rate = same / total if total else 0
    check("nearest neighbour usually shares the alert type", rate >= 0.7,
          f"{same}/{total} = {rate:.0%}")

    # 5. An unlike incident must not match. A store that returns something for everything is a
    #    store that will mislead the RCA on the first novel fault it meets - and a novel fault is
    #    exactly when a wrong hint does the most damage. Several unrelated shapes, not one, so
    #    this is not passing on a single lucky query.
    strangers = [
        {"id": -1, "alertname": "DiskQuotaExceeded", "service": "billing-archiver",
         "summary": "nightly archive job exceeded its storage quota"},
        {"id": -2, "alertname": "CertificateExpiringSoon", "service": "ingress-gateway",
         "summary": "TLS certificate for the public endpoint expires shortly"},
        {"id": -3, "alertname": "BackupJobFailed", "service": "postgres-backup",
         "summary": "scheduled logical backup did not complete"},
    ]
    for q in strangers:
        q["evidence"] = {"values": {}, "failing_operations": []}
        stray = memory.recall(q, limit=3)
        check(f"an unrelated incident ({q['alertname']}) retrieves nothing", not stray,
              f"returned {[(s['incident_id'], s['score']) for s in stray]}" if stray else "")

    # 6. No magnitudes may reach the vector.
    import re
    leaked = [p.payload["incident_id"] for p in points
              if re.search(r"\d+\s*(%|ms|/s)", p.payload.get("symptom", ""))]
    check("no measurement magnitudes reached the embedded text", not leaked,
          f"incidents {leaked[:5]}" if leaked else "")

    return failures


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--verify", action="store_true", help="run checks after storing")
    args = ap.parse_args()

    print(f"Backfilling incident memory from {db.DB_PATH}")
    r = backfill(args.limit)
    print(f"  {r['seen']} incidents seen, {r['stored']} stored, "
          f"{r['skipped']} skipped (no root cause reached), {r['seconds']}s")

    code = 0
    if args.verify:
        print("\nVerifying")
        failures = verify()
        print(f"\n{'all checks passed' if not failures else str(failures) + ' CHECK(S) FAILED'}")
        code = 1 if failures else 0

    memory.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
