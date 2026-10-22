"""SQLite storage for incidents and raw webhook payloads."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint          TEXT NOT NULL,
    starts_at            TEXT NOT NULL,
    ends_at              TEXT,
    status               TEXT NOT NULL,           -- firing | resolved
    alertname            TEXT NOT NULL,
    service              TEXT,
    severity             TEXT,
    summary              TEXT,
    description          TEXT,
    labels               TEXT NOT NULL,           -- JSON
    annotations          TEXT NOT NULL,           -- JSON
    generator_url        TEXT,
    first_received_at    TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    evidence_text        TEXT,
    evidence             TEXT,                    -- JSON
    evidence_collected_at TEXT,
    agent_status         TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | error
    agent_output         TEXT,                    -- JSON (validated TriageResult)
    agent_error          TEXT,
    agent_model          TEXT,
    agent_attempts       INTEGER,
    agent_started_at     TEXT,
    agent_finished_at    TEXT,
    agent_runtime_s      REAL,
    UNIQUE (fingerprint, starts_at)
);
CREATE TABLE IF NOT EXISTS webhook_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    status      TEXT,
    alert_count INTEGER,
    payload     TEXT NOT NULL
);
-- Shared incident context: one row per agent per incident, holding what that agent was given,
-- what it concluded, and how long it took. The incidents.agent_* columns above are kept as the
-- triage headline so existing incidents and the console keep working; triage writes to both.
CREATE TABLE IF NOT EXISTS agent_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  INTEGER NOT NULL REFERENCES incidents(id),
    agent        TEXT NOT NULL,            -- triage | metrics | log | change | orchestrator
    seq          INTEGER NOT NULL DEFAULT 0,  -- display order; skipped agents never start, so
                                              -- started_at cannot order the timeline
    round        INTEGER NOT NULL DEFAULT 1,  -- always 1 now; lets a later replanning phase re-run
    status       TEXT NOT NULL,            -- pending | running | done | error | skipped
    skip_reason  TEXT,
    output       TEXT,                     -- JSON, validated agent result
    evidence     TEXT,                     -- JSON, the raw data this agent used
    evidence_text TEXT,
    model        TEXT,
    runtime_s    REAL,
    attempts     INTEGER,
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (incident_id, agent, round)
);
CREATE INDEX IF NOT EXISTS idx_findings_incident ON agent_findings(incident_id);
"""

JSON_COLUMNS = ("labels", "annotations", "evidence", "agent_output")
FINDING_JSON_COLUMNS = ("output", "evidence")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col in JSON_COLUMNS:
        if d.get(col):
            d[col] = json.loads(d[col])
    return d


def log_webhook(payload: dict) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO webhook_log (received_at, status, alert_count, payload) VALUES (?, ?, ?, ?)",
            (now_iso(), payload.get("status"), len(payload.get("alerts", [])), json.dumps(payload)),
        )


def upsert_alert(alert: dict) -> tuple[int, bool]:
    """Create or update the incident for one Alertmanager alert.

    An incident is one alert instance: same fingerprint and same startsAt. If the same alert
    fires again later, Alertmanager gives it a new startsAt, so it becomes a new incident.
    Returns (incident_id, created).
    """
    labels = alert.get("labels", {})
    ann = alert.get("annotations", {})
    status = alert.get("status", "firing")
    ends_at = alert.get("endsAt") if status == "resolved" else None
    ts = now_iso()
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT id FROM incidents WHERE fingerprint = ? AND starts_at = ?",
            (alert["fingerprint"], alert["startsAt"]),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE incidents SET status = ?, ends_at = ?, annotations = ?, summary = ?, "
                "description = ?, updated_at = ? WHERE id = ?",
                (status, ends_at, json.dumps(ann), ann.get("summary"), ann.get("description"), ts, row["id"]),
            )
            return row["id"], False
        cur = conn.execute(
            "INSERT INTO incidents (fingerprint, starts_at, ends_at, status, alertname, service, severity, "
            "summary, description, labels, annotations, generator_url, first_received_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                alert["fingerprint"], alert["startsAt"], ends_at, status,
                labels.get("alertname", "unknown"), labels.get("service_name"), labels.get("severity"),
                ann.get("summary"), ann.get("description"), json.dumps(labels), json.dumps(ann),
                alert.get("generatorURL"), ts, ts,
            ),
        )
        return cur.lastrowid, True


def update(incident_id: int, **fields) -> None:
    for col in JSON_COLUMNS:
        if col in fields and fields[col] is not None and not isinstance(fields[col], str):
            fields[col] = json.dumps(fields[col])
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock, connect() as conn:
        conn.execute(f"UPDATE incidents SET {cols} WHERE id = ?", (*fields.values(), incident_id))


