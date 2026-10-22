"""AIRA backend: receives Alertmanager webhooks, stores incidents, gathers live evidence."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from . import db, prometheus_tool, triage_agent
from .agents import orchestrator
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
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="AIRA backend (mid-semester preview)", lifespan=lifespan)


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
        async with httpx.AsyncClient(timeout=10) as client:
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
            result, attempts, seconds = await asyncio.wait_for(
                asyncio.to_thread(triage_agent.run, db.get(incident_id)), timeout=AGENT_TIMEOUT_S)
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
            await orchestrator.investigate(incident_id, triage_out)
        except Exception:  # noqa: BLE001 - never let the investigation kill the webhook task
            log.exception("incident %s: orchestrated investigation failed", incident_id)


@app.post("/alerts")
async def alerts(request: Request, background: BackgroundTasks):
    """Alertmanager webhook receiver (webhook payload version 4)."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not JSON")
    db.log_webhook(payload)
    log.info("webhook: status=%s alerts=%d group=%s", payload.get("status"),
             len(payload.get("alerts", [])), payload.get("groupLabels"))

    results = []
    for alert in payload.get("alerts", []):
        if "fingerprint" not in alert or "startsAt" not in alert:
            log.warning("skipping alert without fingerprint/startsAt: %s", alert)
            continue
        incident_id, created = db.upsert_alert(alert)
        results.append({"incident_id": incident_id, "created": created, "status": alert.get("status")})
        log.info("incident %s %s: %s %s/%s", incident_id, "created" if created else "updated",
                 alert.get("status"), alert["labels"].get("alertname"), alert["labels"].get("service_name"))
        if created and alert.get("status") == "firing":
            background.add_task(investigate, incident_id)
    return {"received": len(results), "incidents": results}


@app.get("/incidents")
def incidents(limit: int = 100):
    items = db.list_incidents(limit)
    summary = db.findings_summary([i["id"] for i in items])
    for i in items:
        i["agents"] = summary.get(i["id"], {"agents": [], "running": None, "total_runtime_s": 0.0})
    return items


@app.get("/incidents/{incident_id}")
def incident(incident_id: int):
    inc = db.get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    inc["findings"] = db.get_findings(incident_id)
    return inc


@app.get("/incidents/{incident_id}/findings")
def incident_findings(incident_id: int):
    if not db.get(incident_id):
        raise HTTPException(404, "incident not found")
    return db.get_findings(incident_id)


@app.get("/health")
async def health():
    checks = {}
    async with httpx.AsyncClient(timeout=5) as client:
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
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


@app.get("/")
def console():
    return FileResponse(STATIC / "index.html")
