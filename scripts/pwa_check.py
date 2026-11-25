"""Checks for the mobile app's backend: shared approval, live updates, data endpoints, access, push.

Runs against a running AIRA backend and the SQLite file that backend uses, and compares what the API
says with what the database holds. It creates its own remediation proposals to decide on, and every
one targets `prometheus`, which the safety layer refuses to act on - so approving them exercises the
whole approval path (claim, record, re-validate, refuse) without ever touching a container.

    backend\\.venv\\Scripts\\python.exe scripts\\pwa_check.py --base http://127.0.0.1:8011 --db <path to that backend's aira.db>

Use a scratch backend (a copy of the database, AIRA_MODE=archive, another port) rather than the
live one: the proposals it creates are real rows in whatever database it is pointed at.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[bool, str, str]] = []
# Windows consoles default to a code page that cannot print every character a page may contain.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          {detail}" if detail and not ok else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{title}")


class Env:
    def __init__(self, base: str, db_path: str):
        self.base = base.rstrip("/")
        self.db_path = db_path
        self.http = httpx.Client(base_url=self.base, timeout=30)

    def db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def one(self, sql: str, *args):
        with self.db() as c:
            return c.execute(sql, args).fetchone()[0]

    _pool: list[int] | None = None

    def new_action(self) -> tuple[int, int]:
        """A proposal the safety layer will refuse (target: prometheus). Returns (action, incident).

        Each test proposal goes on a different resolved incident, so counting that incident's events
        afterwards says exactly how many times this one decision ran.
        """
        with self.db() as c:
            if self._pool is None:
                self._pool = [r[0] for r in c.execute(
                    "SELECT id FROM incidents WHERE status = 'resolved' ORDER BY id")]
            incident_id = self._pool.pop(0)
            now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            cur = c.execute(
                "INSERT INTO remediation_actions (incident_id, attempt, proposed_at, action, "
                "target_service, params, risk, reasoning, expected_effect, reversal, status, updated_at) "
                "VALUES (?, 9, ?, 'restart_container', 'prometheus', '{}', 'medium', "
                "'pwa_check test proposal: the safety layer always refuses prometheus', "
                "'none - this is a test', 'nothing to reverse', 'proposed', ?)",
                (incident_id, now, now))
            return cur.lastrowid, incident_id

    def new_incident(self) -> int:
        """A firing critical incident written the way the webhook writes one (db.upsert_alert)."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        with self.db() as c:
            return c.execute(
                "INSERT INTO incidents (fingerprint, starts_at, status, alertname, service, severity, "
                "summary, description, labels, annotations, first_received_at, updated_at) "
                "VALUES (?, ?, 'firing', 'PwaCheckAlert', 'pwa-check-svc', 'critical', "
                "'pwa_check test incident', 'written by scripts/pwa_check.py', '{}', '{}', ?, ?)",
                (uuid.uuid4().hex[:16], now, now, now)).lastrowid

    def resolve_incident(self, incident_id: int) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        with self.db() as c:
            c.execute("UPDATE incidents SET status='resolved', ends_at=?, updated_at=? WHERE id=?",
                      (now, now, incident_id))

    def action(self, action_id: int) -> dict:
        with self.db() as c:
            return dict(c.execute("SELECT * FROM remediation_actions WHERE id = ?", (action_id,)).fetchone())

    def events(self, incident_id: int, kind: str, action_id: int) -> int:
        """How many `kind` events this test proposal produced (its incident carries no other)."""
        a = self.action(action_id)
        with self.db() as c:
            rows = c.execute("SELECT detail FROM agent_events WHERE incident_id = ? AND kind = ? "
                             "AND at >= ?", (incident_id, kind, a["proposed_at"])).fetchall()
        return sum(1 for r in rows if json.loads(r["detail"] or "{}").get("target") == "prometheus")

    def decide(self, action_id: int, kind: str, **body) -> httpx.Response:
        return self.http.post(f"/actions/{action_id}/{kind}", json=body)


# ---------------------------------------------------------------- data endpoints

