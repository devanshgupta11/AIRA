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

from . import (change_agent, critic_agent, log_agent, metrics_agent, postmortem_agent,
               rca_agent, remediation_agent)
from .. import db, events, mcp_client, memory, postmortem, remediation, tracing
from ..config import AGENT_TIMEOUT_S, MEMORY_ENABLED, OLLAMA_MODEL

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


async def _over_mcp(name: str, tool: str, call, direct) -> dict:
    """One tool's evidence over MCP, with the fallback recorded rather than hidden.

    Each tool is reached over the Model Context Protocol, so it is a service with a declared
    interface rather than a function in this process. If that service is down the investigation
    still has to happen, so the direct call is used - but the evidence then carries
    `_mcp.used = False` and the reason, because an investigation that quietly stopped using the
    protocol would look identical to one that never did.
    """
    try:
        return await call()
    except mcp_client.McpUnavailable as e:
        log.warning("incident evidence: %s tool unavailable over MCP, falling back to the direct "
                    "call (%s)", name, e)
        ev = await asyncio.to_thread(direct)
        ev["_mcp"] = {"tool": tool, "used": False, "fallback": "direct call",
                      "error": str(e)[:300]}
        return ev


def _gather_one(name: str, svc: str | None, started: str):
    """The coroutine that collects one investigator's evidence over MCP.

    Named rather than inlined because both the first round and a replanned second round must come
    through exactly the same path: same tool, same validation, same recorded fallback.
    """
    jobs = {
        "metrics": lambda: _over_mcp(
            "metrics", mcp_client.PROMETHEUS_TOOL,
            lambda: mcp_client.collect_prometheus(svc),
            lambda: metrics_agent.collect(svc)),
        "log": lambda: _over_mcp(
            "log", mcp_client.LOG_TOOL,
            lambda: mcp_client.collect_log(svc, started),
            lambda: log_agent.collect(svc, started)),
        "change": lambda: _over_mcp(
            "change", mcp_client.CHANGE_TOOL,
            lambda: mcp_client.collect_change(svc, started),
            lambda: change_agent.collect(svc, started)),
    }
    return jobs[name]()


async def _gather(inc: dict, to_run: list[str]) -> dict[str, dict]:
    """Collect every investigator's evidence concurrently. Pure I/O, no GPU, no LLM.

    All three tools are now reached over MCP, so the jobs are coroutines rather than threads.
    A session per call, deliberately: a shared session multiplexes over one stream and would
    serialise these, which is the opposite of the point - gathering concurrently is what keeps the
    wall-clock cost that of the slowest source rather than their sum.
    """
    svc, started = inc.get("service"), inc["starts_at"]
    names = [n for n in to_run if n in INVESTIGATORS]
    # Each collection is wrapped so its tool-call events are attributed to the agent that will use
    # the evidence, even though all three run concurrently in one task group.
    async def one(name: str):
        with events.investigating(inc["id"], name, phase="evidence"):
            return await _gather_one(name, svc, started)

    results = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
    out = {}
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            log.warning("incident %s: %s evidence failed: %s", inc["id"], name, res)
            out[name] = {"text": f"Evidence collection failed: {res}", "error": str(res)}
        else:
            out[name] = res
    return out


async def investigate(incident_id: int, triage: dict | None) -> dict:
    """Plan, gather and run the investigators for one incident. Returns a summary of what happened.

    Everything below happens inside one span, so the whole investigation is a single trace: every
    agent, every tool call over MCP and the RCA agent's work in its own process all hang off it.
    The trace id is written onto the incident, which is what lets the console offer a link straight
    to it.
    """
    inc = db.get(incident_id)
    with tracing.span("investigation",
                      **{"aira.incident_id": incident_id,
                         "aira.alertname": inc.get("alertname"),
                         "aira.service": inc.get("service"),
                         "aira.severity": inc.get("severity")}):
        trace_id = tracing.current_trace_id()
        if trace_id and inc.get("trace_id") != trace_id:
            db.update(incident_id, trace_id=trace_id)
        return await _investigate(incident_id, inc, triage)


