"""AIRA backend: receives Alertmanager webhooks, stores incidents, gathers live evidence."""

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, Response,
                               StreamingResponse)

from . import (appdata, config, db, events, guardrails, http_util, live, mcp_client,  # noqa: E501
               memory, postmortem, postmortem_pdf, prometheus_tool, remediation, tracing,
               triage_agent)
# CrewAI, ADK and litellm together cost roughly 300 MB of resident memory, and an archive
# deployment never calls any of them: there is no stack to investigate, so the webhook is closed
# and the RCA agent is not part of the deployment. Importing them anyway would put a read-only
# console over Render's 512 MB free tier for nothing. Full mode still imports eagerly, so the
# first real investigation pays no import cost.
if config.ARCHIVE:
    adk_base = orchestrator = None  # type: ignore[assignment]
else:
    from .agents import adk_base, orchestrator
from .config import (AGENT_TIMEOUT_S, ALERTMANAGER_URL, LOG_DIR, OLLAMA_MODEL, OLLAMA_URL,
                     PROMETHEUS_URL)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_DIR / "backend.log", encoding="utf-8")],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("aira")

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    log.info("AIRA backend started; prometheus=%s ollama=%s model=%s", PROMETHEUS_URL, OLLAMA_URL, OLLAMA_MODEL)
    # Subscribed once per process: CrewAI's bus is global, so wiring it per agent run would add a
    # duplicate handler for every incident.
    if events.wire_crewai():
        log.info("monitoring callbacks active: CrewAI event bus + ADK agent callbacks")
    tracing.init()
    backfilled = db.backfill_triage_findings()
    if backfilled:
        log.info("backfilled %d incident(s) with a triage finding", backfilled)
    await reconcile_firing()
    # Firing incidents whose investigation was interrupted by a backend restart are resumed.
    unfinished = [i["id"] for i in db.list_incidents(1000)
                  if i["status"] == "firing" and i["agent_status"] in ("pending", "running")]
    tasks = [asyncio.create_task(investigate(i)) for i in unfinished]
    if unfinished:
        log.info("resuming investigation of incidents %s", unfinished)
    # Anything else left mid-run belongs to an investigation that will never be finished: the
    # incident has already resolved, so it is not resumed above. Say so rather than leaving the
    # console showing an agent that is still thinking.
    orphaned = db.close_orphaned_findings(skip_ids=tuple(unfinished))
    if orphaned:
        log.info("closed %d agent row(s) left running by a previous backend stop", orphaned)
    # Live updates for the console and the mobile app: a database watcher, never a second store.
    live.start()
    yield
    live.stop()
    for t in tasks:
        t.cancel()
    # Flush whatever spans are still buffered, so the last investigation before a restart is not
    # the one missing from Jaeger.
    tracing.shutdown()


app = FastAPI(title="AIRA backend (mid-semester preview)", lifespan=lifespan)


# ---------------------------------------------------------------- access control
#
# Off unless AIRA_ACCESS_TOKEN is set (see config.py). The default deployment listens on 127.0.0.1
# only, so there is nobody else to keep out. Once the backend is reachable from other machines,
# every request from them must carry the token, except:
#   /alerts      Alertmanager's webhook, which arrives from the Docker network; it is already the
#                one validated way in (guardrails.py), and gating it would stop incidents arriving.
#   /health      so monitoring and the app can tell "unreachable" from "not signed in".
#   the app shell and its icons, which contain no data, so a phone can load the sign-in screen.
_OPEN_PATHS = {"/alerts", "/health", "/login", "/api/session", "/manifest.webmanifest",
               "/style.css", "/favicon.ico"}
_OPEN_PREFIXES = ("/app/",)


def _token_ok(request: Request) -> bool:
    supplied = (request.headers.get("x-aira-token") or request.cookies.get("aira_token")
                or request.query_params.get("token") or "")
    return bool(supplied) and hmac.compare_digest(supplied.encode(), config.ACCESS_TOKEN.encode())


@app.middleware("http")
async def access_guard(request: Request, call_next):
    if not config.ACCESS_TOKEN:
        return await call_next(request)
    client = request.client.host if request.client else ""
    path = request.url.path
    if ((config.TRUST_LOOPBACK and client in ("127.0.0.1", "::1"))
            or path in _OPEN_PATHS or path == "/app" or path.startswith(_OPEN_PREFIXES)
            or _token_ok(request)):
        return await call_next(request)
    if "text/html" in request.headers.get("accept", "") and request.method == "GET":
        return HTMLResponse(_LOGIN_PAGE, status_code=401)
    return JSONResponse({"detail": "an access token is required from this address",
                         "auth_required": True}, status_code=401)


_LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · AIRA</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0B0F14;color:#F3F6FA;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}form{width:min(360px,90vw)}
h1{font-size:20px;margin:0 0 6px}p{color:#9AA8B8;font-size:14px;margin:0 0 18px}
input{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #3A4859;
background:#151D27;color:#F3F6FA;font-size:15px}button{margin-top:12px;width:100%;padding:12px;
border:0;border-radius:8px;background:#42D6C5;color:#06231F;font-weight:650;font-size:15px}
.e{color:#F87171;font-size:13px;min-height:18px;margin-top:8px}</style></head><body>
<form id="f"><h1>AIRA</h1><p>This AIRA backend needs its access token from this address.</p>
<input id="t" type="password" autocomplete="current-password" placeholder="Access token" required>
<button>Sign in</button><div class="e" id="e"></div></form>
<script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();
const r=await fetch("/api/session",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({token:document.getElementById("t").value})});
if(r.ok)location.reload();else document.getElementById("e").textContent="That token was not accepted.";};
</script></body></html>"""


@app.get("/login")
def login_page():
    return HTMLResponse(_LOGIN_PAGE)


@app.post("/api/session")
async def open_session(request: Request):
    """Exchange the access token for a cookie, so the console and the app can sign in once."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not config.ACCESS_TOKEN:
        return {"ok": True, "auth_required": False}
    token = str(body.get("token") or "")
    if not token or not hmac.compare_digest(token.encode(), config.ACCESS_TOKEN.encode()):
        raise HTTPException(401, "that token was not accepted")
    resp = JSONResponse({"ok": True, "auth_required": True})
    resp.set_cookie("aira_token", token, max_age=30 * 24 * 3600, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", path="/")
    return resp


@app.delete("/api/session")
def close_session():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("aira_token", path="/")
    return resp


# One agent run at a time: the local model serves requests sequentially anyway, and this keeps
# per-incident runtimes meaningful when an alert cascade arrives at once.
_agent_slot = asyncio.Semaphore(1)

# Timeline positions. Gaps leave room for the investigators added in this phase
# (orchestrator 20, metrics/log/change 30/40/50) without renumbering.
SEQ_TRIAGE = 10


def _fail_triage(incident_id: int, error: str, t0: float, attempts: int | None = None,
                 output: dict | None = None) -> None:
    """Record a triage failure in both the incident row and its finding, with an honest error."""
    finished, runtime = db.now_iso(), round(time.perf_counter() - t0, 2)
    db.update(incident_id, agent_status="error", agent_error=error, agent_attempts=attempts,
              agent_output=output, agent_finished_at=finished, agent_runtime_s=runtime)
    db.upsert_finding(incident_id, "triage", status="error", error=error, attempts=attempts,
                      output=output, finished_at=finished, runtime_s=runtime)


async def reconcile_firing() -> int:
    """Close incidents that Alertmanager no longer considers active.

    If the backend is down when an alert resolves, that webhook is lost and the incident stays
    "firing" forever - after a stop/start cycle the console showed five incidents as active while
    Prometheus reported none. On startup, Alertmanager is the source of truth: any incident still
    marked firing whose fingerprint it does not know about has ended.
    """
    firing = [i for i in db.list_incidents(1000) if i["status"] == "firing"]
    if not firing:
        return 0
    try:
        async with http_util.async_client(timeout=10) as client:
            r = await client.get(f"{ALERTMANAGER_URL}/api/v2/alerts",
                                 params={"active": "true", "silenced": "false", "inhibited": "false"})
            r.raise_for_status()
            live = {a.get("fingerprint") for a in r.json()}
    except Exception as e:  # noqa: BLE001 - leave incidents alone rather than guess
        log.warning("could not reconcile firing incidents with Alertmanager: %s", e)
        return 0

    closed = 0
    for inc in firing:
        if inc["fingerprint"] in live:
            continue
        db.update(inc["id"], status="resolved", ends_at=inc["updated_at"],
                  description=(inc["description"] or "") +
                  "  [closed on restart: Alertmanager no longer lists this alert as active]")
        db.close_pending_actions(inc["id"], "the incident was already over when AIRA restarted")
        closed += 1
    if closed:
        log.info("reconciled %d stale firing incident(s) against Alertmanager", closed)
    return closed


async def investigate(incident_id: int) -> None:
    """For a new firing incident: collect live Prometheus evidence, then run the Triage agent."""
    inc = db.get(incident_id)
    try:
        evidence = await asyncio.to_thread(prometheus_tool.collect, inc["service"])
        db.update(incident_id, evidence=evidence, evidence_text=evidence["text"],
                  evidence_collected_at=evidence["collected_at"])
        log.info("incident %s: evidence collected for %s", incident_id, inc["service"])
    except Exception as e:  # noqa: BLE001
        log.exception("incident %s: evidence collection failed", incident_id)
        db.update(incident_id, evidence_text=f"Evidence collection failed: {e}")

    async with _agent_slot:
        started = db.now_iso()
        db.update(incident_id, agent_status="running", agent_model=OLLAMA_MODEL, agent_started_at=started)
        db.upsert_finding(incident_id, "triage", seq=SEQ_TRIAGE, status="running",
                          model=OLLAMA_MODEL, started_at=started,
                          evidence_text=db.get(incident_id)["evidence_text"])
        log.info("incident %s: triage agent started (%s)", incident_id, OLLAMA_MODEL)
        t0 = time.perf_counter()
        try:
            with events.investigating(incident_id, "triage"):
                result, attempts, seconds = await asyncio.wait_for(
                    asyncio.to_thread(triage_agent.run, db.get(incident_id)),
                    timeout=AGENT_TIMEOUT_S)
            finished, runtime = db.now_iso(), round(seconds, 2)
            db.update(incident_id, agent_status="done", agent_output=result.model_dump(), agent_attempts=attempts,
                      agent_finished_at=finished, agent_runtime_s=runtime)
            db.upsert_finding(incident_id, "triage", status="done", output=result.model_dump(),
                              attempts=attempts, finished_at=finished, runtime_s=runtime)
            log.info("incident %s: triage done in %.1fs (%d attempt(s)): %s",
                     incident_id, seconds, attempts, result.likely_cause_category)
        except triage_agent.TriageError as e:
            _fail_triage(incident_id, str(e), t0, attempts=e.attempts, output={"raw_output": e.raw})
            log.error("incident %s: triage failed: %s", incident_id, e)
        except asyncio.TimeoutError:
            _fail_triage(incident_id, f"agent did not finish within {AGENT_TIMEOUT_S:.0f}s", t0)
            log.error("incident %s: triage timed out", incident_id)
        except Exception as e:  # noqa: BLE001 - e.g. Ollama unreachable
            _fail_triage(incident_id, f"{type(e).__name__}: {e}", t0)
            log.exception("incident %s: triage crashed", incident_id)

        # Triage gives the Orchestrator something to plan from; a failed triage is not fatal, the
        # plan then falls back to the alert's own signal label.
        inc_now = db.get(incident_id)
        triage_out = inc_now.get("agent_output") if inc_now.get("agent_status") == "done" else None
        try:
            if orchestrator is None:
                log.info("incident %s: archive deployment, not investigating", incident_id)
                return
            await orchestrator.investigate(incident_id, triage_out)
        except Exception:  # noqa: BLE001 - never let the investigation kill the webhook task
            log.exception("incident %s: orchestrated investigation failed", incident_id)


@app.post("/alerts")
async def alerts(request: Request, background: BackgroundTasks):
    """Alertmanager webhook receiver (webhook payload version 4).

    This is the only unauthenticated way into AIRA, so it is where input validation belongs. The
    payload is logged before it is validated: a rejected payload is exactly the one worth keeping,
    and `webhook_log` is the quarantine.
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not JSON")
    db.log_webhook(payload)

    try:
        hook = guardrails.validate_webhook(payload)
    except guardrails.Rejected as e:
        # Refused, recorded and reported. Previously a payload of the wrong shape either crashed
        # the handler with a 500 or was silently treated as empty.
        log.warning("webhook REJECTED by input validation: %s", e)
        raise HTTPException(400, f"payload rejected: {e}") from e

    log.info("webhook: status=%s alerts=%d group=%s", payload.get("status"),
             len(hook.alerts), payload.get("groupLabels"))

    results = []
    for validated in hook.alerts:
        # Back to a plain dict: db.upsert_alert and everything downstream work on the alert as
        # Alertmanager sent it, now that it is known to be well formed.
        alert = validated.model_dump(exclude_none=False)
        incident_id, created = db.upsert_alert(alert)
        results.append({"incident_id": incident_id, "created": created, "status": alert.get("status")})
        log.info("incident %s %s: %s %s/%s", incident_id, "created" if created else "updated",
                 alert.get("status"), validated.labels.get("alertname"),
                 validated.labels.get("service_name"))
        if created and alert.get("status") == "firing":
            background.add_task(investigate, incident_id)
        elif alert.get("status") == "resolved":
            # The incident is over, so anything still awaiting approval for it is moot.
            closed = db.close_pending_actions(
                incident_id, "the incident resolved before this was decided")
            if closed:
                log.info("incident %s resolved: closed %d proposal(s) nobody had decided",
                         incident_id, closed)
            # The catch-all for the report. The orchestrator writes it at the points it knows an
            # incident ended - nothing proposed, recovered, exhausted - and the reject endpoint
            # covers a human declining. None of those fire when a proposal is superseded because
            # another incident already applied the same fix, which is exactly what happened to
            # incident 248: it ran eight agents, reached an accepted root cause, and showed
            # "Postmortem - not reached" because its action said "already done". Hanging the
            # report on the alert resolving instead catches that and anything else unenumerated:
            # whatever route the incident took, this is where it is over. Idempotent, so the
            # earlier hooks still win when they fire first.
            if orchestrator is not None:
                background.add_task(orchestrator.write_postmortem, incident_id,
                                    "incident resolved")
    return {"received": len(results), "incidents": results}


@app.get("/incidents")
def incidents(limit: int = 100):
    items = db.list_incidents(limit)
    ids = [i["id"] for i in items]
    summary = db.findings_summary(ids)
    # Only the count here: the trace itself carries prompts and responses, far too much for a view
    # the console polls every few seconds. It is fetched per incident, on demand.
    counts = db.event_counts(ids)
    actions = db.actions_summary(ids)
    for i in items:
        i["agents"] = summary.get(i["id"], {"agents": [], "running": None, "total_runtime_s": 0.0})
        i["event_count"] = counts.get(i["id"], 0)
        i["actions"] = actions.get(i["id"], [])
    return items


@app.get("/incidents/{incident_id}")
def incident(incident_id: int, include_events: bool = True):
    """One incident with its findings, actions and (by default) its full execution trace.

    include_events=false leaves the trace out: it carries every prompt and response and can run to
    megabytes, which the mobile app fetches separately, only when the trace is opened.
    """
    inc = db.get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    inc["findings"] = db.get_findings(incident_id)
    if include_events:
        inc["events"] = db.get_events(incident_id)
    else:
        inc["event_count"] = db.event_counts([incident_id]).get(incident_id, 0)
    inc["actions"] = db.get_actions(incident_id)
    return inc


# Who a decision is recorded against, by where it was made. There are no user accounts in AIRA, so
# the source is the identity; the label is what both the console and the app display.
DECISION_SOURCES = {"app": "developer (app)", "console": "developer (console)"}


def _decision_request(body: dict, action: dict) -> dict:
    """Validate what a client sent with an approval or rejection."""
    source = str(body.get("source") or "").strip().lower() or None
    if source is not None and source not in DECISION_SOURCES:
        raise HTTPException(422, "source must be 'app' or 'console'")
    incident_id = body.get("incident_id")
    if incident_id is not None and str(incident_id) != str(action["incident_id"]):
        # A stale screen must not decide on the wrong incident's proposal.
        raise HTTPException(409, f"action {action['id']} belongs to incident "
                                 f"{action['incident_id']}, not {incident_id}")
    request_id = str(body.get("request_id") or "").strip()[:64] or None
    # Callers that predate the source field (scripts, older pages) keep their own label.
    who = DECISION_SOURCES[source] if source else str(body.get("by") or "human (console)")[:80]
    return {"source": source, "who": who, "note": str(body.get("note") or "")[:400],
            "request_id": request_id, "client_time": str(body.get("client_time") or "")[:40] or None}


def _already_decided(action_id: int, wanted: str, request_id: str | None) -> JSONResponse:
    """The answer for a decision that arrived after another one.

    A retry of the request that already won (same request_id, e.g. resent after a dropped
    connection) gets 200 and the current state, so the client does not show a false failure and
    nothing is done twice. Anything else gets 409 naming who decided first.
    """
    current = db.get_action(action_id) or {}
    if request_id and current.get("decision_request_id") == request_id:
        return JSONResponse({"action_id": action_id, "status": current.get("status"),
                             "duplicate": True, "action": current,
                             "detail": "this request was already recorded; nothing was done twice"})
    status, by = current.get("status") or "unknown", current.get("decided_by")
    return JSONResponse(status_code=409, content={
        "detail": (f"this action is already '{status}'" + (f" by {by}" if by else "")
                   + f", so it cannot be {wanted}"),
        "action": current})


@app.post("/actions/{action_id}/approve")
async def approve_action(action_id: int, request: Request):
    """Approve a proposed action and execute it.

    This is the human-in-the-loop gate. Nothing in AIRA reaches this endpoint on its own: it is
    called by a person clicking Approve on the console, and that is the only way an action runs.

    The sequence is deliberate. The decision is recorded *before* the action is attempted, so an
    execution that crashes or never returns still leaves evidence that it was approved and started.
    `remediation.execute()` then re-validates against the allowlist at the moment of acting,
    because the system may have changed since the proposal was made.
    """
    action = db.get_action(action_id)
    if not action:
        raise HTTPException(404, "no such action")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty body is normal from a button
        body = {}
    if not isinstance(body, dict):
        body = {}
    d = _decision_request(body, action)
    if action["status"] != "proposed":
        # Not an error worth a 500, but it must not run twice. An action already executed, rejected
        # or refused is final.
        return _already_decided(action_id, "approved", d["request_id"])

    # The real gate: one atomic update that succeeds only while the action is still 'proposed'.
    # When the console and the mobile app approve at the same moment, exactly one of them gets
    # past this line, so the action below runs once.
    if not db.claim_action(action_id, status="approved", decided_by=d["who"],
                           decided_at=db.now_iso(), decision_note=d["note"],
                           decision_source=d["source"], decision_request_id=d["request_id"]):
        return _already_decided(action_id, "approved", d["request_id"])
    who, note = d["who"], d["note"]
    with events.investigating(action["incident_id"], "remediation", action["attempt"]):
        events.record("aira", "action_approved",
                      detail={"action": action["action"], "target": action["target_service"],
                              "approved_by": who, "source": d["source"], "note": note,
                              "request_id": d["request_id"], "client_time": d["client_time"]})
    log.info("incident %s: action %s APPROVED by %s", action["incident_id"], action_id, who)

    proposal = remediation.Proposal(
        action=action["action"], target_service=action["target_service"],
        params=action.get("params") or {}, risk=action.get("risk") or "medium",
        reversal=action.get("reversal") or "", before=action.get("before_state") or {})

    started = db.now_iso()
    try:
        with events.investigating(action["incident_id"], "remediation", action["attempt"]):
            with tracing.span("remediation:execute",
                              **{"aira.action": action["action"],
                                 "aira.target": action["target_service"],
                                 "aira.incident_id": action["incident_id"]}):
                outcome = await asyncio.to_thread(remediation.execute, proposal)
    except remediation.ActionRefused as e:
        # A refusal is not a failure. `execute()` re-validates at the moment of acting, so an
        # action approved after the system already changed - a flag someone else turned off, a
        # container already restarted - is refused here having done nothing. Recording that as a
        # failure would misreport a guard working correctly as something going wrong.
        detail = str(e)
        db.update_action(action_id, status="refused", executed_at=started, result_ok=0,
                         result_detail=detail[:500])
        with events.investigating(action["incident_id"], "remediation", action["attempt"]):
            events.record("aira", "action_refused", ok=False,
                          detail={"action": action["action"], "target": action["target_service"],
                                  "reason": detail[:400],
                                  "guardrail": "re-validated at the moment of acting"})
        log.info("incident %s: action %s REFUSED at execution time: %s",
                 action["incident_id"], action_id, detail[:200])
        return {"action_id": action_id, "status": "refused", "detail": detail[:400]}
    except Exception as e:  # noqa: BLE001 - a failed action is recorded and stops; it never cascades
        detail = f"{type(e).__name__}: {e}"
        db.update_action(action_id, status="failed", executed_at=started, result_ok=0,
                         result_detail=detail[:500])
        with events.investigating(action["incident_id"], "remediation", action["attempt"]):
            events.record("aira", "action_failed", ok=False,
                          detail={"action": action["action"], "target": action["target_service"],
                                  "error": detail[:400]})
        log.error("incident %s: action %s FAILED: %s", action["incident_id"], action_id, detail[:200])
        # Reported, not retried. Safety rule: a failed action must not cascade.
        return {"action_id": action_id, "status": "failed", "detail": detail[:400]}

    # One cause often produces several alerts - a payment flag fires on payment, checkout and the
    # frontend - so several incidents can each propose the same remedy. Once one is carried out the
    # others can never succeed, and leaving them on screen as approvable invites a human to click
    # something that will only be refused. They are closed as superseded, naming the action that
    # actually did the work.
    superseded = 0
    for other in db.get_actions_pending_like(action["action"], action["target_service"],
                                             exclude_id=action_id):
        # Conditional, so a proposal a person decided on in the meantime keeps their decision.
        if db.claim_action(other["id"], status="superseded", decided_at=db.now_iso(),
                           decided_by="AIRA (automatic)",
                           decision_note=(f"action {action_id} on incident "
                                          f"{action['incident_id']} already did this")):
            superseded += 1
    if superseded:
        log.info("incident %s: action %s also superseded %d pending proposal(s) for the same remedy",
                 action["incident_id"], action_id, superseded)

    db.update_action(action_id, status="executed", executed_at=started,
                     result_ok=1 if outcome.ok else 0, result_detail=outcome.detail[:500],
                     verified_by=outcome.verified_by[:400], after_state=outcome.after,
                     duration_s=outcome.duration_s, reversal=outcome.reversal)
    with events.investigating(action["incident_id"], "remediation", action["attempt"]):
        events.record("aira", "action_executed", ok=outcome.ok,
                      duration_ms=round(outcome.duration_s * 1000, 1),
                      detail={"action": action["action"], "target": action["target_service"],
                              "result": outcome.detail, "verified_by": outcome.verified_by,
                              "reversal": outcome.reversal})
    log.info("incident %s: action %s EXECUTED: %s | verified: %s", action["incident_id"],
             action_id, outcome.detail, outcome.verified_by)

    # The verification loop runs in the background: recovery takes 31-120s depending on the alert,
    # and the console must not block on it.
    background = BackgroundTasks()
    if orchestrator is None:
        raise HTTPException(503, "this deployment serves recorded incidents only; it cannot act "
                                 "on a running system")
    background.add_task(orchestrator.verify_recovery, action_id)
    return JSONResponse(
        {"action_id": action_id, "status": "executed", "ok": outcome.ok,
         "detail": outcome.detail, "verified_by": outcome.verified_by,
         "reversal": outcome.reversal, "duration_s": outcome.duration_s},
        background=background)


@app.post("/actions/{action_id}/reject")
async def reject_action(action_id: int, request: Request, background: BackgroundTasks):
    """Reject a proposed action. Nothing is executed, and the refusal is recorded with its reason."""
    action = db.get_action(action_id)
    if not action:
        raise HTTPException(404, "no such action")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    d = _decision_request(body, action)
    if action["status"] != "proposed":
        return _already_decided(action_id, "rejected", d["request_id"])
    # Same single gate as approval: an Approve and a Reject arriving together cannot both land.
    if not db.claim_action(action_id, status="rejected", decided_by=d["who"],
                           decided_at=db.now_iso(), decision_note=d["note"],
                           decision_source=d["source"], decision_request_id=d["request_id"]):
        return _already_decided(action_id, "rejected", d["request_id"])
    who, note = d["who"], d["note"]
    with events.investigating(action["incident_id"], "remediation", action["attempt"]):
        events.record("aira", "action_rejected", ok=False,
                      detail={"action": action["action"], "target": action["target_service"],
                              "rejected_by": who, "source": d["source"], "note": note,
                              "request_id": d["request_id"], "client_time": d["client_time"]})
    log.info("incident %s: action %s REJECTED by %s%s", action["incident_id"], action_id, who,
             f" ({note})" if note else "")
    if orchestrator is not None:
        background.add_task(orchestrator.write_postmortem, action["incident_id"],
                            "proposal rejected by a human")
    return {"action_id": action_id, "status": "rejected"}


@app.get("/incidents/{incident_id}/postmortem")
def incident_postmortem(incident_id: int):
    """The post-incident report, rendered as Markdown, plus the narrative as data.

    Returns 404 until the response has settled: a report written while an action is still awaiting
    approval would stop at the least interesting moment of the incident.
    """
    md = postmortem.markdown(incident_id)
    if md is None:
        raise HTTPException(404, f"no incident {incident_id}")
    rows = [f for f in db.get_findings(incident_id) if f["agent"] == postmortem.AGENT_NAME]
    row = next((f for f in rows if f["status"] == "done"), None)
    return {
        "incident_id": incident_id,
        "ready": row is not None,
        "status": rows[0]["status"] if rows else "not started",
        "error": rows[0].get("error") if rows else None,
        "narrative": (row.get("output") if row else None),
        "markdown": md,
    }


@app.get("/incidents/{incident_id}/postmortem.md")
def incident_postmortem_file(incident_id: int):
    """The same report as a downloadable Markdown file.

    Kept alongside the PDF rather than replaced by it. The console offers the PDF, because that is
    what somebody sends or prints, but Markdown is the format this report is assembled in and the
    one that stays diffable and greppable - and removing a published route to add a format would
    break anything already pointing at it.
    """
    md = postmortem.markdown(incident_id)
    if md is None:
        raise HTTPException(404, f"no incident {incident_id}")
    inc = db.get(incident_id)
    name = f"postmortem-{incident_id}-{(inc.get('service') or 'incident')}.md"
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/incidents/{incident_id}/postmortem.pdf")
def incident_postmortem_pdf(incident_id: int):
    """The report as a PDF: the copy somebody prints, attaches or files after the incident.

    Typeset from exactly the same Markdown the console renders, so the downloaded document and the
    page on screen cannot disagree - which is the point of a report that outlives the incident.
    """
    md = postmortem.markdown(incident_id)
    if md is None:
        raise HTTPException(404, f"no incident {incident_id}")
    inc = db.get(incident_id)
    service = inc.get("service") or "incident"
    title = f"AIRA postmortem — incident #{incident_id} · {inc['alertname']} on {service}"
    try:
        pdf = postmortem_pdf.render(md, title=title)
    except Exception as e:  # noqa: BLE001 - a typesetting failure must name itself, not 500 blankly
        log.exception("incident %s: could not render the postmortem as PDF", incident_id)
        raise HTTPException(500, f"could not render the report as PDF: {type(e).__name__}: {e}"
                            ) from e
    name = f"postmortem-{incident_id}-{service}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/incidents/{incident_id}/actions")
def incident_actions(incident_id: int):
    """Every action proposed for this incident, and what became of each."""
    if not db.get(incident_id):
        raise HTTPException(404, "incident not found")
    return db.get_actions(incident_id)


@app.get("/memory/recall/{incident_id}")
async def memory_recall(incident_id: int, limit: int = 3):
    """Past cases resembling this incident.

    Qdrant runs embedded and takes an exclusive lock on its directory, so the backend is the only
    process that may open it. Everything else - the MCP tools server, the RCA agent, the
    evaluation harness - reaches memory through here. Read-only: this endpoint cannot write.
    """
    inc = db.get(incident_id)
    if not inc:
        raise HTTPException(404, f"no incident {incident_id}")
    if not config.MEMORY_ENABLED:
        return {"incident_id": incident_id, "cases": [], "detail": "memory is disabled"}
    try:
        cases = await asyncio.to_thread(memory.recall, inc, limit)
    except Exception as e:  # noqa: BLE001 - a caller must be able to carry on without memory
        raise HTTPException(503, f"memory unavailable: {type(e).__name__}: {e}") from e
    return {"incident_id": incident_id, "query": memory.symptom_key(inc), "cases": cases}


@app.get("/memory/stats")
async def memory_stats():
    """What is in the store, for the console and for the evaluation report."""
    if not config.MEMORY_ENABLED:
        return {"enabled": False}
    try:
        return {"enabled": True, **await asyncio.to_thread(memory.stats)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"memory unavailable: {type(e).__name__}: {e}") from e


@app.get("/incidents/{incident_id}/events")
def incident_events(incident_id: int, limit: int = 2000):
    """The execution trace: every callback-reported event for this incident, in order.

    Separate from /findings deliberately. Findings are what each agent concluded and are small
    enough for the console to poll; a trace carries prompts and responses and is read on demand
    when someone wants to know how a conclusion was reached.
    """
    if not db.get(incident_id):
        raise HTTPException(404, "incident not found")
    return db.get_events(incident_id, limit=limit)


@app.get("/incidents/{incident_id}/prompts")
def incident_prompts(incident_id: int):
    """Exactly what each agent was asked, per agent and round.

    Served separately from /findings for the same reason the trace is: a prompt carries the whole
    evidence block, and the console polls /incidents every three seconds. This is fetched only
    when a reader opens a prompt, so the poll stays small.
    """
    if not db.get(incident_id):
        raise HTTPException(404, "incident not found")
    return db.get_prompts(incident_id)


@app.get("/incidents/{incident_id}/findings")
def incident_findings(incident_id: int):
    if not db.get(incident_id):
        raise HTTPException(404, "incident not found")
    return db.get_findings(incident_id)


# Dependencies that only exist where the monitored stack does. In an archive deployment their
# absence is the design, not a fault, so they are reported as such rather than as failures.
LOCAL_ONLY = ("prometheus", "ollama", "mcp_tools", "tracing", "rca_a2a")


@app.get("/health")
async def health():
    checks = {}
    async with http_util.async_client(timeout=5) as client:
        try:
            r = await client.get(f"{PROMETHEUS_URL}/-/ready")
            checks["prometheus"] = {"ok": r.status_code == 200, "url": PROMETHEUS_URL, "detail": r.text.strip()}
        except Exception as e:  # noqa: BLE001
            checks["prometheus"] = {"ok": False, "url": PROMETHEUS_URL, "detail": str(e)}
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            checks["ollama"] = {"ok": OLLAMA_MODEL in models, "url": OLLAMA_URL, "model": OLLAMA_MODEL,
                                "detail": "model available" if OLLAMA_MODEL in models else f"model missing; have {models}"}
        except Exception as e:  # noqa: BLE001
            checks["ollama"] = {"ok": False, "url": OLLAMA_URL, "model": OLLAMA_MODEL, "detail": str(e)}
        # The evidence tools are reached over MCP. A dead tools server does not stop an
        # investigation - it falls back to direct calls and records that on the incident - but the
        # system is then not running the way it is meant to, so this does count against overall
        # health. Degraded and honest, rather than silently fine.
        try:
            # Bounded: without a cap, a dead tools server made /health take 2.5s while the MCP
            # client worked through its own retries, which is long enough for a status probe to
            # time out and report the backend itself as down. Health must stay fast whatever the
            # dependencies are doing.
            names = await asyncio.wait_for(mcp_client.tool_names(), timeout=1.5)
            checks["mcp_tools"] = {
                "ok": bool(names), "url": mcp_client.MCP_TOOLS_URL,
                "detail": f"tools advertised: {', '.join(names)}" if names
                          else "server answered but advertises no tools"}
        except Exception as e:  # noqa: BLE001
            # Unwrapped: the transport runs in an anyio task group, so the bare exception reads
            # "unhandled errors in a TaskGroup (1 sub-exception)" and says nothing useful.
            why = ("did not answer within 1.5s" if isinstance(e, asyncio.TimeoutError)
                   else mcp_client._why(e))
            checks["mcp_tools"] = {
                "ok": False, "url": mcp_client.MCP_TOOLS_URL,
                "detail": f"not reachable, evidence tools fall back to direct calls: {why}"[:200]}

        # Tracing is reported but deliberately does not count against overall health: an
        # investigation runs identically without it, and failing health for missing telemetry
        # would make a monitoring problem look like an outage.
        tstate = tracing.state()
        if not tstate.get("on"):
            detail = f"tracing OFF: {tstate.get('reason')}"
        elif await tracing.endpoint_reachable() or (
                tracing.retarget_if_moved() and await tracing.endpoint_reachable()):
            detail = "tracing on, exporting to the collector"
        else:
            # Probed rather than assumed: the investigation is unaffected either way, but saying
            # tracing is fine while every batch is being dropped would be a lie the console repeats.
            detail = "tracing on but the collector is unreachable - spans are being dropped"
        checks["tracing"] = {"ok": True, "url": tstate.get("endpoint"), "detail": detail}

        # The RCA agent is a separate process reached over A2A. Without it the root-cause step of
        # every investigation fails, so its absence belongs in this health report rather than only
        # in a log line after the fact.
        # Guarded on the module, not just wrapped in try/except: in archive mode `adk_base` is
        # None, and the except branch below dereferences it too - so an AttributeError there would
        # escape and turn /health itself into a 500. A health endpoint that cannot report ill
        # health is worse than no health endpoint.
        if adk_base is None:
            checks["rca_a2a"] = {"ok": True, "archive": True,
                                 "detail": "the RCA agent is not part of this deployment"}
        else:
            try:
                r = await client.get(adk_base.RCA_A2A_CARD)
                card = r.json() if r.status_code == 200 else {}
                name = card.get("name")
                checks["rca_a2a"] = {
                    "ok": bool(name), "url": adk_base.RCA_A2A_CARD,
                    "detail": f"agent card served: {name}" if name else f"HTTP {r.status_code}, no agent card"}
            except Exception as e:  # noqa: BLE001
                checks["rca_a2a"] = {"ok": False, "url": adk_base.RCA_A2A_CARD,
                                     "detail": f"not reachable: {e}"}

    # Incident memory. Reported as a dependency because a reader should be able to see whether an
    # investigation had prior cases available to it, but an empty or unreachable store is not an
    # unhealthy system: the pipeline runs without it, which is the whole point of the design.
    if not config.MEMORY_ENABLED:
        checks["memory"] = {"ok": True, "detail": "disabled by configuration"}
    else:
        try:
            st = await asyncio.to_thread(memory.stats)
            checks["memory"] = {
                "ok": True, "url": st["path"],
                "detail": f"{st['stored']} incidents stored, {st['retrievable']} retrievable "
                          f"({st['model']}, {st['dim']}d, cpu)"}
        except Exception as e:  # noqa: BLE001
            checks["memory"] = {"ok": False, "detail": f"unavailable, investigations run without "
                                                       f"it: {type(e).__name__}: {e}"}
    if config.ARCHIVE:
        for name in LOCAL_ONLY:
            if name in checks:
                checks[name] = {"ok": True, "archive": True,
                                "detail": "not part of this deployment; recorded incidents are "
                                          "served, new ones cannot be investigated"}
    return {"ok": all(c["ok"] for c in checks.values()),
            "mode": config.MODE, "checks": checks}


# ---------------------------------------------------------------- data for the metrics page

PROM_PANELS = {
    "request_rate": {
        "title": "Requests per second",
        "unit": "req/s",
        "query": 'sum by (service_name) (rate(traces_span_metrics_calls_total'
                 '{span_kind="SPAN_KIND_SERVER"}[2m]))',
    },
    "error_ratio": {
        "title": "Error ratio",
        "unit": "%",
        "scale": 100,
        "query": 'sum by (service_name) (rate(traces_span_metrics_calls_total'
                 '{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR"}[2m])) '
                 '/ sum by (service_name) (rate(traces_span_metrics_calls_total'
                 '{span_kind="SPAN_KIND_SERVER"}[2m])) > 0',
    },
    "latency_p95": {
        "title": "p95 latency",
        "unit": "ms",
        "query": 'histogram_quantile(0.95, sum by (service_name, le) (rate('
                 'traces_span_metrics_duration_milliseconds_bucket'
                 '{span_kind="SPAN_KIND_SERVER"}[2m])))',
    },
    "cpu": {
        "title": "CPU, % of one core",
        "unit": "%",
        "query": 'sum by (container_name) (rate(container_cpu_usage_nanoseconds_total[1m])) '
                 '/ 1e9 * 100',
    },
    "memory": {
        "title": "Memory",
        "unit": "MiB",
        "scale": 1 / 1048576,
        "query": "sum by (container_name) (container_memory_usage_total_bytes)",
    },
}


async def _prom_range(client, query: str, minutes: int, step: int) -> list[dict]:
    """One PromQL query over a window, shaped for a chart."""
    now = int(time.time())
    r = await client.get(f"{PROMETHEUS_URL}/api/v1/query_range", params={
        "query": query, "start": now - minutes * 60, "end": now, "step": step})
    r.raise_for_status()
    out = []
    for series in r.json()["data"]["result"]:
        labels = series.get("metric") or {}
        name = labels.get("service_name") or labels.get("container_name") or "(unnamed)"
        points = [[int(t), (None if v in ("NaN", "+Inf", "-Inf") else float(v))]
                  for t, v in series.get("values") or []]
        if any(p[1] is not None for p in points):
            out.append({"name": name, "points": points})
    out.sort(key=lambda s: -max((p[1] or 0) for p in s["points"]))
    return out


@app.get("/api/metrics/panels")
async def metrics_panels(minutes: int = 30, step: int = 30):
    """Every chart on the metrics page, in one request.

    One round trip rather than five: the page is a single view of one moment, and five independent
    requests would let the panels disagree with each other about what "now" is.
    """
    minutes = max(5, min(minutes, 180))
    step = max(15, min(step, 300))
    panels = []
    async with http_util.async_client(timeout=25) as client:
        for key, spec in PROM_PANELS.items():
            try:
                series = await _prom_range(client, spec["query"], minutes, step)
                scale = spec.get("scale", 1)
                if scale != 1:
                    for s_ in series:
                        s_["points"] = [[t, (None if v is None else v * scale)]
                                        for t, v in s_["points"]]
                panels.append({"key": key, "title": spec["title"], "unit": spec["unit"],
                               "series": series})
            except Exception as e:  # noqa: BLE001 - one bad panel must not empty the page
                panels.append({"key": key, "title": spec["title"], "unit": spec["unit"],
                               "series": [], "error": f"{type(e).__name__}: {e}"[:160]})
    return {"minutes": minutes, "step": step, "generated_at": db.now_iso(), "panels": panels}


@app.get("/api/metrics/services")
async def metrics_services():
    """A one-row-per-service health picture: traffic, errors, latency, and whether it is reporting."""
    queries = {
        "request_rate": 'sum by (service_name) (rate(traces_span_metrics_calls_total'
                        '{span_kind="SPAN_KIND_SERVER"}[2m]))',
        "error_ratio": 'sum by (service_name) (rate(traces_span_metrics_calls_total'
                       '{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR"}[2m])) '
                       '/ sum by (service_name) (rate(traces_span_metrics_calls_total'
                       '{span_kind="SPAN_KIND_SERVER"}[2m]))',
        "latency_p95": 'histogram_quantile(0.95, sum by (service_name, le) (rate('
                       'traces_span_metrics_duration_milliseconds_bucket'
                       '{span_kind="SPAN_KIND_SERVER"}[2m])))',
    }
    services: dict[str, dict] = {}
    async with http_util.async_client(timeout=20) as client:
        for key, q in queries.items():
            try:
                r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": q})
                r.raise_for_status()
                for item in r.json()["data"]["result"]:
                    name = (item.get("metric") or {}).get("service_name")
                    if not name:
                        continue
                    value = item["value"][1]
                    services.setdefault(name, {"service": name})[key] = (
                        None if value in ("NaN", "+Inf", "-Inf") else float(value))
            except Exception:  # noqa: BLE001
                continue
        # Which alerts are live, so a service can be marked as alerting.
        alerting: dict[str, list[str]] = {}
        try:
            r = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts")
            r.raise_for_status()
            for a in r.json()["data"]["alerts"]:
                labels = a.get("labels") or {}
                svc = labels.get("service_name")
                if svc:
                    alerting.setdefault(svc, []).append(labels.get("alertname"))
        except Exception:  # noqa: BLE001
            pass
    for name, row in services.items():
        row["alerts"] = alerting.get(name, [])
    return {"generated_at": db.now_iso(),
            "services": sorted(services.values(), key=lambda r: -(r.get("request_rate") or 0))}


@app.get("/api/traces/recent")
async def traces_recent(service: str = "aira", limit: int = 20, hours: int = 3):
    """Recent traces for one service, read from Jaeger.

    Defaults to `aira`, which is AIRA's own investigations rather than the shop's traffic - the
    thing this page exists to show.
    """
    base = "http://localhost:8080/jaeger/ui/api"
    now_us = int(time.time() * 1_000_000)
    out = []
    async with http_util.async_client(timeout=25) as client:
        try:
            r = await client.get(f"{base}/traces", params={
                "service": service, "limit": max(1, min(limit, 100)),
                "start": now_us - hours * 3600 * 1_000_000, "end": now_us})
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:  # noqa: BLE001
            return {"service": service, "traces": [], "error": f"{type(e).__name__}: {e}"[:200]}

        for t in data:
            spans = t.get("spans") or []
            if not spans:
                continue
            procs = t.get("processes") or {}
            root = min(spans, key=lambda s: s["startTime"])
            started = root["startTime"] // 1000
            tags = {x["key"]: x["value"] for x in root.get("tags", [])}
            out.append({
                "trace_id": t.get("traceID"),
                "name": root.get("operationName"),
                "started_ms": started,
                "duration_ms": round(root.get("duration", 0) / 1000, 1),
                "span_count": len(spans),
                "services": sorted({p.get("serviceName") for p in procs.values() if p.get("serviceName")}),
                "incident_id": tags.get("aira.incident_id"),
                "alertname": tags.get("aira.alertname"),
                "service": tags.get("aira.service"),
            })
    out.sort(key=lambda t: -t["started_ms"])
    return {"service": service, "generated_at": db.now_iso(), "traces": out}


@app.get("/api/traces/services")
async def traces_services():
    """Which services Jaeger has traces for, so the page can offer them."""
    async with http_util.async_client(timeout=15) as client:
        try:
            r = await client.get("http://localhost:8080/jaeger/ui/api/services")
            r.raise_for_status()
            names = r.json().get("data") or []
        except Exception as e:  # noqa: BLE001
            return {"services": [], "error": f"{type(e).__name__}: {e}"[:200]}
    aira = sorted(n for n in names if n.startswith("aira"))
    rest = sorted(n for n in names if not n.startswith("aira"))
    return {"services": aira + rest, "aira_services": aira}


@app.get("/api/dashboards")
async def dashboards():
    """Grafana's dashboards, so the page can link straight to the useful ones.

    Links rather than embeds: Grafana answers with `x-frame-options: deny`, and an embedded
    Grafana would look like Grafana rather than like this console. The charts that matter are drawn
    natively from the same Prometheus data instead.
    """
    async with http_util.async_client(timeout=15) as client:
        try:
            r = await client.get("http://localhost:8080/grafana/api/search",
                                 params={"type": "dash-db"})
            r.raise_for_status()
            items = r.json()
        except Exception as e:  # noqa: BLE001
            return {"dashboards": [], "error": f"{type(e).__name__}: {e}"[:200]}
    return {"dashboards": [{"title": d.get("title"), "uid": d.get("uid"),
                            "url": f"http://localhost:8080/grafana{d.get('url', '')}"}
                           for d in items]}


# ---------------------------------------------------------------- data for the mobile app
#
# Read-only views over the same tables (appdata.py). They exist because GET /incidents cannot page,
# filter or count, and sends every agent's full output for 100 incidents at once; a phone needs all
# incidents, a page at a time, in a light form. Nothing here duplicates or caches an incident.

@app.get("/api/summary")
def api_summary():
    """The dashboard's numbers, counted from the database now."""
    return appdata.summary()


@app.get("/api/incidents")
def api_incidents(limit: int = 30, offset: int = 0, state: str | None = None,
                  severity: str | None = None, q: str | None = None, since: str | None = None,
                  sort: str = "active"):
    """All incidents, a page at a time: light rows plus the total matching the filters."""
    if state is not None and state not in appdata.STATES:
        raise HTTPException(422, f"state must be one of {', '.join(appdata.STATES)}")
    if sort not in appdata.SORTS:
        raise HTTPException(422, f"sort must be one of {', '.join(appdata.SORTS)}")
    items, total = appdata.list_incidents(limit, offset, state, severity, q, since, sort)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/incidents/{incident_id}/row")
def api_incident_row(incident_id: int):
    row = appdata.incident_row(incident_id)
    if not row:
        raise HTTPException(404, "incident not found")
    return row


@app.get("/api/approvals")
def api_approvals():
    """Every remediation awaiting a human decision, with the diagnosis behind it."""
    return appdata.pending_approvals()


@app.get("/api/activity")
def api_activity(limit: int = 50, before: str | None = None):
    """Notable events across all incidents, newest first, from the stored event trace."""
    return appdata.activity(limit, before)


@app.get("/api/stream")
async def api_stream(request: Request):
    """Server-Sent Events: which incidents changed, as they change (see live.py).

    A connection lasts at most a few minutes and the browser reconnects by itself, sending the last
    id it saw; this keeps a stalled proxy or a sleeping phone from holding a dead stream open.
    """
    last_id = request.headers.get("last-event-id") or request.query_params.get("last")
    queue, backlog = live.HUB.subscribe(last_id)

    async def stream():
        try:
            yield "retry: 2000\n\n"
            hello = {"type": "hello", "boot": live.BOOT_ID, "seq": live.HUB.seq,
                     "id": f"{live.BOOT_ID}:{live.HUB.seq}",
                     "summary": await asyncio.to_thread(appdata.summary)}
            yield live.frame(hello)
            for m in backlog:
                yield live.frame(m)
            opened = time.monotonic()
            while time.monotonic() - opened < 240:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # A named event rather than an SSE comment: comments are invisible to the page,
                    # and the app needs to see the stream is alive to notice when it silently is not
                    # (a phone that paused the app in the background).
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield live.frame(message)
        finally:
            live.HUB.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/client-config")
def api_client_config(request: Request):
    """What the app needs to know about this server: no secrets, only what is switched on."""
    return {
        "mode": config.MODE,
        "auth_required": bool(config.ACCESS_TOKEN),
        "signed_in": (not config.ACCESS_TOKEN) or _token_ok(request)
                     or (config.TRUST_LOOPBACK and request.client is not None
                         and request.client.host in ("127.0.0.1", "::1")),
        "decision_label": DECISION_SOURCES["app"],
        "push_available": True,
        "secure_context_hint": request.url.scheme == "https"
                               or (request.url.hostname or "") in ("localhost", "127.0.0.1"),
        "boot": live.BOOT_ID,
    }


@app.get("/api/push/key")
def api_push_key():
    from . import webpush  # noqa: PLC0415

    return {"public_key": webpush.public_key_b64()}


@app.post("/api/push/subscribe")
async def api_push_subscribe(request: Request):
    """Store one device's push subscription (endpoint and keys from the browser's PushManager)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "body is not JSON")
    sub = body.get("subscription") if isinstance(body, dict) else None
    keys = (sub or {}).get("keys") or {}
    endpoint = str((sub or {}).get("endpoint") or "")
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(422, "a push subscription needs an https endpoint and p256dh/auth keys")
    appdata.save_subscription(endpoint, str(keys["p256dh"]), str(keys["auth"]),
                              request.headers.get("user-agent"))
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    endpoint = str((body or {}).get("endpoint") or "")
    return {"ok": True, "removed": appdata.delete_subscription(endpoint) if endpoint else False}


@app.post("/api/push/test")
async def api_push_test(request: Request):
    """Send one test notification to the device that asks (or report why it cannot)."""
    from . import webpush  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    endpoint = str((body or {}).get("endpoint") or "")
    sub = next((s for s in appdata.subscriptions() if s["endpoint"] == endpoint), None)
    if not sub:
        raise HTTPException(404, "this device is not subscribed")
    status = await webpush.send(sub, {"title": "AIRA notifications are on",
                                      "body": "You will be told about new incidents, approvals "
                                              "waiting and remediation results.",
                                      "level": "info", "key": f"test-{int(time.time())}",
                                      "url": "/app/#/settings"})
    return {"ok": 200 <= status < 300, "push_service_status": status}


# ---------------------------------------------------------------- the mobile app (PWA)
#
# Served from the repository's pwa/ folder at /app/. Its service worker is scoped to /app/, so it
# can never intercept the console's pages. If the folder is absent these routes answer 404 and the
# console is unaffected.

_PWA_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html",
              ".png": "image/png", ".webmanifest": "application/manifest+json",
              ".json": "application/json", ".svg": "image/svg+xml"}


def _pwa_file(name: str, headers: dict | None = None) -> FileResponse:
    root = config.PWA_DIR.resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file() or path.suffix not in _PWA_TYPES:
        raise HTTPException(404, "not found")
    # no-cache = check with the server first, so an updated app reaches phones on the next load.
    return FileResponse(path, media_type=_PWA_TYPES[path.suffix],
                        headers={"Cache-Control": "no-cache", **(headers or {})})


@app.get("/app")
def app_root():
    return Response(status_code=307, headers={"Location": "/app/"})


@app.get("/app/")
def app_index():
    return _pwa_file("index.html")


@app.get("/app/{name:path}")
def app_asset(name: str):
    return _pwa_file(name)


# ---------------------------------------------------------------- pages

@app.get("/")
def console():
    return FileResponse(STATIC / "index.html")


@app.get("/metrics")
def metrics_page():
    return FileResponse(STATIC / "metrics.html")


@app.get("/traces")
def traces_page():
    return FileResponse(STATIC / "traces.html")


@app.get("/style.css")
def stylesheet():
    return FileResponse(STATIC / "style.css", media_type="text/css")
