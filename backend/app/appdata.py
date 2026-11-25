"""Read-only views of the incident database for the mobile app (and anything else that wants them).

Nothing here writes, and nothing here holds a copy of anything: every answer is computed from the
same SQLite tables the console and the agents use, at the moment it is asked for. The mobile app
therefore cannot disagree with the console about an incident, an approval or a result.

Why these exist alongside GET /incidents: that endpoint returns every agent's full output for the
latest 100 incidents in one response, which is right for the console's polling but cannot page
through all incidents, filter them, or count them. The queries below can, and they return a
light row per incident so a phone on a mobile connection is not sent megabytes it will not show.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import db

# ---------------------------------------------------------------- incident list

SORTS = {
    # The console's own order: anything still firing first, then newest.
    "active": "(i.status = 'firing') DESC, i.id DESC",
    "latest": "i.id DESC",
    "oldest": "i.id ASC",
    "severity": "(i.severity = 'critical') DESC, (i.status = 'firing') DESC, i.id DESC",
}

STATES = ("firing", "resolved", "pending_approval", "investigating")

_LIGHT_ROW = """
SELECT i.id, i.alertname, i.service, i.severity, i.status, i.starts_at, i.ends_at, i.updated_at,
       i.first_received_at, i.summary, i.description, i.agent_status,
       (SELECT f.output FROM agent_findings f
         WHERE f.incident_id = i.id AND f.agent = 'conclusion'
         ORDER BY f.round DESC, f.id DESC LIMIT 1)                           AS conclusion,
       EXISTS (SELECT 1 FROM agent_findings f
                WHERE f.incident_id = i.id AND f.status IN ('running', 'pending')) AS investigating,
       (SELECT COUNT(*) FROM remediation_actions a
         WHERE a.incident_id = i.id AND a.status = 'proposed')               AS pending_approvals,
       (SELECT json_object('id', a.id, 'status', a.status, 'action', a.action,
                           'target_service', a.target_service, 'decided_by', a.decided_by,
                           'decision_source', a.decision_source,
                           'outcome', json_extract(a.verification, '$.outcome'))
          FROM remediation_actions a WHERE a.incident_id = i.id
         ORDER BY a.id DESC LIMIT 1)                                         AS last_action
  FROM incidents i
"""


def _light(row) -> dict:
    d = dict(row)
    d["investigating"] = bool(d["investigating"])
    for col in ("conclusion", "last_action"):
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except json.JSONDecodeError:
                d[col] = None
    c = d.get("conclusion") or {}
    d["conclusion"] = ({"outcome": c.get("outcome"), "statement": c.get("statement")}
                       if c else None)
    return d


def list_incidents(limit: int = 30, offset: int = 0, state: str | None = None,
                   severity: str | None = None, q: str | None = None, since: str | None = None,
                   sort: str = "active") -> tuple[list[dict], int]:
    """One page of incidents, light rows, plus the total that match the filters."""
    where, args = [], []
    if state in ("firing", "resolved"):
        where.append("i.status = ?"); args.append(state)
    elif state == "pending_approval":
        where.append("EXISTS (SELECT 1 FROM remediation_actions a "
                     "WHERE a.incident_id = i.id AND a.status = 'proposed')")
    elif state == "investigating":
        where.append("EXISTS (SELECT 1 FROM agent_findings f "
                     "WHERE f.incident_id = i.id AND f.status IN ('running', 'pending'))")
    if severity:
        where.append("i.severity = ?"); args.append(severity)
    if since:
        where.append("i.first_received_at >= ?"); args.append(since)
    if q:
        q = q.strip().lstrip("#")
        like = f"%{q}%"
        cond = ("(i.service LIKE ? OR i.alertname LIKE ? OR i.summary LIKE ? "
                "OR i.description LIKE ? OR i.severity LIKE ?")
        qargs = [like, like, like, like, like]
        if q.isdigit():
            cond += " OR i.id = ?"; qargs.append(int(q))
        where.append(cond + ")"); args.extend(qargs)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = SORTS.get(sort, SORTS["active"])
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM incidents i{clause}", args).fetchone()[0]
        rows = conn.execute(f"{_LIGHT_ROW}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
                            [*args, limit, offset]).fetchall()
    return [_light(r) for r in rows], total


def incident_row(incident_id: int) -> dict | None:
    """The same light row for one incident (used to patch a list after a live update)."""
    with db.connect() as conn:
        row = conn.execute(f"{_LIGHT_ROW} WHERE i.id = ?", (incident_id,)).fetchone()
    return _light(row) if row else None


# ---------------------------------------------------------------- dashboard numbers

def summary() -> dict:
    """Every number on the app's dashboard, counted from the tables at this moment."""
    with db.connect() as conn:
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        out = {
            "total": one("SELECT COUNT(*) FROM incidents"),
            "firing": one("SELECT COUNT(*) FROM incidents WHERE status = 'firing'"),
            "resolved": one("SELECT COUNT(*) FROM incidents WHERE status = 'resolved'"),
            "critical_firing": one("SELECT COUNT(*) FROM incidents "
                                   "WHERE status = 'firing' AND severity = 'critical'"),
            "pending_approvals": one("SELECT COUNT(*) FROM remediation_actions "
                                     "WHERE status = 'proposed'"),
            "investigating": one(
                "SELECT COUNT(DISTINCT incident_id) FROM agent_findings "
                "WHERE status IN ('running', 'pending')"),
            # Approved and being carried out right now (the approval request is still running it).
            "executing": one("SELECT COUNT(*) FROM remediation_actions WHERE status = 'approved'"),
            # Carried out, and AIRA is waiting to see whether the service recovers.
            "verifying": one(
                "SELECT COUNT(*) FROM remediation_actions WHERE status = 'executed' "
                "AND json_extract(verification, '$.outcome') = 'not_yet'"),
            "executed": one("SELECT COUNT(*) FROM remediation_actions WHERE status = 'executed'"),
            "recovered": one(
                "SELECT COUNT(*) FROM remediation_actions "
                "WHERE json_extract(verification, '$.outcome') = 'recovered'"),
        }
    out["as_of"] = db.now_iso()
    return out