async def _investigate(incident_id: int, inc: dict, triage: dict | None) -> dict:
    decisions = plan(inc, triage)
    to_run = [name for name, skip in decisions.items() if skip is None]

    # Record the whole plan up front, including skips, so the console shows the intended
    # investigation immediately rather than agents appearing one by one.
    for name, skip in decisions.items():
        db.upsert_finding(incident_id, name, seq=SEQ[name],
                          status="skipped" if skip else "pending",
                          skip_reason=skip, model=None if skip else OLLAMA_MODEL)
    with events.investigating(incident_id, "orchestrator"):
        events.record("aira", "investigation_started",
                      detail={"alert": inc["alertname"], "service": inc.get("service"),
                              "severity": inc.get("severity")})
        events.record("aira", "plan_decided",
                      detail={"run": to_run,
                              "skipped": {n: r for n, r in decisions.items() if r}})
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
            # The context is set here, outside to_thread: asyncio.to_thread copies the current
            # context into the worker thread, which is how CrewAI's bus events - fired from inside
            # that thread - find their way to this incident.
            with events.investigating(incident_id, name):
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

    reasoning = await reason(incident_id)
    proposal = await propose_remediation(incident_id)

    # If nothing is going to happen, this is where the incident ends, so write the report now.
    # When an action is awaiting a human it is not over, and the report waits for the outcome.
    if not proposal or proposal.get("action") == "none":
        await write_postmortem(incident_id, trigger="no action proposed")

    with events.investigating(incident_id, "orchestrator"):
        events.record("aira", "investigation_finished", ok=True,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      detail={"ran": list(ran), "rounds": len(reasoning.get("rounds") or []),
                              "accepted": reasoning.get("accepted")})

    total = round(time.perf_counter() - t0, 2)
    log.info("incident %s: investigation finished in %.1fs", incident_id, total)
    return {
        "planned": decisions,
        "ran": ran,
        "evidence_gather_s": round(gather_s, 2),
        "reasoning": reasoning,
        "remediation": proposal,
        "total_s": total,
    }


# ---------------------------------------------------------------- verification

# How long each alert can take to stop firing once its cause is gone. Derived from each rule's own
# parameters rather than guessed, because the first version guessed and got it wrong:
#
#   clear time <= rate window  +  keep_firing_for  +  one evaluation interval (15s)
#
#   AiraHighErrorRate      3m + 2m + 15s = 315s      (observed: still firing at 210s)
#   AiraOperationFailing   3m + 2m + 15s = 315s
#   AiraHighCpu            1m +  -  + 15s =  75s     (observed: ~74s - the derivation holds)
#   AiraServiceDown       45s +  -  + 15s =  60s     (observed: ~31s)
#
# A 210s window for the error rules produced a false "not recovered" on incident 162 while the
# error rate was already zero, which then asked the Remediation agent for a second action against a
# problem that no longer existed. Margin added on top of the derivation.
RECOVERY_WINDOW_S = {
    "AiraServiceDown": 90,
    "AiraHighCpu": 120,
    "AiraHighErrorRate": 360,
    "AiraOperationFailing": 360,
    "AiraHighLatency": 360,
    # Memory is the slow one, and not because the fix is slow. Resetting a leak flag stops the
    # service allocating more; it does not hand back what it already holds. The alert compares
    # each container to its own level 15 minutes earlier, so what actually clears it is that
    # baseline window sliding forward over the elevated period. Nothing can make that faster, and
    # a shorter window here would report a working fix as a failure.
    "AiraMemoryGrowth": 900,
}
DEFAULT_RECOVERY_WINDOW_S = 360
VERIFY_POLL_S = 15

# Which measurement actually says this alert's problem is over. Checked alongside the alert state,
# because `keep_firing_for` deliberately holds an alert after its cause is gone - so "still firing"
# and "still broken" are different questions, and only the second one means the action did not work.
RECOVERY_SIGNAL = {
    "AiraHighErrorRate": ("error_rate_rps", "errors per second", 0.01, "below"),
    "AiraOperationFailing": ("error_rate_rps", "errors per second", 0.01, "below"),
    "AiraHighCpu": ("cpu_percent_of_core", "% of one core", 100.0, "below"),
    "AiraServiceDown": ("container_seconds_since_last_report", "s since the container reported",
                        45.0, "below"),
    "AiraHighLatency": ("latency_p95_ms", "ms at p95", 500.0, "below"),
    # No entry for AiraMemoryGrowth on purpose. The useful question is "is it still growing",
    # which is a ratio against a sliding baseline rather than a level, and no single number in the
    # evidence answers it - memory_bytes alone would call a service that stopped leaking at 80% of
    # its limit "not recovered" forever. Without a signal the verification falls back to the alert
    # state, which for this rule is the honest measure.
}


