"""Incident memory: what AIRA has seen before, and what turned out to be true.

Every concluded investigation becomes one vector in Qdrant, so that a new incident can start from
the cases that resemble it rather than from nothing.

Three decisions shape this module, and each of them is load-bearing.

**What goes into the vector is the symptom, never the conclusion.**  At the moment a new incident
arrives it has an alert and a set of measurements; it has no root cause, because that is the thing
being worked out.  If the index were built from conclusions there would be nothing to query it
with.  So the embedded text is built by one function, `symptom_key`, used identically when storing
and when searching - the index and the query cannot drift apart, because they are the same code
path.  The conclusion is kept beside the vector as payload, which is what gets returned.

**No magnitudes anywhere near the vector.**  Two incidents with the same cause rarely have the
same numbers: 75% of calls failing and 100% of calls failing are the same fault.  Embedding the
digits pushes identical cases apart and clusters unrelated ones that happen to share a value.  So
`symptom_key` states direction against baseline - "errors far above baseline" - and `_scrub`
strips numeric literals out of any text that reaches the vector.  The same rule protects the
prompt later: this project has already been bitten by a model copying example numbers out of a
prompt instead of reading its data, and a retrieved case carrying its own measurements into the
RCA prompt is that same bug with extra steps.

**A rejected hypothesis is not a memory.**  Of the first 154 incidents, 62 reached a root cause
but the Critic accepted only 29 of them.  Storing all of them and retrieving all of them would
feed the model's own discarded guesses back into later investigations, and the quality of the
memory would fall the longer it ran.  So everything concluded is stored, with its verdict in the
payload, and `recall` filters to the accepted ones.  The rejected cases stay on record - they are
worth counting, and worth showing - without being allowed to influence anything.

Qdrant runs embedded, which takes an exclusive lock on the storage directory: only one process may
open it.  That process is the backend.  The MCP tools server and the RCA agent must reach memory
through the backend API, never by opening `QDRANT_PATH` themselves.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from . import config, db

log = logging.getLogger("aira.memory")

COLLECTION = "aira_incidents"

# Lazily built: importing fastembed costs a second or so and may download the model, which must
# not happen at backend import time.
_lock = threading.Lock()
_embedder: Any = None
_client: Any = None


class MemoryUnavailable(RuntimeError):
    """Memory could not be opened. Never fatal: an investigation runs without it."""


# ---------------------------------------------------------------- the vector side


def _get_embedder():
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                from fastembed import TextEmbedding

                config.FASTEMBED_CACHE.mkdir(parents=True, exist_ok=True)
                # Pinned to the CPU provider explicitly. fastembed's device default is "auto",
                # which would put this on the GPU beside the 7B and make Ollama evict it.
                _embedder = TextEmbedding(
                    config.EMBED_MODEL,
                    cache_dir=str(config.FASTEMBED_CACHE),
                    providers=["CPUExecutionProvider"],
                )
                log.info("embedding model ready: %s (cpu)", config.EMBED_MODEL)
    return _embedder


def embed(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _get_embedder().embed(texts)]


def _get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                from qdrant_client import QdrantClient, models

                config.QDRANT_PATH.mkdir(parents=True, exist_ok=True)
                try:
                    c = QdrantClient(path=str(config.QDRANT_PATH))
                except Exception as exc:  # already locked by another process
                    raise MemoryUnavailable(
                        f"cannot open {config.QDRANT_PATH}: {exc}. Qdrant runs embedded here, so "
                        "only one process may hold it - that process is the backend."
                    ) from exc
                if not c.collection_exists(COLLECTION):
                    c.create_collection(
                        COLLECTION,
                        vectors_config=models.VectorParams(
                            size=config.EMBED_DIM, distance=models.Distance.COSINE
                        ),
                    )
                    log.info("created collection %s", COLLECTION)
                _client = c
    return _client


def close() -> None:
    """Release the storage lock. Used by the backfill script so the backend can reopen it."""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None


# ---------------------------------------------------------------- building the key

# Three alternatives rather than one optional unit group: a word boundary cannot follow "%", so a
# single pattern leaves the sign behind and turns "set to 100%" into "set to %".
_NUMBER = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*%"
    r"|\b\d+(?:[.,]\d+)?\s*(?:ms|req/s|rps|GB|MB|MiB|KB|bytes?|seconds?|minutes?|hours?)\b"
    r"|\b\d+(?:[.,]\d+)?\b",
    re.IGNORECASE,
)


def scrub_magnitudes(text: str) -> str:
    """Remove numeric magnitudes, keeping the words around them.

    "75% of calls failed over the last 3 minutes" becomes "of calls failed over the last minutes".
    Ugly to read, but it is never read - it is embedded, and what it has lost is exactly the part
    that differs between two instances of the same fault.
    """
    return re.sub(r"\s{2,}", " ", _NUMBER.sub("", text or "")).strip(" .,;:")


def _band(now: float | None, base: float | None) -> str | None:
    """Where a measurement sits against its own baseline, as a phrase rather than a ratio."""
    if now is None or base is None:
        return None
    try:
        now, base = float(now), float(base)
    except (TypeError, ValueError):
        return None
    if base == 0:
        return "appeared" if now > 0 else None
    r = now / base
    if r >= 3:
        return "far above"
    if r >= 1.25:
        return "above"
    if r > 0.8:
        return None  # unremarkable: saying so would add noise to every key
    if r > 0.33:
        return "below"
    return "far below"


# The measurements worth describing, and what to call them. Order is fixed so that two incidents
# with the same deviations produce the same sentence.
_SIGNALS = [
    ("error_ratio", "error_ratio_baseline", "errors"),
    ("request_rate_rps", "request_rate_rps_baseline", "request rate"),
    ("latency_p95_ms", "latency_p95_ms_baseline", "p95 latency"),
    ("cpu_percent_of_core", "cpu_percent_of_core_baseline", "CPU"),
    ("memory_bytes", "memory_bytes_baseline", "memory"),
]


def symptom_key(inc: dict) -> str:
    """The text that is embedded, for storing and for searching alike.

    Built only from what exists before any agent has run - the alert and the collected evidence -
    because that is all a new incident has when it needs to ask what this resembles.
    """
    service = inc.get("service") or "unknown service"
    parts = [f"{inc.get('alertname') or 'alert'} on {service}"]

    summary = scrub_magnitudes(inc.get("summary") or "")
    if summary:
        parts.append(summary)

    ev = inc.get("evidence") or {}
    values = ev.get("values") or {}

    moves = []
    for now_k, base_k, label in _SIGNALS:
        b = _band(values.get(now_k), values.get(base_k))
        if b:
            moves.append(f"{label} {b} baseline")
    # A container that has stopped reporting is the defining symptom of a service being down, and
    # it has no baseline to compare against.
    stale = values.get("container_seconds_since_last_report")
    if isinstance(stale, (int, float)) and stale > 60:
        moves.append("container stopped reporting")
    parts.append("; ".join(moves) if moves else "no measurement deviated from its baseline")

    ops = [o.get("operation") for o in (ev.get("failing_operations") or []) if o.get("operation")]
    if ops:
        parts.append("failing operations: " + ", ".join(sorted(ops)[:4]))

    return ". ".join(parts)


# ---------------------------------------------------------------- what is remembered


def _latest(findings: list[dict], agent: str) -> dict | None:
    rows = [f for f in findings if f["agent"] == agent and f["status"] == "done"]
    return max(rows, key=lambda f: f.get("round") or 1) if rows else None


def case_of(incident_id: int) -> dict | None:
    """The record for one incident, or None when there is nothing worth remembering.

    "Nothing worth remembering" means no root cause was reached. An investigation that ran and
    concluded it could not explain the incident is a real outcome, but it is not a case that helps
    a future one.
    """
    inc = db.get(incident_id)
    if not inc:
        return None
    findings = db.get_findings(incident_id)
    rca = _latest(findings, "rca")
    if not rca:
        return None
    hyps = (rca.get("output") or {}).get("hypotheses") or []
    if not hyps:
        return None

    critic = _latest(findings, "critic")
    verdict = (critic.get("output") or {}).get("verdict") if critic else None
    rounds = len({f.get("round") or 1 for f in findings if f["agent"] != "conclusion"})

    # The action is the last one that was actually carried out, not the last proposed: a proposal
    # nobody approved says nothing about what fixes this fault.
    executed = [a for a in db.get_actions(incident_id) if a["status"] == "executed"]
    act = executed[-1] if executed else None
    worked = None
    if act and act.get("verification"):
        outcome = (act["verification"] or {}).get("outcome")
        worked = True if outcome == "recovered" else False if outcome == "not_recovered" else None

    return {
        "incident_id": incident_id,
        "alertname": inc.get("alertname"),
        "service": inc.get("service"),
        "severity": inc.get("severity"),
        "symptom": symptom_key(inc),
        "cause": hyps[0].get("statement") or "",
        "cause_confidence": hyps[0].get("confidence"),
        "verdict": verdict,
        "rounds": rounds,
        "action": act["action"] if act else None,
        "action_target": act.get("target_service") if act else None,
        "action_worked": worked,
        "resolved_at": inc.get("ends_at") or inc.get("updated_at"),
    }


def remember(incident_id: int) -> bool:
    """Store one concluded incident. Returns False when there was nothing to store."""
    case = case_of(incident_id)
    if not case:
        return False
    from qdrant_client import models

    vec = embed([case["symptom"]])[0]
    _get_client().upsert(
        COLLECTION,
        points=[models.PointStruct(id=incident_id, vector=vec, payload=case)],
    )
    return True


# Calibrated against the stored corpus rather than chosen: across 29 accepted cases, a neighbour
# that shares the alert type never scored below 0.811, while a deliberately unrelated query
# ("DiskQuotaExceeded on billing-archiver") never scored above 0.654. Nothing occupies the gap
# between, so the threshold sits inside it. The asymmetry matters - a retrieved case that does not
# belong actively misleads the RCA, whereas a missed one only forfeits a hint - so this is set to
# reject rather than to recall generously. Worth re-measuring as the corpus grows: it was fitted
# on twelve fault-type and service combinations.
MIN_SCORE = 0.75


def recall(inc: dict, limit: int = 3, min_score: float = MIN_SCORE) -> list[dict]:
    """Past cases that resemble this incident, best first.

    Only cases whose cause the Critic accepted are returned, and the incident itself is excluded -
    an incident that has been investigated twice would otherwise retrieve its own conclusion and
    present it as corroboration.
    """
    from qdrant_client import models

    query = symptom_key(inc)
    flt = models.Filter(
        must=[models.FieldCondition(key="verdict", match=models.MatchValue(value="accept"))],
        must_not=(
            [models.FieldCondition(key="incident_id", match=models.MatchValue(value=inc["id"]))]
            if inc.get("id") is not None
            else []
        ),
    )
    res = _get_client().query_points(
        COLLECTION,
        query=embed([query])[0],
        query_filter=flt,
        limit=limit,
        score_threshold=min_score,
        with_payload=True,
    )
    return [dict(p.payload, score=round(p.score, 4)) for p in res.points]


def stats() -> dict:
    """What is actually in the store. Used by /health and by the backfill script."""
    from qdrant_client import models

    c = _get_client()
    total = c.count(COLLECTION, exact=True).count
    accepted = c.count(
        COLLECTION,
        count_filter=models.Filter(
            must=[models.FieldCondition(key="verdict", match=models.MatchValue(value="accept"))]
        ),
        exact=True,
    ).count
    return {
        "stored": total,
        "retrievable": accepted,
        "model": config.EMBED_MODEL,
        "dim": config.EMBED_DIM,
        "path": str(config.QDRANT_PATH),
    }