# ---------------------------------------------------------------- pending approvals

def pending_approvals() -> list[dict]:
    """Every proposal awaiting a human, with what a person needs to decide on it.

    The diagnosis is the RCA agent's latest summary and the investigation's conclusion, exactly as
    stored; nothing is summarised again here.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT a.*, i.alertname, i.service, i.severity, i.status AS incident_status, "
            "       i.starts_at, i.summary AS incident_summary "
            "  FROM remediation_actions a JOIN incidents i ON i.id = a.incident_id "
            " WHERE a.status = 'proposed' ORDER BY a.id DESC").fetchall()
        out = []
        for r in rows:
            d = db._action_row(r)  # noqa: SLF001 - same JSON decoding as everywhere else
            for agent, key in (("rca", "rca"), ("conclusion", "conclusion"), ("critic", "critic")):
                f = conn.execute(
                    "SELECT output FROM agent_findings WHERE incident_id = ? AND agent = ? "
                    "AND status = 'done' ORDER BY round DESC, id DESC LIMIT 1",
                    (d["incident_id"], agent)).fetchone()
                d[key] = json.loads(f["output"]) if f and f["output"] else None
            out.append(d)
    return out


# ---------------------------------------------------------------- activity

# The events that are worth telling a person about. Everything else in agent_events (model calls,
# tool calls, retries) is the execution trace, shown on the incident itself.
ACTIVITY_KINDS = (
    "investigation_started", "investigation_finished", "action_proposed", "action_approved",
    "action_rejected", "action_executed", "action_refused", "action_failed",
    "verification_started", "verification_recovered", "verification_not_recovered",
    "remediation_exhausted", "postmortem_written", "postmortem_failed",
)


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def describe(item: dict) -> dict:
    """Title, body and level for one activity item: what the app lists and what a push shows."""
    kind, d = item["kind"], item.get("detail") or {}
    where = f"{item.get('service') or 'unknown'} · {item.get('alertname') or ''}".strip(" ·")
    action = d.get("action")
    target = d.get("target")
    what = f"{action} on {target}" if action and target else (action or "")
    level, title = "info", kind.replace("_", " ")
    body = where
    if kind == "incident_opened":
        crit = (item.get("severity") == "critical")
        level, title = ("critical" if crit else "warning"), \
            ("Critical incident" if crit else "New incident")
        body = f"#{item['incident_id']} {where}"
    elif kind == "incident_resolved":
        level, title, body = "good", "Incident resolved", f"#{item['incident_id']} {where}"
    elif kind == "investigation_started":
        title = "Investigation started"
    elif kind == "investigation_finished":
        title = "Investigation finished" + (" · cause accepted" if d.get("accepted") else "")
    elif kind == "action_proposed":
        level, title = "approval", "Approval needed"
        body = f"{what} · #{item['incident_id']} {where}"
    elif kind == "action_approved":
        level, title = "good", f"Approved by {d.get('approved_by') or 'a developer'}"
        body = f"{what} · #{item['incident_id']}"
    elif kind == "action_rejected":
        title = f"Rejected by {d.get('rejected_by') or 'a developer'}"
        body = f"{what} · #{item['incident_id']}"
    elif kind == "action_executed":
        level = "good" if item.get("ok") else "bad"
        title = "Remediation executed" if item.get("ok") else "Remediation ran, not confirmed"
        body = f"{what} · {d.get('result') or ''}".strip(" ·")
    elif kind == "action_refused":
        level, title = "bad", "Refused by the safety layer"
        body = f"{what} · {d.get('reason') or ''}".strip(" ·")
    elif kind == "action_failed":
        level, title = "bad", "Remediation failed"
        body = f"{what} · {d.get('error') or ''}".strip(" ·")
    elif kind == "verification_started":
        title = "Verifying recovery"
        body = f"#{item['incident_id']} {where} · waiting up to {d.get('window_s', '?')}s"
    elif kind == "verification_recovered":
        level, title = "good", "Recovery verified"
        body = d.get("detail") or where
    elif kind == "verification_not_recovered":
        level, title = "bad", "Not recovered"
        body = d.get("detail") or where
    elif kind == "remediation_exhausted":
        level, title = "bad", "Remediation stopped after two attempts"
    elif kind == "postmortem_written":
        level, title = "info", "Postmortem ready"
        body = f"#{item['incident_id']} {d.get('title') or where}"
    elif kind == "postmortem_failed":
        level, title = "bad", "Postmortem failed"
    return {"title": title, "body": str(body)[:300], "level": level}


def activity(limit: int = 50, before: str | None = None) -> list[dict]:
    """Recent notable events across all incidents, newest first.

    Built from agent_events plus each incident's opening and resolution, all of which are stored
    when they happen, so this list is the same whenever and from wherever it is asked for.
    """
    limit = max(1, min(int(limit), 200))
    kinds = ", ".join("?" for _ in ACTIVITY_KINDS)
    with db.connect() as conn:
        ev = conn.execute(
            f"SELECT e.id, e.incident_id, e.at, e.kind, e.ok, e.detail, i.service, i.alertname, "
            f"       i.severity FROM agent_events e JOIN incidents i ON i.id = e.incident_id "
            f" WHERE e.kind IN ({kinds}) ORDER BY e.id DESC LIMIT ?",
            (*ACTIVITY_KINDS, limit * 2)).fetchall()
        opened = conn.execute(
            "SELECT id, first_received_at AS at, service, alertname, severity FROM incidents "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        resolved = conn.execute(
            "SELECT id, COALESCE(ends_at, updated_at) AS at, service, alertname, severity "
            "FROM incidents WHERE status = 'resolved' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    items = []
    for r in ev:
        try:
            detail = json.loads(r["detail"]) if r["detail"] else {}
        except json.JSONDecodeError:
            detail = {}
        items.append({"key": f"e{r['id']}", "incident_id": r["incident_id"], "at": r["at"],
                      "kind": r["kind"], "ok": None if r["ok"] is None else bool(r["ok"]),
                      "detail": detail, "service": r["service"], "alertname": r["alertname"],
                      "severity": r["severity"]})
    for r in opened:
        items.append({"key": f"o{r['id']}", "incident_id": r["id"], "at": r["at"],
                      "kind": "incident_opened", "ok": None, "detail": {}, "service": r["service"],
                      "alertname": r["alertname"], "severity": r["severity"]})
    for r in resolved:
        items.append({"key": f"r{r['id']}", "incident_id": r["id"], "at": r["at"],
                      "kind": "incident_resolved", "ok": True, "detail": {},
                      "service": r["service"], "alertname": r["alertname"],
                      "severity": r["severity"]})
    items.sort(key=lambda x: _ts(x["at"]), reverse=True)
    if before:
        cutoff = _ts(before)
        items = [x for x in items if _ts(x["at"]) < cutoff]
    items = items[:limit]
    for x in items:
        x.update(describe(x))
        # The trace detail can be large and carry log text; the list only needs the description.
        x.pop("detail", None)
    return items


# ---------------------------------------------------------------- push subscriptions

def save_subscription(endpoint: str, p256dh: str, auth: str, user_agent: str | None) -> None:
    with db._lock, db.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at, user_agent) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
            "auth = excluded.auth, user_agent = excluded.user_agent, failures = 0",
            (endpoint, p256dh, auth, db.now_iso(), (user_agent or "")[:200]))


def delete_subscription(endpoint: str) -> bool:
    with db._lock, db.connect() as conn:  # noqa: SLF001
        return conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?",
                            (endpoint,)).rowcount > 0


def subscriptions() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM push_subscriptions").fetchall()]


def mark_subscription(endpoint: str, ok: bool) -> None:
    with db._lock, db.connect() as conn:  # noqa: SLF001
        if ok:
            conn.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 "
                         "WHERE endpoint = ?", (db.now_iso(), endpoint))
        else:
            conn.execute("UPDATE push_subscriptions SET failures = failures + 1 "
                         "WHERE endpoint = ?", (endpoint,))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