async def _alert_still_firing(inc: dict) -> bool | None:
    """Is this incident's alert still active in Prometheus? None if Prometheus cannot be reached.

    Prometheus rather than the incident's own status: a resolved webhook may not have arrived yet,
    and the question being asked is about the system, not about AIRA's record of it.
    """
    from .. import http_util
    from ..config import PROMETHEUS_URL

    try:
        async with http_util.async_client(timeout=10) as c:
            r = await c.get(f"{PROMETHEUS_URL}/api/v1/alerts")
            r.raise_for_status()
            alerts = r.json()["data"]["alerts"]
    except Exception as e:  # noqa: BLE001 - unknown is not the same as recovered
        log.warning("incident %s: could not read alert state: %s", inc["id"], e)
        return None
    for a in alerts:
        labels = a.get("labels") or {}
        if (labels.get("alertname") == inc["alertname"]
                and labels.get("service_name") == inc.get("service")):
            return True
    return False


async def _signal_recovered(inc: dict) -> tuple[bool | None, str]:
    """Has the measurement behind this alert returned to normal?

    Asked separately from the alert state. `keep_firing_for: 2m` means an alert outlives its cause
    on purpose, so an alert that is still firing while its signal is clean has been fixed and is
    merely still clearing - which is a success, not a failure.
    """
    spec = RECOVERY_SIGNAL.get(inc["alertname"])
    if not spec:
        return None, "no recovery signal is defined for this alert"
    key, unit, threshold, direction = spec
    try:
        ev = await asyncio.to_thread(metrics_agent.collect, inc.get("service"))
    except Exception as e:  # noqa: BLE001 - unknown is not the same as recovered
        return None, f"could not measure {key}: {e}"
    value = (ev.get("values") or {}).get(key)
    if value is None:
        # For a stopped container, an absent reading is itself the symptom, so absence cannot be
        # read as recovery.
        return None, f"{key} is unavailable"
    ok = value < threshold if direction == "below" else value > threshold
    return ok, f"{key} is {value:.4g} {unit} (recovered when {direction} {threshold:g})"


async def verify_recovery(action_id: int) -> dict:
    """After an action, wait the measured window and decide whether the service recovered.

    Three outcomes, recorded distinctly, because they mean different things:

    * **recovered**     - the alert cleared. The action worked.
    * **not_yet**       - still inside the window. Reported only if this returns early; the loop
                          itself keeps waiting rather than guessing.
    * **not_recovered** - the window passed and the alert is still firing. The incident goes back
                          for another cycle, capped at MAX_REMEDIATION_ATTEMPTS.
    """
    action = db.get_action(action_id)
    if not action:
        return {"outcome": "unknown", "detail": "the action record disappeared"}
    inc = db.get(action["incident_id"])
    window = RECOVERY_WINDOW_S.get(inc["alertname"], DEFAULT_RECOVERY_WINDOW_S)
    attempt = action["attempt"]

    log.info("incident %s: verifying recovery, allowing up to %ds for %s to clear",
             inc["id"], window, inc["alertname"])
    db.update_action(action_id, verification={"outcome": "not_yet", "window_s": window,
                                              "detail": f"waiting up to {window}s for the alert to clear"})
    with events.investigating(inc["id"], remediation_agent.AGENT_NAME, attempt):
        events.record("aira", "verification_started",
                      detail={"alert": inc["alertname"], "window_s": window})

    t0 = time.perf_counter()
    unknown_reads = 0
    while time.perf_counter() - t0 < window:
        await asyncio.sleep(VERIFY_POLL_S)
        firing = await _alert_still_firing(inc)
        signal_ok, signal_detail = await _signal_recovered(inc)
        if firing is None and signal_ok is None:
            unknown_reads += 1
            continue
        if not firing or signal_ok:
            waited = round(time.perf_counter() - t0, 1)
            how = (f"{inc['alertname']} for {inc.get('service')} cleared after {waited}s"
                   if not firing else
                   f"the problem is over after {waited}s - {signal_detail} - though "
                   f"{inc['alertname']} is still clearing, which it does for up to "
                   f"{RECOVERY_WINDOW_S.get(inc['alertname'], DEFAULT_RECOVERY_WINDOW_S)}s")
            result = {"outcome": "recovered", "waited_s": waited, "window_s": window,
                      "alert_cleared": not firing, "signal": signal_detail,
                      "detail": how}
            db.update_action(action_id, verification=result)
            with events.investigating(inc["id"], remediation_agent.AGENT_NAME, attempt):
                events.record("aira", "verification_recovered", ok=True,
                              duration_ms=round(waited * 1000, 1), detail=result)
            log.info("incident %s: RECOVERED - %s", inc["id"], result["detail"])
            await write_postmortem(inc["id"], trigger="recovered after remediation")
            return result

    waited = round(time.perf_counter() - t0, 1)
    _, final_signal = await _signal_recovered(inc)
    result = {"outcome": "not_recovered", "waited_s": waited, "window_s": window,
              "detail": (f"{inc['alertname']} for {inc.get('service')} was still firing after "
                         f"{waited}s, past the {window}s this alert takes to clear, and its "
                         f"measurement has not recovered either: {final_signal}"),
              "signal": final_signal,
              "unreadable_checks": unknown_reads}
    db.update_action(action_id, verification=result)
    with events.investigating(inc["id"], remediation_agent.AGENT_NAME, attempt):
        events.record("aira", "verification_not_recovered", ok=False,
                      duration_ms=round(waited * 1000, 1), detail=result)
    log.warning("incident %s: NOT RECOVERED - %s", inc["id"], result["detail"])

    # Back round the loop, once. A system that keeps acting on a problem it cannot fix is worse
    # than one that stops and says so.
    if attempt >= MAX_REMEDIATION_ATTEMPTS:
        log.info("incident %s: %d remediation attempt(s) made, stopping rather than trying again",
                 inc["id"], attempt)
        with events.investigating(inc["id"], remediation_agent.AGENT_NAME, attempt):
            events.record("aira", "remediation_exhausted", ok=False,
                          detail={"attempts": attempt,
                                  "detail": "two attempts made; AIRA stops rather than acting again"})
        await write_postmortem(inc["id"], trigger="remediation exhausted")
        return result

    log.info("incident %s: proposing a second remediation attempt", inc["id"])
    await propose_remediation(inc["id"], attempt=attempt + 1)
    return result


