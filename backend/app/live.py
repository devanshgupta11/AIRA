"""Live updates: telling every open console and mobile app that something changed.

**The database is the only source of truth.** A live message never carries state for a client to
trust; it says *which incidents changed* (plus the dashboard counts, which are cheap and computed
fresh), and the client then reads the authoritative state through the ordinary API. A client that
missed messages, reconnected, or opened a second tab therefore cannot end up showing anything the
database does not hold.

**Where changes come from.** Rather than hooking every write path, a watcher reads the tables
once a second for rows written since its last look: incidents, agent findings, remediation actions
and the event trace. That covers every writer - the webhook, the orchestrator and its agents, both
approval endpoints, the verification loop, and scripts run from outside this process - with no
change to any of them. A second of latency is well inside what a human approving an action
notices, and the queries touch only recently updated rows.

**Transport.** Server-Sent Events (one-way, plain HTTP, reconnect built into every browser), on
GET /api/stream. Each message has an id of the form `<boot>:<seq>`. A reconnecting client sends the
last id it saw; if this process still holds the messages after it they are replayed, otherwise -
the backend restarted, or the client was away too long - it is told to `resync`, i.e. re-read
everything. Either way it converges on the database.

**Push.** The same watcher spots events a person should hear about (a new incident, an approval
waiting, a result) and hands them to webpush.py for any device that subscribed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

from . import appdata, db

log = logging.getLogger("aira.live")

BOOT_ID = uuid.uuid4().hex[:10]
POLL_S = 1.0
HISTORY = 500           # messages kept for replay to a reconnecting client
CLIENT_QUEUE = 200      # messages a slow client may fall behind by before it is told to resync

# What gets a push notification. Investigation progress is in-app activity only: a phone buzzing for
# every agent step would be noise.
PUSH_KINDS = {
    "incident_opened", "incident_resolved", "action_proposed", "action_approved",
    "action_rejected", "action_executed", "action_refused", "action_failed",
    "verification_recovered", "verification_not_recovered", "remediation_exhausted",
}


class Hub:
    """Fan-out of change messages to every connected client, with a short replay history."""

    def __init__(self) -> None:
        self.seq = 0
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self.clients: set[asyncio.Queue] = set()

    def publish(self, message: dict) -> None:
        self.seq += 1
        message = {**message, "seq": self.seq, "boot": BOOT_ID, "id": f"{BOOT_ID}:{self.seq}"}
        self.history.append(message)
        for q in list(self.clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Too far behind to catch up message by message: drop its backlog and make it
                # re-read everything, which is always correct.
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync", "reason": "client fell behind",
                              "seq": self.seq, "boot": BOOT_ID, "id": f"{BOOT_ID}:{self.seq}"})

    def subscribe(self, last_id: str | None) -> tuple[asyncio.Queue, list[dict]]:
        """A queue for a new connection, and what it missed (or a resync if that is unknowable)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.clients.add(q)
        backlog: list[dict] = []
        if last_id:
            boot, _, seq = last_id.partition(":")
            oldest = self.history[0]["seq"] if self.history else self.seq + 1
            if boot != BOOT_ID or not seq.isdigit() or int(seq) < oldest - 1:
                backlog = [{"type": "resync", "reason": "missed updates while disconnected",
                            "seq": self.seq, "boot": BOOT_ID, "id": f"{BOOT_ID}:{self.seq}"}]
            else:
                backlog = [m for m in self.history if m["seq"] > int(seq)]
        return q, backlog

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)


HUB = Hub()


# ---------------------------------------------------------------- the watcher