def get(incident_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone())


def list_incidents(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY (status = 'firing') DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- agent findings


def _finding_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col in FINDING_JSON_COLUMNS:
        if d.get(col):
            d[col] = json.loads(d[col])
    return d


def upsert_finding(incident_id: int, agent: str, round_: int = 1, **fields) -> int:
    """Create or update one agent's row for an incident. Returns the finding id.

    Upsert rather than insert so resuming an interrupted investigation after a backend restart
    updates the existing row instead of adding a duplicate.
    """
    for col in FINDING_JSON_COLUMNS:
        if col in fields and fields[col] is not None and not isinstance(fields[col], str):
            fields[col] = json.dumps(fields[col])
    fields["updated_at"] = now_iso()
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT id FROM agent_findings WHERE incident_id = ? AND agent = ? AND round = ?",
            (incident_id, agent, round_),
        ).fetchone()
        if row:
            if fields:
                cols = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE agent_findings SET {cols} WHERE id = ?", (*fields.values(), row["id"]))
            return row["id"]
        fields.setdefault("status", "pending")
        keys = ["incident_id", "agent", "round", *fields]
        values = [incident_id, agent, round_, *fields.values()]
        placeholders = ", ".join("?" for _ in keys)
        cur = conn.execute(
            f"INSERT INTO agent_findings ({', '.join(keys)}) VALUES ({placeholders})", values
        )
        return cur.lastrowid


def get_findings(incident_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_findings WHERE incident_id = ? ORDER BY seq, id", (incident_id,)
        ).fetchall()
    return [_finding_row(r) for r in rows]


EVIDENCE_PREVIEW = 1500


def findings_summary(incident_ids: list[int]) -> dict[int, dict]:
    """Per-incident agent timeline for the list view, in one query (no N+1).

    Carries each agent's validated output and a truncated evidence preview, which is everything the
    console needs to render a full investigation. The raw evidence JSON is deliberately left out:
    the page refreshes every few seconds and those blobs are large. GET /incidents/{id} returns them
    in full.
    """
    if not incident_ids:
        return {}
    marks = ", ".join("?" for _ in incident_ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT incident_id, agent, status, skip_reason, output, evidence_text, model, "
            f"runtime_s, attempts, error, started_at, finished_at "
            f"FROM agent_findings WHERE incident_id IN ({marks}) ORDER BY seq, id",
            incident_ids,
        ).fetchall()
    out: dict[int, dict] = {i: {"agents": [], "running": None, "total_runtime_s": 0.0} for i in incident_ids}
    for r in rows:
        s = out[r["incident_id"]]
        ev = r["evidence_text"] or ""
        s["agents"].append({
            "agent": r["agent"], "status": r["status"], "skip_reason": r["skip_reason"],
            "output": json.loads(r["output"]) if r["output"] else None,
            "evidence_preview": ev[:EVIDENCE_PREVIEW] + ("\n... (truncated)" if len(ev) > EVIDENCE_PREVIEW else ""),
            "model": r["model"], "runtime_s": r["runtime_s"], "attempts": r["attempts"],
            "error": r["error"], "started_at": r["started_at"], "finished_at": r["finished_at"],
        })
        if r["status"] == "running":
            s["running"] = r["agent"]
        if r["runtime_s"]:
            s["total_runtime_s"] = round(s["total_runtime_s"] + r["runtime_s"], 2)
    return out


def backfill_triage_findings() -> int:
    """Give incidents created before the multi-agent phase a 'triage' finding row.

    Idempotent: the upsert key means running it repeatedly changes nothing. Without it the
    timeline would be empty for every incident recorded before this phase.
    """
    created = 0
    for inc in list_incidents(10000):
        if inc["agent_status"] in (None, "pending"):
            continue
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM agent_findings WHERE incident_id = ? AND agent = 'triage' AND round = 1",
                (inc["id"],),
            ).fetchone()
        if exists:
            continue
        upsert_finding(
            inc["id"], "triage", seq=10, status=inc["agent_status"], output=inc["agent_output"],
            evidence_text=inc["evidence_text"], model=inc["agent_model"], runtime_s=inc["agent_runtime_s"],
            attempts=inc["agent_attempts"], started_at=inc["agent_started_at"],
            finished_at=inc["agent_finished_at"], error=inc["agent_error"],
        )
        created += 1
    return created


def reset() -> int:
    with _lock, connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        conn.execute("DELETE FROM agent_findings")
        conn.execute("DELETE FROM incidents")
        conn.execute("DELETE FROM webhook_log")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('incidents', 'webhook_log', 'agent_findings')")
        return n