SEQ_POSTMORTEM = postmortem_agent.SEQ


async def write_postmortem(incident_id: int, trigger: str) -> dict | None:
    """Write the post-incident report, once the response has actually settled.

    "Settled" is the whole difficulty. An incident is not finished when the investigation ends -
    it is finished when either an action has run and been verified, or it is established that no
    action will run. Writing at the end of `investigate` would produce a report that stops at
    "awaiting approval", which is the least interesting moment of the whole incident. So this is
    called from each of the three places the story can actually end, and is idempotent: whichever
    arrives first writes, and the rest find it already there.

    Never raises. A missing report is a missing document; it is not a reason to fail an incident
    that has already been handled.
    """
    existing = [f for f in db.get_findings(incident_id)
                if f["agent"] == postmortem_agent.AGENT_NAME and f["status"] == "done"]
    if existing:
        return (existing[0].get("output") or {})

    facts = postmortem.facts(incident_id)
    if not facts:
        return None

    db.upsert_finding(incident_id, postmortem_agent.AGENT_NAME, seq=SEQ_POSTMORTEM,
                      status="running", started_at=db.now_iso(), model=OLLAMA_MODEL)
    t0 = time.perf_counter()
    try:
        with events.investigating(incident_id, postmortem_agent.AGENT_NAME):
            record = postmortem.record_for_model(facts)
            result, attempts, secs = await asyncio.wait_for(
                asyncio.to_thread(postmortem_agent.run, record), timeout=AGENT_TIMEOUT_S)
            events.record("aira", "postmortem_written", ok=True,
                          duration_ms=round(secs * 1000, 1),
                          detail={"trigger": trigger, "title": result.title,
                                  "follow_ups": len(result.follow_ups)})
        db.upsert_finding(incident_id, postmortem_agent.AGENT_NAME, status="done",
                          output=result.model_dump(), attempts=attempts,
                          finished_at=db.now_iso(), runtime_s=round(secs, 2))
        log.info("incident %s: postmortem written (%s) in %.1fs: %s",
                 incident_id, trigger, secs, result.title[:70])
        return result.model_dump()
    except Exception as e:  # noqa: BLE001 - a report is not worth failing a handled incident for
        msg = (f"postmortem did not finish within {AGENT_TIMEOUT_S:.0f}s"
               if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}")
        db.upsert_finding(incident_id, postmortem_agent.AGENT_NAME, status="error",
                          error=msg[:500], finished_at=db.now_iso(),
                          runtime_s=round(time.perf_counter() - t0, 2))
        with events.investigating(incident_id, postmortem_agent.AGENT_NAME):
            events.record("aira", "postmortem_failed", ok=False, detail={"error": msg[:300]})
        log.error("incident %s: postmortem failed: %s", incident_id, msg[:200])
        return None


