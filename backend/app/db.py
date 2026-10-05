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
"""

JSON_COLUMNS = ("labels", "annotations", "evidence", "agent_output")


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


def reset() -> int:
    with _lock, connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        conn.execute("DELETE FROM incidents")
        conn.execute("DELETE FROM webhook_log")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('incidents', 'webhook_log')")
        return n
