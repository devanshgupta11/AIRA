"""Check that the Triage Agent never writes a number that is not in its input.

Re-runs the agent against incidents already stored in the database (no fault injection needed)
and compares every number in the agent's text against the numbers in the alert + evidence it was
given. Reports any invented figure.

Usage:  python check_agent_numbers.py [incident_id ...]
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import db, triage_agent  # noqa: E402

NUM = re.compile(r"\d+(?:\.\d+)?")


def numbers(text: str) -> set[str]:
    """Numbers as written, plus a normalised form so 70 and 70.0 compare equal."""
    out = set()
    for n in NUM.findall(text):
        out.add(n)
        out.add(str(float(n)))
        if float(n).is_integer():
            out.add(str(int(float(n))))
    return out


def check(incident_id: int) -> bool:
    inc = db.get(incident_id)
    if not inc or not inc.get("evidence_text"):
        print(f"#{incident_id}: no stored evidence, skipped")
        return True

    source = triage_agent._incident_block(inc)
    allowed = numbers(source)

    result, attempts, seconds = triage_agent.run(inc)
    text = " ".join([result.summary, *result.observed_symptoms, *result.suggested_next_investigation])

    source_values = [float(x) for x in NUM.findall(source)]
    invented, rounded = [], []
    for n in NUM.findall(text):
        variants = {n, str(float(n))}
        if float(n).is_integer():
            variants.add(str(int(float(n))))
        if variants & allowed:
            continue
        # A value within 1% of something real is a rounding ("over 58%" for 58.54%), which is
        # imprecise but not false. Anything else is fabricated, e.g. 80% from blending 70 and 90.
        v = float(n)
        if any(abs(v - s) <= max(abs(s) * 0.01, 0.05) for s in source_values):
            rounded.append(n)
        else:
            invented.append(n)

    ok = not invented
    status = "OK  " if ok and not rounded else ("ROUND" if ok else "FAIL")
    print(f"{status} #{incident_id} {inc['alertname']}/{inc['service']}  {seconds:.1f}s  attempts={attempts}")
    print(f"      {result.summary}")
    if rounded:
        print(f"      rounded (still true): {', '.join(rounded)}")
    if invented:
        print(f"      INVENTED NUMBERS: {', '.join(invented)}")
    return ok


def main(argv):
    ids = [int(a) for a in argv[1:]] or [
        i["id"] for i in db.list_incidents(100) if i.get("evidence_text")
    ][:8]
    results = [check(i) for i in ids]
    print(f"\n{sum(results)}/{len(results)} incidents produced no invented numbers")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