# ---------------------------------------------------------------- remediation

SEQ_REMEDIATION = remediation_agent.SEQ
MAX_REMEDIATION_ATTEMPTS = 2   # a system that keeps acting on what it cannot fix is worse than one
                               # that stops and says so


async def propose_remediation(incident_id: int, attempt: int = 1) -> dict | None:
    """Ask the Remediation agent what should be done. Proposes only; executes nothing.

    The proposal is written to `remediation_actions` with status `proposed`, which is what the
    console turns into an Approve/Reject decision. A declined proposal is recorded too - "no safe
    action applies" is an outcome worth keeping, not an absence.
    """
    inc = db.get(incident_id)
    findings = [f for f in _latest(db.get_findings(incident_id))
                if f["agent"] not in ("rca", "critic", "conclusion")]
    by = {f["agent"]: f for f in db.get_findings(incident_id)}
    rca = (by.get("rca") or {}).get("output")
    critic = (by.get("critic") or {}).get("output")

    # On a retry the findings are stale by definition - something was done since they were
    # gathered - so the current flag and container state is read fresh. Deterministic tools only,
    # no model call, so this costs a fraction of a second.
    current_state = None
    if attempt > 1:
        try:
            current_state = await asyncio.to_thread(
                change_agent.collect, inc.get("service"), inc["starts_at"])
            done = [a for a in db.get_actions(incident_id) if a["status"] == "executed"]
            if done:
                last = done[-1]
                current_state["previous_action"] = (
                    f"{last['action']} on {last['target_service']} - "
                    f"{last.get('result_detail') or 'completed'}")
        except Exception as e:  # noqa: BLE001 - a stale view is better than no proposal at all
            log.warning("incident %s: could not read current state for retry: %s", incident_id, e)

    db.upsert_finding(incident_id, remediation_agent.AGENT_NAME, round_=attempt,
                      seq=SEQ_REMEDIATION, status="running", started_at=db.now_iso(),
                      model=OLLAMA_MODEL)
    try:
        with events.investigating(incident_id, remediation_agent.AGENT_NAME, attempt):
            result, attempts, secs = await asyncio.wait_for(
                asyncio.to_thread(remediation_agent.run, inc, findings, rca, critic,
                                  current_state),
                timeout=AGENT_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - a failure to propose must not fail the investigation
        msg = (f"remediation agent did not finish within {AGENT_TIMEOUT_S:.0f}s"
               if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}")
        db.upsert_finding(incident_id, remediation_agent.AGENT_NAME, round_=attempt,
                          status="error", error=msg[:500], finished_at=db.now_iso())
        log.error("incident %s: remediation proposal failed: %s", incident_id, msg[:200])
        return None

    out = result.model_dump()
    db.upsert_finding(incident_id, remediation_agent.AGENT_NAME, round_=attempt, status="done",
                      output=out, attempts=attempts, finished_at=db.now_iso(),
                      runtime_s=round(secs, 2))

    if result.action == "none":
        action_id = db.add_action(
            incident_id, attempt=attempt, action="none", target_service=result.target_service,
            risk=result.risk, reasoning=result.reasoning, status="declined",
            reversal="nothing was proposed, so there is nothing to reverse")
        log.info("incident %s: no safe action applies - %s", incident_id, result.reasoning[:120])
        out["action_id"] = action_id
        return out

    # Validated against the real allowlist before it is offered to a human. A proposal that the
    # safety layer would refuse is recorded as refused rather than shown as approvable.
    params = {"flag": result.flag, "to_variant": "off"} if result.flag else {}
    try:
        checked = remediation.validate(result.action, result.target_service, params)
    except remediation.ActionRefused as e:
        action_id = db.add_action(
            incident_id, attempt=attempt, action=result.action,
            target_service=result.target_service, params=params, risk=result.risk,
            reasoning=result.reasoning, expected_effect=result.expected_effect,
            status="refused", result_detail=str(e)[:400],
            reversal="nothing was executed, so there is nothing to reverse")
        log.warning("incident %s: proposal refused by the safety layer: %s", incident_id, e)
        out["action_id"] = action_id
        out["refused"] = str(e)
        return out

    action_id = db.add_action(
        incident_id, attempt=attempt, action=checked.action,
        target_service=checked.target_service, params=checked.params, risk=checked.risk,
        reasoning=result.reasoning, expected_effect=result.expected_effect,
        reversal=checked.reversal, before_state=checked.before, status="proposed")
    with events.investigating(incident_id, remediation_agent.AGENT_NAME, attempt):
        events.record("aira", "action_proposed",
                      detail={"action": checked.action, "target": checked.target_service,
                              "risk": checked.risk, "params": checked.params,
                              "awaiting": "human approval"})
    log.info("incident %s: action PROPOSED and awaiting approval: %s", incident_id,
             checked.describe())
    out["action_id"] = action_id
    out["describe"] = checked.describe()
    return out


# ---------------------------------------------------------------- reasoning and replanning

MAX_ROUNDS = 2          # a loop on a local model is a hang, not a feature
SEQ_RCA, SEQ_CRITIC = rca_agent.SEQ, critic_agent.SEQ


def _latest(findings: list[dict]) -> list[dict]:
    """One row per agent: the most recent round.

    After a second round the table holds two rows for a re-run investigator. Handing both to the
    RCA agent would show it the superseded answer alongside the new one, which is exactly the kind
    of contradiction a 7B model resolves by averaging. Only the latest round is evidence.
    """
    newest: dict[str, dict] = {}
    for f in findings:
        name = f["agent"]
        if name not in newest or f.get("round", 1) >= newest[name].get("round", 1):
            newest[name] = f
    return sorted(newest.values(), key=lambda f: (f.get("seq") or 0, f["id"]))


def _recall(inc: dict) -> list[dict]:
    """Past cases resembling this incident, or nothing.

    Deliberately total: memory is an advantage, never a dependency. A locked store, a missing
    model or a corrupt index must cost this investigation a hint and nothing else, so every
    failure here is logged and swallowed rather than raised into the reasoning path.
    """
    if not MEMORY_ENABLED:
        return []
    t0 = time.perf_counter()
    try:
        hits = memory.recall(inc)
    except Exception as e:  # noqa: BLE001 - see docstring
        log.warning("incident %s: memory unavailable, continuing without it: %s", inc["id"], e)
        events.record("aira", "memory_unavailable", agent="rca", ok=False,
                      detail={"error": f"{type(e).__name__}: {e}"[:300]})
        return []
    ms = (time.perf_counter() - t0) * 1000
    events.record(
        "aira", "memory_recalled", agent="rca", duration_ms=ms, ok=True,
        detail={"matched": len(hits),
                "query": memory.symptom_key(inc)[:300],
                "cases": [{"incident_id": h["incident_id"], "score": h["score"],
                           "alertname": h["alertname"], "service": h["service"]} for h in hits]},
    )
    if hits:
        log.info("incident %s: memory recalled %d case(s): %s", inc["id"], len(hits),
                 ", ".join(f"#{h['incident_id']}@{h['score']:.2f}" for h in hits))
    return hits


async def _run_rca(incident_id: int, round_no: int) -> dict | None:
    """RCA over A2A: the CrewAI side crosses a protocol boundary to reach the ADK agent."""
    inc, findings = db.get(incident_id), _latest(db.get_findings(incident_id))
    started = db.now_iso()
    db.upsert_finding(incident_id, rca_agent.AGENT_NAME, round_=round_no, seq=SEQ_RCA,
                      status="running", started_at=started, model=OLLAMA_MODEL)
    t0 = time.perf_counter()
    prior: list[dict] = []
    try:
        # Inside `investigating` so the recall is recorded against this incident - events outside
        # that context are silently dropped, which is how the first run of this left no trace of
        # what memory had offered. Outside `agent_call`, because a vector search is not one.
        with events.investigating(incident_id, rca_agent.AGENT_NAME, round_no):
            # Retrieved here rather than at triage: by now every investigator has reported, so the
            # symptom key is built from the complete evidence, and the cases go to the one agent
            # whose job is to reason across sources.
            prior = _recall(inc)
            with events.agent_call(rca_agent.AGENT_NAME, "a2a"):
                result, attempts, secs = await asyncio.wait_for(
                    rca_agent.run(inc, findings, prior), timeout=AGENT_TIMEOUT_S)
        out = result.model_dump()
        out["prior_cases"] = prior   # what memory offered, kept beside what the agent concluded
        db.upsert_finding(incident_id, rca_agent.AGENT_NAME, round_=round_no, status="done",
                          output=out, attempts=attempts,
                          finished_at=db.now_iso(), runtime_s=round(secs, 2))
        log.info("incident %s: RCA (round %d) in %.1fs: %s",
                 incident_id, round_no, secs, result.summary[:90])
        return result.model_dump()
    except Exception as e:  # noqa: BLE001 - an unreachable A2A peer must be visible, not fatal
        msg = (f"RCA agent did not finish within {AGENT_TIMEOUT_S:.0f}s"
               if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}")
        db.upsert_finding(incident_id, rca_agent.AGENT_NAME, round_=round_no, status="error",
                          error=msg[:500], finished_at=db.now_iso(),
                          runtime_s=round(time.perf_counter() - t0, 2))
        log.error("incident %s: RCA failed: %s", incident_id, msg[:200])
        return None


async def _run_critic(incident_id: int, rca: dict, round_no: int) -> dict | None:
    inc, findings = db.get(incident_id), _latest(db.get_findings(incident_id))
    db.upsert_finding(incident_id, critic_agent.AGENT_NAME, round_=round_no, seq=SEQ_CRITIC,
                      status="running", started_at=db.now_iso(), model=OLLAMA_MODEL)
    t0 = time.perf_counter()
    try:
        # CrewAI refuses to run synchronously inside a running event loop, same as the investigators.
        with events.investigating(incident_id, critic_agent.AGENT_NAME, round_no):
            result, attempts, secs = await asyncio.wait_for(
                asyncio.to_thread(critic_agent.run, inc, findings, rca), timeout=AGENT_TIMEOUT_S)
        db.upsert_finding(incident_id, critic_agent.AGENT_NAME, round_=round_no, status="done",
                          output=result.model_dump(), attempts=attempts,
                          finished_at=db.now_iso(), runtime_s=round(secs, 2))
        log.info("incident %s: critic (round %d) in %.1fs: %s",
                 incident_id, round_no, secs, result.verdict)
        return result.model_dump()
    except Exception as e:  # noqa: BLE001
        msg = (f"critic did not finish within {AGENT_TIMEOUT_S:.0f}s"
               if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}")
        db.upsert_finding(incident_id, critic_agent.AGENT_NAME, round_=round_no, status="error",
                          error=msg[:500], finished_at=db.now_iso(),
                          runtime_s=round(time.perf_counter() - t0, 2))
        log.error("incident %s: critic failed: %s", incident_id, msg[:200])
        return None


async def _reinvestigate(incident_id: int, gaps: list[dict], round_no: int) -> list[str]:
    """Re-run the investigators the Critic named, each asked its specific question."""
    inc = db.get(incident_id)
    svc, started = inc.get("service"), inc["starts_at"]
    wanted: dict[str, str] = {}
    for g in gaps:
        name = g.get("agent")
        if name in INVESTIGATORS:
            wanted.setdefault(name, g.get("missing", ""))
    if not wanted:
        return []

    # Round two collects over MCP as well. Re-running an investigator against freshly gathered
    # evidence is the whole point of a second round, so it must come through the same path - and
    # the same recorded fallback - as the first.
    names = list(wanted)

    async def one(name: str):
        with events.investigating(incident_id, name, round_no, phase="evidence"):
            return await _gather_one(name, svc, started)

    fresh = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)

    done = []
    for name, ev in zip(names, fresh):
        if isinstance(ev, BaseException):
            log.warning("incident %s: round %d evidence for %s failed: %s", incident_id, round_no, name, ev)
            continue
        db.upsert_finding(incident_id, name, round_=round_no, seq=SEQ[name], status="running",
                          started_at=db.now_iso(), evidence=ev, evidence_text=ev.get("text"),
                          model=OLLAMA_MODEL)
        try:
            with events.investigating(incident_id, name, round_no):
                result, attempts, secs = await asyncio.wait_for(
                    asyncio.to_thread(INVESTIGATORS[name].run, inc, ev, wanted[name]),
                    timeout=AGENT_TIMEOUT_S)
            db.upsert_finding(incident_id, name, round_=round_no, status="done",
                              output=result.model_dump(), attempts=attempts,
                              finished_at=db.now_iso(), runtime_s=round(secs, 2))
            done.append(name)
            log.info("incident %s: %s agent re-ran (round %d) in %.1fs", incident_id, name, round_no, secs)
        except Exception as e:  # noqa: BLE001
            db.upsert_finding(incident_id, name, round_=round_no, status="error",
                              error=f"{type(e).__name__}: {e}"[:500], finished_at=db.now_iso())
            log.error("incident %s: %s agent re-run failed: %s", incident_id, name, e)
    return done


