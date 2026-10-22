"""Orchestrator: plans and runs the investigation for one incident.

Planning is deliberate, not a fixed pipeline. Which investigators run depends on the alert's
`aira_signal` label and on what the Triage agent concluded, and every investigator that is NOT run
is recorded with the reason, so the choice is visible rather than implied.

Execution is shaped by the hardware this runs on. All evidence gathering is I/O (Prometheus,
OpenSearch, docker) and runs concurrently at no GPU cost. The reasoning is serialised, because one
local model serves every agent and there is not enough spare VRAM to load a second.

The planning rules are deterministic rather than a further LLM call: the choice must be explainable
to an examiner, repeatable between runs, and must not cost an extra ~7 seconds of generation.
"""

import asyncio
import logging
import time

from . import change_agent, log_agent, metrics_agent
from .. import db
from ..config import AGENT_TIMEOUT_S, OLLAMA_MODEL

log = logging.getLogger("aira.orchestrator")

# Timeline positions; triage is 10 (see main.SEQ_TRIAGE).
SEQ = {"metrics": 30, "log": 40, "change": 50}

INVESTIGATORS = {
    "metrics": metrics_agent,
    "log": log_agent,
    "change": change_agent,
}


def plan(inc: dict, triage: dict | None) -> dict[str, str | None]:
    """Decide which investigators to run. Returns {agent: None to run, or a reason to skip}.

    Rules are driven by the alert signal and the triage category, both of which describe what kind
    of failure this is:

    * metrics  - always useful; it is the only agent that quantifies severity.
    * log      - skipped for a pure CPU alert, where logs rarely explain saturation, and for a
                 stopped service, whose logs end before the incident starts.
    * change   - always useful here, because a feature flag toggle is the usual cause and a flag
                 can be read even when the service is down.
    """
    signal = (inc.get("labels") or {}).get("aira_signal")
    category = (triage or {}).get("likely_cause_category")
    decisions: dict[str, str | None] = {"metrics": None, "log": None, "change": None}

    if signal == "cpu" or category == "resource_saturation":
        decisions["log"] = "CPU saturation is a resource problem; logs rarely explain it"
    elif signal == "availability" or category == "service_unavailable":
        decisions["log"] = "service is not running, so it stopped logging before the incident"

    return decisions


async def _gather(inc: dict, to_run: list[str]) -> dict[str, dict]:
    """Collect every investigator's evidence concurrently. Pure I/O, no GPU, no LLM."""
    svc, started = inc.get("service"), inc["starts_at"]
    jobs = {
        "metrics": lambda: metrics_agent.collect(svc),
        "log": lambda: log_agent.collect(svc, started),
        "change": lambda: change_agent.collect(svc, started),
    }
    names = [n for n in to_run if n in jobs]
    results = await asyncio.gather(*(asyncio.to_thread(jobs[n]) for n in names), return_exceptions=True)
    out = {}
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            log.warning("incident %s: %s evidence failed: %s", inc["id"], name, res)
            out[name] = {"text": f"Evidence collection failed: {res}", "error": str(res)}
        else:
            out[name] = res
    return out


async def investigate(incident_id: int, triage: dict | None) -> dict:
    """Plan, gather and run the investigators for one incident. Returns a summary of what happened."""
    inc = db.get(incident_id)
    decisions = plan(inc, triage)
    to_run = [name for name, skip in decisions.items() if skip is None]

    # Record the whole plan up front, including skips, so the console shows the intended
    # investigation immediately rather than agents appearing one by one.
    for name, skip in decisions.items():
        db.upsert_finding(incident_id, name, seq=SEQ[name],
                          status="skipped" if skip else "pending",
                          skip_reason=skip, model=None if skip else OLLAMA_MODEL)
    log.info("incident %s: plan -> run %s; skip %s", incident_id,
             ", ".join(to_run) or "nothing",
             ", ".join(f"{n} ({r})" for n, r in decisions.items() if r) or "nothing")

    t0 = time.perf_counter()
    evidence = await _gather(inc, to_run)
    gather_s = time.perf_counter() - t0
    log.info("incident %s: evidence for %d agent(s) gathered in %.1fs (parallel)", incident_id, len(to_run), gather_s)

    ran = {}
    for name in to_run:
        agent = INVESTIGATORS[name]
        ev = evidence.get(name, {})
        started = db.now_iso()
        db.upsert_finding(incident_id, name, status="running", started_at=started,
                          evidence=ev, evidence_text=ev.get("text"))
        t1 = time.perf_counter()
        try:
            result, attempts, seconds = await asyncio.wait_for(
                asyncio.to_thread(agent.run, inc, ev), timeout=AGENT_TIMEOUT_S)
            db.upsert_finding(incident_id, name, status="done", output=result.model_dump(),
                              attempts=attempts, finished_at=db.now_iso(), runtime_s=round(seconds, 2))
            ran[name] = {"status": "done", "runtime_s": round(seconds, 2), "attempts": attempts}
            log.info("incident %s: %s agent done in %.1fs (%d attempt(s))", incident_id, name, seconds, attempts)
        except Exception as e:  # noqa: BLE001 - record honestly, keep investigating
            runtime = round(time.perf_counter() - t1, 2)
            message = f"agent did not finish within {AGENT_TIMEOUT_S:.0f}s" if isinstance(e, asyncio.TimeoutError) \
                else f"{type(e).__name__}: {e}"
            db.upsert_finding(incident_id, name, status="error", error=message[:500],
                              finished_at=db.now_iso(), runtime_s=runtime)
            ran[name] = {"status": "error", "runtime_s": runtime, "error": message[:200]}
            log.error("incident %s: %s agent failed: %s", incident_id, name, message[:200])

    total = round(time.perf_counter() - t0, 2)
    log.info("incident %s: investigation finished in %.1fs", incident_id, total)
    return {
        "planned": decisions,
        "ran": ran,
        "evidence_gather_s": round(gather_s, 2),
        "total_s": total,
    }
