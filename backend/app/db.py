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
    trace_id             TEXT,                    -- the distributed trace for this investigation
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
    agent        TEXT NOT NULL,            -- triage | metrics | log | change | rca | critic | conclusion
    seq          INTEGER NOT NULL DEFAULT 0,  -- display order; skipped agents never start, so
                                              -- started_at cannot order the timeline
    round        INTEGER NOT NULL DEFAULT 1,  -- 2 when the Critic rejected round 1 and sent an
                                              -- investigator back out with a sharper question
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
-- The execution trace: what happened, in order, while an incident was investigated. This is a
-- different question from agent_findings, which records what each agent *concluded*. An event says
-- an agent started, a model was called with this prompt and answered that, a tool call crossed MCP
-- and took this long, output failed validation and was retried. Findings are the result; events are
-- the flow that produced it, which is what you need when the result is wrong.
CREATE TABLE IF NOT EXISTS agent_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    at          TEXT NOT NULL,            -- ISO 8601, when it happened
    source      TEXT NOT NULL,            -- crewai | adk | aira  (which mechanism reported it)
    kind        TEXT NOT NULL,            -- agent_started | model_call_completed | tool_call_* | ...
    agent       TEXT,                     -- the AIRA agent it belongs to, when known
    round       INTEGER NOT NULL DEFAULT 1,
    duration_ms REAL,
    ok          INTEGER,                  -- 1 success, 0 failure, NULL not applicable
    detail      TEXT                      -- JSON: the payload, truncated
);
CREATE INDEX IF NOT EXISTS idx_events_incident ON agent_events(incident_id, id);
-- Every action AIRA proposes against the running system, and what became of it. One row per
-- proposal, updated in place as it moves through proposed -> approved/rejected -> executed. The
-- row exists *before* anything runs, so an action that fails or never completes is still on record
-- rather than inferred from its absence.
CREATE TABLE IF NOT EXISTS remediation_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     INTEGER NOT NULL REFERENCES incidents(id),
    attempt         INTEGER NOT NULL DEFAULT 1,   -- capped at 2 by the verification loop
    proposed_at     TEXT NOT NULL,
    action          TEXT NOT NULL,                -- reset_feature_flag | restart_container | none
    target_service  TEXT,
    params          TEXT,                         -- JSON, validated against the allowlist
    risk            TEXT,
    reasoning       TEXT,                         -- the agent's own words
    expected_effect TEXT,
    reversal        TEXT,                         -- how to undo it, or why no undo is needed
    before_state    TEXT,                         -- JSON, captured before acting
    status          TEXT NOT NULL,                -- proposed | approved | rejected | executed
                                                  -- | failed | declined | refused
    decided_by      TEXT,                         -- who approved or rejected it
    decided_at      TEXT,
    decision_note   TEXT,
    executed_at     TEXT,
    result_ok       INTEGER,
    result_detail   TEXT,
    verified_by     TEXT,                         -- how the result was confirmed at the source
    after_state     TEXT,                         -- JSON, read back after acting
    duration_s      REAL,
    verification    TEXT,                         -- JSON: recovered | not_yet | not_recovered
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_incident ON remediation_actions(incident_id, id);
"""

JSON_COLUMNS = ("labels", "annotations", "evidence", "agent_output")
EVENT_JSON_COLUMNS = ("detail",)
ACTION_JSON_COLUMNS = ("params", "before_state", "after_state", "verification")
FINDING_JSON_COLUMNS = ("output", "evidence")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS will not add a column
# to an existing database, so each one is applied with an ALTER that is safe to run every start.
MIGRATIONS = (
    ("incidents", "trace_id", "TEXT"),
)


def init() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            existing = {r[1] for r in conn.execute(f"pragma table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col in JSON_COLUMNS:
        if d.get(col):
            d[col] = json.loads(d[col])
    return d


def log_webhook(payload) -> None:
    """Record a received webhook payload, whatever shape it arrived in.

    This runs before validation on purpose - a payload AIRA rejects is exactly the one worth
    keeping - so it must tolerate anything that parsed as JSON. It previously assumed a dict and
    crashed the handler with a 500 on a JSON array, before the guardrail could reject it.
    """
    status, count = None, 0
    if isinstance(payload, dict):
        status = payload.get("status")
        alerts = payload.get("alerts")
        count = len(alerts) if isinstance(alerts, (list, tuple)) else 0
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO webhook_log (received_at, status, alert_count, payload) VALUES (?, ?, ?, ?)",
            (now_iso(), status if isinstance(status, str) else None, count,
             json.dumps(payload, default=str)),
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


def add_event(incident_id: int, source: str, kind: str, agent: str | None = None,
              round_: int = 1, duration_ms: float | None = None, ok: bool | None = None,
              detail: dict | None = None) -> int:
    """Append one event to an incident's execution trace.

    Append-only and deliberately cheap: these fire from inside agent callbacks on the hot path, so
    nothing here reads, aggregates or locks more than the single insert.
    """
    with _lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent_events (incident_id, at, source, kind, agent, round, duration_ms, "
            "ok, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (incident_id, now_iso(), source, kind, agent, round_, duration_ms,
             None if ok is None else int(ok),
             json.dumps(detail, default=str) if detail is not None else None),
        )
        return cur.lastrowid


def get_events(incident_id: int, limit: int = 2000) -> list[dict]:
    """An incident's execution trace, in the order it happened."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_events WHERE incident_id = ? ORDER BY id LIMIT ?",
            (incident_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except json.JSONDecodeError:
                pass
        d["ok"] = None if d["ok"] is None else bool(d["ok"])
        out.append(d)
    return out


def event_counts(incident_ids: list[int]) -> dict[int, int]:
    """How many events each incident recorded, for the list view."""
    if not incident_ids:
        return {}
    marks = ", ".join("?" for _ in incident_ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT incident_id, COUNT(*) AS n FROM agent_events "
            f"WHERE incident_id IN ({marks}) GROUP BY incident_id", incident_ids).fetchall()
    return {r["incident_id"]: r["n"] for r in rows}


def add_action(incident_id: int, **fields) -> int:
    """Record a proposed action. Written before anything is executed."""
    for col in ACTION_JSON_COLUMNS:
        if col in fields and fields[col] is not None and not isinstance(fields[col], str):
            fields[col] = json.dumps(fields[col])
    now = now_iso()
    fields.setdefault("proposed_at", now)
    fields.setdefault("status", "proposed")
    fields["updated_at"] = now
    keys = ["incident_id", *fields]
    with _lock, connect() as conn:
        cur = conn.execute(
            f"INSERT INTO remediation_actions ({', '.join(keys)}) "
            f"VALUES ({', '.join('?' for _ in keys)})",
            [incident_id, *fields.values()])
        return cur.lastrowid


def update_action(action_id: int, **fields) -> None:
    """Move an action along: decided, executed, verified."""
    for col in ACTION_JSON_COLUMNS:
        if col in fields and fields[col] is not None and not isinstance(fields[col], str):
            fields[col] = json.dumps(fields[col])
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock, connect() as conn:
        conn.execute(f"UPDATE remediation_actions SET {cols} WHERE id = ?",
                     (*fields.values(), action_id))


def _action_row(row) -> dict:
    d = dict(row)
    for col in ACTION_JSON_COLUMNS:
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except json.JSONDecodeError:
                pass
    d["result_ok"] = None if d.get("result_ok") is None else bool(d["result_ok"])
    return d


def get_action(action_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM remediation_actions WHERE id = ?",
                           (action_id,)).fetchone()
    return _action_row(row) if row else None


def get_actions(incident_id: int) -> list[dict]:
    """Every action proposed for this incident, oldest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM remediation_actions WHERE incident_id = ? ORDER BY id",
            (incident_id,)).fetchall()
    return [_action_row(r) for r in rows]


def close_pending_actions(incident_id: int, reason: str) -> int:
    """Close any proposals still awaiting a decision on an incident that is over.

    An incident that resolved on its own, or was fixed by an action on a different incident with
    the same cause, leaves proposals nobody should approve any more: the condition they were
    written for has gone, and `execute()` would refuse them anyway. Leaving them on the console
    invites a human to click something pointless, which is how action 13 - a frontend restart for
    an incident that had resolved 20 minutes earlier - ended up still awaiting approval.
    """
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT id FROM remediation_actions WHERE incident_id = ? AND status = 'proposed'",
            (incident_id,)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE remediation_actions SET status = 'superseded', decided_at = ?, "
                "decided_by = 'AIRA (automatic)', decision_note = ?, updated_at = ? WHERE id = ?",
                (now_iso(), reason, now_iso(), r["id"]))
    return len(rows)


def get_actions_pending_like(action: str, target_service: str | None,
                             exclude_id: int) -> list[dict]:
    """Other proposals still awaiting a decision that would do the same thing.

    A single cause commonly raises several alerts, so several incidents propose the same remedy.
    Once one has been carried out the rest are moot.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM remediation_actions WHERE status = 'proposed' AND action = ? "
            "AND target_service IS ? AND id != ?",
            (action, target_service, exclude_id)).fetchall()
    return [_action_row(r) for r in rows]


