"""Check that the Triage Agent never writes a number that is not in its input.

Re-runs the agent against incidents already stored in the database (no fault injection needed)
and compares every number in the agent's text against the numbers in the alert + evidence it was
given. Reports any invented figure.

Usage:  python check_agent_numbers.py [incident_id ...]
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import db, triage_agent  # noqa: E402
from app.agents import (adk_base, change_agent, critic_agent, log_agent,  # noqa: E402
                        metrics_agent, rca_agent, remediation_agent)
from pydantic import BaseModel  # noqa: E402

NUM = re.compile(r"\d+(?:\.\d+)?")


def _run_triage(inc: dict):
    source = triage_agent._incident_block(inc)
    result, attempts, seconds = triage_agent.run(inc)
    return source, result, attempts, seconds


def _run_metrics(inc: dict):
    """Re-uses the evidence stored with the incident, so a resolved incident is still checkable
    against the numbers the agent was actually given."""
    ev = inc.get("evidence")
    source = triage_agent._incident_block(inc, evidence_text=(ev or {}).get("text"))
    result, attempts, seconds = metrics_agent.run(inc, evidence=ev)
    return source, result, attempts, seconds


def _run_log(inc: dict):
    ev = log_agent.collect(inc.get("service"), inc["starts_at"])
    source = triage_agent._incident_block(inc, evidence_text=ev.get("text"))
    result, attempts, seconds = log_agent.run(inc, evidence=ev)
    return source, result, attempts, seconds


def _run_change(inc: dict):
    ev = change_agent.collect(inc.get("service"), inc["starts_at"])
    source = triage_agent._incident_block(inc, evidence_text=ev.get("text"))
    result, attempts, seconds = change_agent.run(inc, evidence=ev)
    return source, result, attempts, seconds


def _reasoning_source(inc: dict) -> tuple[str, list[dict]]:
    """The reasoning agents' input: what the investigators concluded, not raw evidence.

    Their numbers therefore have to come from the findings block, which is what is checked here.
    The investigators' own quotes were already verified when they ran.
    """
    findings = [f for f in db.get_findings(inc["id"])
                if f["agent"] not in ("rca", "critic", "conclusion")]
    return adk_base.findings_block(inc, findings), findings


async def _rca(inc: dict, findings: list[dict]):
    """Over A2A when the server is up, in-process when it is not, so this check never silently
    depends on a second process being running. Probed inside the same loop as the call: the A2A
    client belongs to one event loop."""
    local = not await adk_base.a2a_available()
    return await rca_agent.run(inc, findings, local=local)


def _run_rca(inc: dict):
    source, findings = _reasoning_source(inc)
    result, attempts, seconds = asyncio.run(_rca(inc, findings))
    return source, result, attempts, seconds


def _run_critic(inc: dict):
    source, findings = _reasoning_source(inc)
    stored = next((f.get("output") for f in db.get_findings(inc["id"])
                   if f["agent"] == "rca" and f["status"] == "done" and f.get("output")), None)
    if stored is None:
        rca, _, _ = asyncio.run(_rca(inc, findings))
        stored = rca.model_dump()
    source += "\n\n" + critic_agent._rca_block(stored)
    result, attempts, seconds = critic_agent.run(inc, findings, stored)
    return source, result, attempts, seconds


def _run_remediation(inc: dict):
    """The remediation agent reasons over the findings and the RCA, so that is its source.

    It proposes only; nothing it returns is executed here. Its numbers matter for the same reason
    the others' do: a proposal quoting a measurement that was never taken would be a fabricated
    justification for changing a running system.
    """
    source, findings = _reasoning_source(inc)
    by = {f["agent"]: f for f in db.get_findings(inc["id"])}
    rca = (by.get("rca") or {}).get("output")
    critic = (by.get("critic") or {}).get("output")
    if rca:
        source += "\n\n" + critic_agent._rca_block(rca)
    result, attempts, seconds = remediation_agent.run(inc, findings, rca, critic)
    return source, result, attempts, seconds


AGENTS = {"triage": _run_triage, "metrics": _run_metrics, "log": _run_log, "change": _run_change,
          "rca": _run_rca, "critic": _run_critic, "remediation": _run_remediation}

# Fields whose value an agent must copy verbatim from its evidence. These carry text the agent
# could otherwise fabricate outright - a log line that was never written, a change that never
# happened - so they are compared against the source.
#
# "measurement" is deliberately excluded: it is a reference to a metric, not a quotation, and the
# agent legitimately shortens verbose labels ("Container CPU (1m)" for "Container CPU (1m, 100% =
# one core)"). The numbers inside it are already validated by the invented-number check above,
# which is what actually matters.
QUOTE_FIELDS = ("quoted_line", "detail")


def check_quotes(result: BaseModel, source: str) -> list[str]:
    """A fabricated log line is this agent's main failure mode, so verify each quote really appears
    in the evidence. Compared on collapsed whitespace, ignoring any trailing truncation."""
    d = result.model_dump()
    if "findings" not in d:
        return []
    norm = " ".join(source.split()).lower()
    bad = []
    for f in d["findings"]:
        for field in QUOTE_FIELDS:
            q = str(f.get(field, "")).strip()
            if not q or q.lower() in ("none", "n/a", "-"):
                continue
            # An agent may cite several evidence lines in one field to justify a conclusion, e.g.
            # quoting both the container status and the flag state to support "no changes". Each
            # line is verified on its own: that still catches a fabricated line, while not failing
            # an answer whose every line is real.
            for part in (p for p in q.splitlines() if p.strip()):
                needle = " ".join(part.split()).lower().rstrip(". ")
                if len(needle) < 12:
                    continue          # too short to verify meaningfully
                if needle[:80] not in norm:
                    bad.append(f"{field}={part.strip()[:140]}")
    return bad


def all_text(model: BaseModel) -> str:
    """Every string anywhere in an agent's result, whatever its schema."""
    out: list[str] = []

    def walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(model.model_dump())
    return " ".join(out)