def test_data(e: Env) -> None:
    section("Data endpoints match the database")
    s = e.http.get("/api/summary").json()
    check("summary: total incidents", s["total"] == e.one("SELECT COUNT(*) FROM incidents"), str(s["total"]))
    check("summary: firing", s["firing"] == e.one("SELECT COUNT(*) FROM incidents WHERE status='firing'"))
    check("summary: resolved", s["resolved"] == e.one("SELECT COUNT(*) FROM incidents WHERE status='resolved'"))
    check("summary: pending approvals",
          s["pending_approvals"] == e.one("SELECT COUNT(*) FROM remediation_actions WHERE status='proposed'"))

    ids, offset, total = [], 0, None
    while True:
        page = e.http.get("/api/incidents", params={"limit": 50, "offset": offset, "sort": "latest"}).json()
        total = page["total"]
        ids += [i["id"] for i in page["items"]]
        offset += 50
        if offset >= total or not page["items"]:
            break
    with e.db() as c:
        db_ids = [r[0] for r in c.execute("SELECT id FROM incidents ORDER BY id DESC")]
    check("incidents: every incident is reachable by paging", ids == db_ids,
          f"api {len(ids)} vs db {len(db_ids)}")
    check("incidents: no duplicates across pages", len(ids) == len(set(ids)))

    for state, sql in (("firing", "status='firing'"), ("resolved", "status='resolved'")):
        n = e.http.get("/api/incidents", params={"state": state, "limit": 1}).json()["total"]
        check(f"incidents: filter state={state}", n == e.one(f"SELECT COUNT(*) FROM incidents WHERE {sql}"))
    n = e.http.get("/api/incidents", params={"severity": "critical", "limit": 1}).json()["total"]
    check("incidents: filter severity=critical",
          n == e.one("SELECT COUNT(*) FROM incidents WHERE severity='critical'"))
    some = db_ids[len(db_ids) // 2]
    hit = e.http.get("/api/incidents", params={"q": f"#{some}"}).json()["items"]
    check("incidents: search by #id finds it", some in [i["id"] for i in hit])
    svc = e.one("SELECT service FROM incidents WHERE id = ?", some)
    n = e.http.get("/api/incidents", params={"q": svc, "limit": 1}).json()["total"]
    check("incidents: search by service", n >= e.one(
        "SELECT COUNT(*) FROM incidents WHERE service = ?", svc), f"{svc}: {n}")
    check("incidents: invalid state is refused", e.http.get("/api/incidents", params={"state": "bogus"}).status_code == 422)
    olds = e.http.get("/api/incidents", params={"sort": "oldest", "limit": 3}).json()["items"]
    check("incidents: sort oldest", [i["id"] for i in olds] == sorted(db_ids)[:3])

    for iid in db_ids[:3] + db_ids[-2:]:
        full = e.http.get(f"/incidents/{iid}", params={"include_events": "false"}).json()
        with e.db() as c:
            nf = c.execute("SELECT COUNT(*) FROM agent_findings WHERE incident_id=?", (iid,)).fetchone()[0]
            na = c.execute("SELECT COUNT(*) FROM remediation_actions WHERE incident_id=?", (iid,)).fetchone()[0]
            ne = c.execute("SELECT COUNT(*) FROM agent_events WHERE incident_id=?", (iid,)).fetchone()[0]
        check(f"detail #{iid}: findings, actions and event count match the database",
              len(full["findings"]) == nf and len(full["actions"]) == na and full["event_count"] == ne
              and "events" not in full)
    legacy = e.http.get(f"/incidents/{db_ids[0]}").json()
    check("detail: the console's default response still includes the trace", "events" in legacy)
    check("detail: unknown incident is 404", e.http.get("/incidents/999999?include_events=false").status_code == 404)

    act = e.http.get("/api/activity", params={"limit": 20}).json()
    check("activity: newest first, each with a title", all(a.get("title") for a in act) and
          [a["at"] for a in act] == sorted([a["at"] for a in act], key=lambda t: t, reverse=True) or len(act) <= 1,
          json.dumps([a["at"] for a in act][:5]))


# ---------------------------------------------------------------- shared approval

def test_approval(e: Env) -> None:
    section("Shared approval: one backend decision, attributed, atomic")

    aid, iid = e.new_action()
    listed = [a for a in e.http.get("/api/approvals").json() if a["id"] == aid]
    check("a new proposal appears in pending approvals with its incident", bool(listed)
          and listed[0]["incident_id"] == iid and "service" in listed[0])

    r = e.decide(aid, "approve", source="app", incident_id=iid, request_id=str(uuid.uuid4()))
    a = e.action(aid)
    check("approve from the app: accepted, safety layer refuses prometheus",
          r.status_code == 200 and r.json().get("status") == "refused", r.text[:200])
    check("approve from the app: recorded as developer (app), source app",
          a["decided_by"] == "developer (app)" and a["decision_source"] == "app")
    check("approve from the app: approved once and re-validated once",
          e.events(iid, "action_approved", aid) == 1 and e.events(iid, "action_refused", aid) == 1)
    check("approve from the app: it no longer appears as pending",
          aid not in [x["id"] for x in e.http.get("/api/approvals").json()])

    aid, iid = e.new_action()
    r = e.decide(aid, "approve", source="console", incident_id=iid, request_id=str(uuid.uuid4()))
    a = e.action(aid)
    check("approve from the console: recorded as developer (console)",
          r.status_code == 200 and a["decided_by"] == "developer (console)" and a["decision_source"] == "console")

    for src in ("app", "console"):
        aid, iid = e.new_action()
        r = e.decide(aid, "reject", source=src, incident_id=iid, request_id=str(uuid.uuid4()), note="test")
        a = e.action(aid)
        check(f"reject from the {src}: nothing executed, recorded as developer ({src})",
              r.status_code == 200 and a["status"] == "rejected" and a["decided_by"] == f"developer ({src})"
              and a["decision_note"] == "test" and e.events(iid, "action_refused", aid) == 0
              and e.events(iid, "action_executed", aid) == 0)

    # ---- first valid decision wins
    def race(calls):
        aid, iid = e.new_action()
        out = [None] * len(calls)
        barrier = threading.Barrier(len(calls))

        def go(n, kind, src):
            with httpx.Client(base_url=e.base, timeout=30) as c:
                barrier.wait()
                out[n] = c.post(f"/actions/{aid}/{kind}", json={"source": src, "incident_id": iid,
                                                                  "request_id": str(uuid.uuid4())})
        ts = [threading.Thread(target=go, args=(n, k, s)) for n, (k, s) in enumerate(calls)]
        [t.start() for t in ts]; [t.join() for t in ts]
        return aid, iid, out

    aid, iid, out = race([("approve", "app")] * 5 + [("approve", "console")] * 5)
    codes = sorted(r.status_code for r in out)
    a = e.action(aid)
    check("10 simultaneous approvals (5 app, 5 console): exactly one accepted",
          codes.count(200) == 1 and codes.count(409) == 9, str(codes))
    check("...and the remediation step ran exactly once",
          e.events(iid, "action_approved", aid) == 1 and e.events(iid, "action_refused", aid) == 1,
          f"approved={e.events(iid, 'action_approved', aid)} refused={e.events(iid, 'action_refused', aid)}")
    losers = [r.json() for r in out if r.status_code == 409]
    check("...every other request is told who decided first",
          all("already" in x["detail"] and a["decided_by"] in x["detail"] and x["action"]["id"] == aid
              for x in losers), losers[0]["detail"] if losers else "")

    aid, iid, out = race([("approve", "app"), ("reject", "console")] * 5)
    codes = sorted(r.status_code for r in out)
    a = e.action(aid)
    check("approve and reject at the same moment: exactly one decision lands",
          codes.count(200) == 1 and codes.count(409) == 9 and a["status"] in ("refused", "rejected"),
          f"{codes} -> {a['status']}")

    # ---- idempotent retry
    aid, iid = e.new_action()
    rid = str(uuid.uuid4())
    r1 = e.decide(aid, "approve", source="app", incident_id=iid, request_id=rid)
    r2 = e.decide(aid, "approve", source="app", incident_id=iid, request_id=rid)
    check("a retried request (same request id) is recognised, not refused",
          r1.status_code == 200 and r2.status_code == 200 and r2.json().get("duplicate") is True)
    check("...and nothing ran twice", e.events(iid, "action_approved", aid) == 1
          and e.events(iid, "action_refused", aid) == 1)
    r3 = e.decide(aid, "approve", source="console", incident_id=iid, request_id=str(uuid.uuid4()))
    check("a different request after the decision is refused with 409", r3.status_code == 409)

    # ---- validation
    aid, iid = e.new_action()
    check("a decision aimed at the wrong incident is refused",
          e.decide(aid, "approve", source="app", incident_id=iid - 1).status_code == 409
          and e.action(aid)["status"] == "proposed")
    check("an unknown source is refused", e.decide(aid, "approve", source="phone").status_code == 422
          and e.action(aid)["status"] == "proposed")
    check("an unknown action is 404", e.decide(99999999, "approve", source="app").status_code == 404)
    r = e.decide(aid, "reject", by="regression script")
    check("an older caller without 'source' keeps its own label",
          r.status_code == 200 and e.action(aid)["decided_by"] == "regression script")


# ---------------------------------------------------------------- live updates (SSE)

async def read_events(client: httpx.AsyncClient, url: str, headers: dict, until, timeout: float):
    got = []
    async with client.stream("GET", url, headers=headers, timeout=timeout + 5) as resp:
        event, data, eid = None, [], None
        start = time.monotonic()
        async for line in resp.aiter_lines():
            if time.monotonic() - start > timeout:
                break
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line.startswith("id:"):
                eid = line[3:].strip()
            elif line == "" and data:
                msg = {"event": event, "id": eid, **json.loads("".join(data))}
                got.append(msg)
                event, data, eid = None, [], None
                if until(got):
                    break
    return got


def test_live(e: Env) -> None:
    section("Live updates (Server-Sent Events)")

    async def run():
        async with httpx.AsyncClient(base_url=e.base) as client:
            aid, iid = e.new_action()

            async def act_soon():
                await asyncio.sleep(1.5)
                await asyncio.to_thread(e.decide, aid, "approve", source="app", incident_id=iid,
                                        request_id=str(uuid.uuid4()))
            t0 = time.monotonic()
            task = asyncio.create_task(act_soon())
            got = await read_events(client, "/api/stream", {}, lambda g: any(
                m["event"] == "change" and iid in m.get("incidents", [])
                and any(a.get("kind") == "action_refused" for a in m.get("activity", [])) for m in g), 20)
            await task
            dt = time.monotonic() - t0 - 1.5
            hello = got[0] if got else {}
            check("the stream opens with a hello carrying the dashboard numbers",
                  hello.get("event") == "hello" and "summary" in hello)
            changes = [m for m in got if m["event"] == "change" and iid in m.get("incidents", [])]
            check("a decision made through the API reaches a connected client", bool(changes),
                  f"{len(got)} messages")
            check("...within a few seconds", bool(changes) and dt < 6, f"{dt:.1f}s")
            acts = [a for m in changes for a in m.get("activity", [])]
            check("...with the attributed activity item",
                  any(a["title"] == "Approved by developer (app)" for a in acts),
                  json.dumps([a["title"] for a in acts]))

            last = changes[0]["id"] if changes else hello.get("id")
            boot, _, seq = last.partition(":")
            aid2, iid2 = e.new_action()
            await asyncio.to_thread(e.decide, aid2, "reject", source="console", incident_id=iid2)
            await asyncio.sleep(2.5)
            got = await read_events(client, "/api/stream", {"Last-Event-ID": f"{boot}:{int(seq) - 1}"},
                                    lambda g: any(m["event"] == "change" and iid2 in m.get("incidents", []) for m in g), 6)
            check("a reconnecting client is sent what it missed (Last-Event-ID)",
                  any(m["event"] == "change" and iid2 in m.get("incidents", []) for m in got))
            got = await read_events(client, "/api/stream", {"Last-Event-ID": "not-this-boot:5"},
                                    lambda g: any(m["event"] == "resync" for m in g), 4)
            check("a client from before a restart is told to re-read everything (resync)",
                  any(m["event"] == "resync" for m in got))

            # A new incident is what a phone most needs to hear about.
            async def open_incident_soon():
                await asyncio.sleep(1.5)
                await asyncio.to_thread(e.new_incident)
            task = asyncio.create_task(open_incident_soon())
            got = await read_events(client, "/api/stream", {}, lambda g: any(
                a.get("kind") == "incident_opened" for m in g for a in m.get("activity", [])), 15)
            await task
            opened = [a for m in got for a in m.get("activity", []) if a.get("kind") == "incident_opened"]
            check("a new incident reaches connected apps as an alert", bool(opened))
            check("...titled for a person, with service and alert",
                  bool(opened) and opened[0]["title"] == "Critical incident" and "pwa-check-svc" in opened[0]["body"],
                  json.dumps(opened[:1]))
            iid_new = opened[0]["incident_id"] if opened else None

            async def resolve_soon():
                await asyncio.sleep(1.5)
                await asyncio.to_thread(e.resolve_incident, iid_new)
            task = asyncio.create_task(resolve_soon())
            got = await read_events(client, "/api/stream", {}, lambda g: any(
                a.get("kind") == "incident_resolved" and a.get("incident_id") == iid_new
                for m in g for a in m.get("activity", [])), 15)
            await task
            check("its resolution is announced too", any(
                a.get("kind") == "incident_resolved" and a.get("incident_id") == iid_new
                for m in got for a in m.get("activity", [])))

            got = await read_events(client, "/api/stream", {}, lambda g: any(m["event"] == "ping" for m in g), 20)
            check("the stream sends a visible keep-alive (ping) so a phone can detect a dead connection",
                  any(m["event"] == "ping" for m in got))
    asyncio.run(run())


# ---------------------------------------------------------------- access token (in-process)

def test_access() -> None:
    section("Access token (only when AIRA_ACCESS_TOKEN is set)")
    sys.path.insert(0, str(ROOT / "backend"))
    os.environ.setdefault("AIRA_MODE", "archive")
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from app import config, main  # noqa: PLC0415

    saved = config.ACCESS_TOKEN
    try:
        config.ACCESS_TOKEN = None
        c = TestClient(main.app)     # its client address is "testclient", i.e. not this machine
        check("no token configured: requests from another address are served as before",
              c.get("/api/summary").status_code == 200)
        config.ACCESS_TOKEN = "s3cret-test-token"
        c = TestClient(main.app)
        check("token configured: data needs it", c.get("/api/summary").status_code == 401)
        check("token configured: approval needs it", c.post("/actions/1/approve", json={}).status_code == 401)
        check("token configured: a browser is shown the sign-in page",
              c.get("/", headers={"accept": "text/html"}).status_code == 401
              and "Sign in" in c.get("/", headers={"accept": "text/html"}).text)
        check("token configured: the Alertmanager webhook stays open (validated as before)",
              c.post("/alerts", json=["not", "a", "dict"]).status_code == 400)
        check("token configured: /health and the app shell stay open",
              c.get("/health").status_code != 401 and c.get("/app/").status_code == 200)
        check("token configured: the right header is accepted",
              c.get("/api/summary", headers={"x-aira-token": "s3cret-test-token"}).status_code == 200)
        check("token configured: a wrong token is refused",
              c.get("/api/summary", headers={"x-aira-token": "nope"}).status_code == 401)
        check("token configured: a wrong token cannot open a session",
              c.post("/api/session", json={"token": "nope"}).status_code == 401)
        r = c.post("/api/session", json={"token": "s3cret-test-token"})
        check("token configured: the right token opens a session cookie",
              r.status_code == 200 and "aira_token" in r.headers.get("set-cookie", "")
              and "httponly" in r.headers.get("set-cookie", "").lower())
        check("token configured: the session cookie is then accepted", c.get("/api/summary").status_code == 200)
    finally:
        config.ACCESS_TOKEN = saved


# ---------------------------------------------------------------- push

def test_push(e: Env) -> None:
    section("Web Push")
    import base64, hashlib, hmac, struct  # noqa: E401, PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: PLC0415
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
    from app import webpush  # noqa: PLC0415

    key = e.http.get("/api/push/key").json()["public_key"]
    check("the server publishes a 65-byte P-256 application server key", len(webpush.b64u_decode(key)) == 65)
    bad = e.http.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://insecure.example/x", "keys": {}}})
    check("a subscription without https and keys is refused", bad.status_code == 422)

    ua = ec.generate_private_key(ec.SECP256R1())
    ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    endpoint = f"https://push.invalid/pwa-check-{uuid.uuid4().hex}"
    ok = e.http.post("/api/push/subscribe", json={"subscription": {"endpoint": endpoint, "keys": {
        "p256dh": webpush.b64u(ua_pub), "auth": webpush.b64u(auth)}}})
    check("a valid subscription is stored", ok.status_code == 200 and e.one(
        "SELECT COUNT(*) FROM push_subscriptions WHERE endpoint = ?", endpoint) == 1)
    rm = e.http.post("/api/push/unsubscribe", json={"endpoint": endpoint}).json()
    check("...and removed again", rm.get("removed") is True)

    msg = json.dumps({"title": "Approval needed", "body": "restart_container on payment"}).encode()
    blob = webpush.encrypt(msg, webpush.b64u(ua_pub), webpush.b64u(auth))
    salt, idlen = blob[:16], blob[20]
    as_pub, ct = blob[21:21 + idlen], blob[21 + idlen:]
    secret = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    H = lambda k, m: hmac.new(k, m, hashlib.sha256).digest()  # noqa: E731
    prk = H(salt, H(H(auth, secret), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01"))
    pt = AESGCM(H(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
        H(prk, b"Content-Encoding: nonce\x00\x01")[:12], ct, None)
    check("a message encrypted for a device decrypts on that device (RFC 8291)", pt == msg + b"\x02")
    jwt = webpush.vapid_jwt("https://fcm.googleapis.com/fcm/send/x")
    h, c, s = jwt.split(".")
    raw = webpush.b64u_decode(s)
    try:
        webpush.private_key().public_key().verify(
            encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
            f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))
        verified = True
    except Exception:  # noqa: BLE001
        verified = False
    check("the VAPID token verifies with the server's key (RFC 8292)", verified)


# ---------------------------------------------------------------- app shell

def test_shell(e: Env) -> None:
    section("App shell and installability files")
    for path, ctype in (("/app/", "text/html"), ("/app/manifest.webmanifest", "application/manifest+json"),
                        ("/app/sw.js", "text/javascript"), ("/app/app.css", "text/css"),
                        ("/app/js/app.js", "text/javascript"), ("/app/icons/icon-192.png", "image/png"),
                        ("/app/icons/icon-maskable-512.png", "image/png"), ("/app/offline.html", "text/html")):
        r = e.http.get(path)
        check(f"{path} is served as {ctype}", r.status_code == 200 and r.headers["content-type"].startswith(ctype))
    m = e.http.get("/app/manifest.webmanifest").json()
    check("manifest: standalone, scoped to /app/, with 192 and 512 icons and a maskable one",
          m["display"] == "standalone" and m["scope"] == "/app/" and m["start_url"].startswith("/app/")
          and {"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]}
          and any(i.get("purpose") == "maskable" for i in m["icons"]))
    check("nothing outside the app folder can be read through it",
          e.http.get("/app/../backend/app/config.py").status_code == 404
          and e.http.get("/app/%2e%2e/%2e%2e/backend/.env").status_code == 404)
    check("the web console is still served at /", e.http.get("/").status_code == 200
          and "Console" in e.http.get("/").text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--db", required=True, help="the SQLite file the backend at --base uses")
    args = ap.parse_args()
    os.environ.setdefault("AIRA_DB_PATH", args.db)
    os.environ.setdefault("AIRA_DATA_DIR", str(Path(args.db).parent))
    os.environ.setdefault("AIRA_MODE", "archive")
    e = Env(args.base, args.db)
    test_shell(e)
    test_data(e)
    test_approval(e)
    test_live(e)
    test_access()
    test_push(e)
    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for _, name, detail in failed:
        print(f"  FAILED: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