def actions_summary(incident_ids: list[int]) -> dict[int, list[dict]]:
    """Actions for several incidents at once, for the list view (no N+1)."""
    if not incident_ids:
        return {}
    marks = ", ".join("?" for _ in incident_ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM remediation_actions WHERE incident_id IN ({marks}) ORDER BY id",
            incident_ids).fetchall()
    out: dict[int, list[dict]] = {i: [] for i in incident_ids}
    for r in rows:
        out[r["incident_id"]].append(_action_row(r))
    return out


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
            f"SELECT incident_id, agent, round, status, skip_reason, output, evidence_text, model, "
            f"runtime_s, attempts, error, started_at, finished_at "
            f"FROM agent_findings WHERE incident_id IN ({marks}) ORDER BY seq, id",
            incident_ids,
        ).fetchall()
    out: dict[int, dict] = {i: {"agents": [], "running": None, "total_runtime_s": 0.0} for i in incident_ids}
    for r in rows:
        s = out[r["incident_id"]]
        ev = r["evidence_text"] or ""
        s["agents"].append({
            "agent": r["agent"], "round": r["round"], "status": r["status"],
            "skip_reason": r["skip_reason"],
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


def close_orphaned_findings(skip_ids: tuple[int, ...] = ()) -> int:
    """Mark agent rows left 'pending' or 'running' by a killed backend as errors.

    A crash or a host sleeping mid-investigation leaves rows that no process will ever finish. The
    console renders those as "reading the evidence..." forever, which is a lie about a run that
    ended. Recording an honest error state instead is both truthful and visible.

    `skip_ids` are incidents the caller is about to resume; their rows will be rewritten by that
    run, so touching them here would only flicker an error onto the console.
    """
    reason = "backend stopped while this agent was running; the investigation did not finish"
    with _lock, connect() as conn:
        placeholders = ", ".join("?" for _ in skip_ids)
        where = "status IN ('pending', 'running')"
        params: list = []
        if skip_ids:
            where += f" AND incident_id NOT IN ({placeholders})"
            params = list(skip_ids)
        rows = conn.execute(
            f"SELECT id, incident_id, agent FROM agent_findings WHERE {where}", params
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE agent_findings SET status = 'error', error = ?, finished_at = ?, "
                "updated_at = ? WHERE id = ?",
                (reason, now_iso(), now_iso(), r["id"]),
            )
        # The triage headline on the incident itself is a separate copy of the same state.
        incidents = conn.execute(
            "SELECT id FROM incidents WHERE agent_status IN ('pending', 'running')"
            + (f" AND id NOT IN ({placeholders})" if skip_ids else ""),
            list(skip_ids),
        ).fetchall()
        for r in incidents:
            conn.execute(
                "UPDATE incidents SET agent_status = 'error', agent_error = ?, updated_at = ? "
                "WHERE id = ?",
                (reason, now_iso(), r["id"]),
            )
    return len(rows) + len(incidents)


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
        conn.execute("DELETE FROM remediation_actions")
        conn.execute("DELETE FROM agent_events")
        conn.execute("DELETE FROM agent_findings")
        conn.execute("DELETE FROM incidents")
        conn.execute("DELETE FROM webhook_log")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('incidents', 'webhook_log', 'agent_findings', 'agent_events', "
                     "'remediation_actions')")
        return n