def headline(model: BaseModel) -> str:
    d = model.model_dump()
    if "summary" in d:
        return str(d["summary"])
    if "findings" in d:
        return " | ".join(str(f.get("statement", f)) for f in d["findings"])
    if "verdict" in d:
        return f"{d['verdict']}: {d.get('reasoning', '')}"
    if "action" in d:
        return (f"{d['action']} on {d.get('target_service')}"
                f"{' flag=' + d['flag'] if d.get('flag') else ''}: {d.get('reasoning', '')}")
    return str(d)[:160]


def numbers(text: str) -> set[str]:
    """Numbers as written, plus a normalised form so 70 and 70.0 compare equal."""
    out = set()
    for n in NUM.findall(text):
        out.add(n)
        out.add(str(float(n)))
        if float(n).is_integer():
            out.add(str(int(float(n))))
    return out


def check(incident_id: int, agent: str = "triage") -> bool:
    inc = db.get(incident_id)
    if not inc or not inc.get("evidence_text"):
        print(f"#{incident_id}: no stored evidence, skipped")
        return True

    runner = AGENTS[agent]
    source, result, attempts, seconds = runner(inc)
    allowed = numbers(source)
    text = all_text(result)

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

    fabricated_quotes = check_quotes(result, source)
    ok = not invented and not fabricated_quotes
    status = "OK  " if ok and not rounded else ("ROUND" if ok else "FAIL")
    print(f"{status} #{incident_id} [{agent}] {inc['alertname']}/{inc['service']}  {seconds:.1f}s  attempts={attempts}")
    print(f"      {headline(result)}")
    if rounded:
        print(f"      rounded (still true): {', '.join(rounded)}")
    if invented:
        print(f"      INVENTED NUMBERS: {', '.join(invented)}")
    if fabricated_quotes:
        print(f"      QUOTE NOT IN EVIDENCE: {' | '.join(fabricated_quotes)}")
    return ok


def main(argv):
    args = argv[1:]
    agents = [a.split("=", 1)[1] for a in args if a.startswith("--agent=")] or list(AGENTS)
    ids = [int(a) for a in args if not a.startswith("--")] or [
        i["id"] for i in db.list_incidents(100) if i.get("evidence_text")
    ][:6]
    results = [check(i, agent) for agent in agents for i in ids]
    print(f"\n{sum(results)}/{len(results)} agent runs produced no invented numbers "
          f"({len(ids)} incident(s) x {len(agents)} agent(s): {', '.join(agents)})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