async def reason(incident_id: int) -> dict:
    """Combine the findings, challenge the result, and investigate again if it does not hold.

    This is the reflection loop: the ADK RCA agent proposes, the CrewAI Critic reviews, and a
    rejected analysis sends the named investigators back out with a sharper question. Capped at
    MAX_ROUNDS - if the Critic is still unconvinced, that is recorded as the outcome rather than
    looped over, because "investigated twice and still not established" is a legitimate answer.
    """
    t0 = time.perf_counter()
    rounds = []
    for round_no in range(1, MAX_ROUNDS + 1):
        rca = await _run_rca(incident_id, round_no)
        if rca is None:
            rounds.append({"round": round_no, "rca": "error"})
            break
        critic = await _run_critic(incident_id, rca, round_no)
        verdict = (critic or {}).get("verdict")
        rounds.append({"round": round_no, "verdict": verdict or "critic-error",
                       "summary": rca.get("summary")})

        if critic is None or verdict == "accept":
            break
        if round_no == MAX_ROUNDS:
            log.info("incident %s: evidence still insufficient after %d rounds - recorded as such",
                     incident_id, MAX_ROUNDS)
            break
        reran = await _reinvestigate(incident_id, critic.get("gaps") or [], round_no + 1)
        if not reran:
            log.info("incident %s: critic asked for nothing actionable; stopping at round %d",
                     incident_id, round_no)
            break

    total = round(time.perf_counter() - t0, 2)
    accepted = any(r.get("verdict") == "accept" for r in rounds)
    _record_conclusion(incident_id, rounds, accepted, total)
    log.info("incident %s: reasoning finished in %.1fs over %d round(s)", incident_id, total, len(rounds))
    return {"rounds": rounds, "total_s": total, "accepted": accepted}


