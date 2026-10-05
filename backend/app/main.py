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
from .config import AGENT_TIMEOUT_S, LOG_DIR, OLLAMA_MODEL, OLLAMA_URL, PROMETHEUS_URL

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
        db.update(incident_id, agent_status="running", agent_model=OLLAMA_MODEL, agent_started_at=db.now_iso())
        log.info("incident %s: triage agent started (%s)", incident_id, OLLAMA_MODEL)
        t0 = time.perf_counter()
        try:
            result, attempts, seconds = await asyncio.wait_for(
                asyncio.to_thread(triage_agent.run, db.get(incident_id)), timeout=AGENT_TIMEOUT_S)
            db.update(incident_id, agent_status="done", agent_output=result.model_dump(), agent_attempts=attempts,
                      agent_finished_at=db.now_iso(), agent_runtime_s=round(seconds, 2))
            log.info("incident %s: triage done in %.1fs (%d attempt(s)): %s",
                     incident_id, seconds, attempts, result.likely_cause_category)
        except triage_agent.TriageError as e:
            db.update(incident_id, agent_status="error", agent_error=str(e), agent_attempts=e.attempts,
                      agent_output={"raw_output": e.raw}, agent_finished_at=db.now_iso(),
                      agent_runtime_s=round(time.perf_counter() - t0, 2))
            log.error("incident %s: triage failed: %s", incident_id, e)
        except asyncio.TimeoutError:
            db.update(incident_id, agent_status="error", agent_finished_at=db.now_iso(),
                      agent_error=f"agent did not finish within {AGENT_TIMEOUT_S:.0f}s",
                      agent_runtime_s=round(time.perf_counter() - t0, 2))
            log.error("incident %s: triage timed out", incident_id)
        except Exception as e:  # noqa: BLE001 - e.g. Ollama unreachable
            db.update(incident_id, agent_status="error", agent_error=f"{type(e).__name__}: {e}",
                      agent_finished_at=db.now_iso(), agent_runtime_s=round(time.perf_counter() - t0, 2))
            log.exception("incident %s: triage crashed", incident_id)


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
    return db.list_incidents(limit)


@app.get("/incidents/{incident_id}")
def incident(incident_id: int):
    inc = db.get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    return inc


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