def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class _Scanner:
    """Finds what changed since the last scan. Runs in a worker thread (blocking SQLite reads)."""

    def __init__(self) -> None:
        with db.connect() as conn:
            self.last_event = conn.execute("SELECT COALESCE(MAX(id), 0) FROM agent_events").fetchone()[0]
            self.last_incident = conn.execute("SELECT COALESCE(MAX(id), 0) FROM incidents").fetchone()[0]
            self.status = {r[0]: r[1] for r in conn.execute("SELECT id, status FROM incidents")}
        self.sigs: dict[tuple, str] = {}

    def scan(self) -> dict:
        # updated_at is written in whole seconds, so look back a few seconds and use the row
        # signatures to tell genuinely new writes from ones already reported.
        since = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
        changed: set[int] = set()
        notable: list[str] = []      # activity keys worth telling a person about
        with db.connect() as conn:
            for r in conn.execute("SELECT id, status, updated_at FROM incidents "
                                  "WHERE updated_at >= ? OR id > ?", (since, self.last_incident)):
                key = ("i", r["id"])
                sig = f"{r['status']}|{r['updated_at']}"
                if self.sigs.get(key) == sig:
                    continue
                self.sigs[key] = sig
                changed.add(r["id"])
                if r["id"] > self.last_incident:
                    notable.append(f"o{r['id']}")
                elif self.status.get(r["id"]) == "firing" and r["status"] == "resolved":
                    notable.append(f"r{r['id']}")
                self.status[r["id"]] = r["status"]
            self.last_incident = max([self.last_incident, *[i for i in changed]])

            for r in conn.execute("SELECT incident_id, agent, round, status, updated_at "
                                  "FROM agent_findings WHERE updated_at >= ?", (since,)):
                key = ("f", r["incident_id"], r["agent"], r["round"])
                sig = f"{r['status']}|{r['updated_at']}"
                if self.sigs.get(key) != sig:
                    self.sigs[key] = sig
                    changed.add(r["incident_id"])

            for r in conn.execute("SELECT id, incident_id, status, updated_at, verification "
                                  "FROM remediation_actions WHERE updated_at >= ?", (since,)):
                key = ("a", r["id"])
                sig = f"{r['status']}|{r['updated_at']}|{r['verification'] or ''}"
                if self.sigs.get(key) != sig:
                    self.sigs[key] = sig
                    changed.add(r["incident_id"])

            rows = conn.execute("SELECT id, incident_id, kind FROM agent_events WHERE id > ? "
                                "ORDER BY id LIMIT 5000", (self.last_event,)).fetchall()
            for r in rows:
                changed.add(r["incident_id"])
                if r["kind"] in appdata.ACTIVITY_KINDS:
                    notable.append(f"e{r['id']}")
            if rows:
                self.last_event = rows[-1]["id"]

        # Forget signatures old enough that they can no longer be re-read.
        if len(self.sigs) > 20000:
            self.sigs.clear()
        return {"incidents": sorted(changed), "notable": notable}


async def _watch() -> None:
    scanner = await asyncio.to_thread(_Scanner)
    log.info("live updates on (boot %s): watching the database every %.0fs", BOOT_ID, POLL_S)
    while True:
        await asyncio.sleep(POLL_S)
        try:
            found = await asyncio.to_thread(scanner.scan)
            if not found["incidents"]:
                continue
            activity = []
            if found["notable"]:
                wanted = set(found["notable"])
                recent = await asyncio.to_thread(appdata.activity, 80)
                activity = [a for a in recent if a["key"] in wanted]
            HUB.publish({"type": "change", "incidents": found["incidents"],
                         "activity": activity,
                         "summary": await asyncio.to_thread(appdata.summary)})
            to_push = [a for a in activity if a["kind"] in PUSH_KINDS]
            if to_push:
                asyncio.create_task(_push(to_push))
        except Exception:  # noqa: BLE001 - the watcher must outlive any one bad read
            log.exception("live update scan failed; continuing")


async def _push(items: list[dict]) -> None:
    from . import webpush  # noqa: PLC0415

    try:
        if not await asyncio.to_thread(appdata.subscriptions):
            return
        for a in reversed(items):   # oldest first, so a phone shows them in the order they happened
            await webpush.broadcast(
                {"title": a["title"], "body": a["body"], "level": a["level"], "key": a["key"],
                 "incident_id": a["incident_id"], "kind": a["kind"],
                 "url": f"/app/#/incident/{a['incident_id']}"},
                urgency="high" if a["level"] in ("critical", "approval") else "normal")
    except Exception:  # noqa: BLE001 - push is optional
        log.exception("push notification failed")


_task: asyncio.Task | None = None


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_watch())


def stop() -> None:
    if _task is not None:
        _task.cancel()


# ---------------------------------------------------------------- SSE framing

def frame(message: dict) -> str:
    event = message.get("type", "change")
    return f"id: {message['id']}\nevent: {event}\ndata: {json.dumps(message, default=str)}\n\n"