SEQ_CONCLUSION = 80


def _record_conclusion(incident_id: int, rounds: list[dict], accepted: bool, total_s: float) -> None:
    """Write what the investigation actually concluded, including when it concluded nothing.

    "Investigated twice and the evidence still does not establish a cause" is a real outcome and the
    console must be able to say so, rather than presenting an unreviewed hypothesis as an answer.
    Stored as its own findings row so no schema change is needed and old incidents keep rendering.
    """
    last = rounds[-1] if rounds else {}
    rounds_run = len(rounds)
    if accepted:
        outcome = "accepted"
        statement = last.get("summary") or "Root cause accepted by the reviewer."
    elif last.get("rca") == "error" or last.get("verdict") == "critic-error":
        outcome = "error"
        statement = "The reasoning agents did not complete, so no conclusion was reached."
    else:
        outcome = "insufficient"
        statement = (f"Investigated {'twice' if rounds_run > 1 else 'once'}; the evidence still does "
                     "not establish a cause. The best hypothesis is recorded but unconfirmed.")
    # No runtime_s: this row is a summary, not a model call. The console adds the column up to show
    # how long the investigation took, and counting the reasoning total again would double it.
    db.upsert_finding(
        incident_id, "conclusion", seq=SEQ_CONCLUSION, status="done",
        finished_at=db.now_iso(),
        output={"outcome": outcome, "statement": statement, "rounds_run": rounds_run,
                "reasoning_s": total_s, "rounds": rounds},
    )
    _remember(incident_id, outcome)


def _remember(incident_id: int, outcome: str) -> None:
    """Add this investigation to memory, now that it has concluded.

    Stored whatever the verdict was - a rejected hypothesis is worth counting, and the store keeps
    the verdict so `recall` can exclude it. Like recall, this never raises: failing to record a
    case must not fail the incident that produced it.
    """
    if not MEMORY_ENABLED:
        return
    # Declared against the incident so the store shows up in its trace like every other step.
    with events.investigating(incident_id, "memory", phase="memory"):
        t0 = time.perf_counter()
        try:
            stored = memory.remember(incident_id)
        except Exception as e:  # noqa: BLE001 - memory is an advantage, not a dependency
            log.warning("incident %s: could not store in memory: %s", incident_id, e)
            events.record("aira", "memory_store_failed", ok=False,
                          detail={"error": f"{type(e).__name__}: {e}"[:300]})
            return
        events.record("aira", "memory_stored" if stored else "memory_not_stored", ok=True,
                      duration_ms=(time.perf_counter() - t0) * 1000,
                      detail={"outcome": outcome,
                              "why": None if stored else "no root cause was reached"})
    if stored:
        log.info("incident %s: stored in memory (%s)", incident_id, outcome)
