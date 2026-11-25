"""Browser checks for the mobile app and the web console together, in headless Chrome.

Opens the app at phone size and the console at desktop size side by side against one backend, and
checks what a person would see: installability, the service worker, real numbers, paging, search,
an approval made in the app appearing in the console without a reload (and the other way round),
offline behaviour and reconnection, and layout at several screen sizes. Screenshots are written to
--out for a human to look at.

Like pwa_check.py it creates its own proposals targeting `prometheus`, which the safety layer always
refuses, so no approval here can touch a container. Point it at a scratch backend:

    backend\\.venv\\Scripts\\python.exe scripts\\pwa_ui_check.py --base http://localhost:8011 --db <its aira.db> --out <folder>
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

CHROME = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
results: list[tuple[bool, str, str]] = []
# Windows consoles default to a code page that cannot print every character a page may contain.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check(name, ok, detail=""):
    results.append((bool(ok), name, str(detail)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          {detail}" if detail and not ok else ""))
    return bool(ok)


class Page:
    """One browser tab over the DevTools protocol."""

    def __init__(self, port: int):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
        info = json.load(urllib.request.urlopen(req))
        self.ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=60)
        self.n = 0
        self.errors: list[str] = []
        for m in ("Runtime.enable", "Log.enable", "Page.enable", "Network.enable"):
            self.call(m)

    def _note(self, m):
        meth = m.get("method")
        if meth == "Runtime.exceptionThrown":
            self.errors.append(json.dumps(m["params"]["exceptionDetails"])[:300])
        elif meth == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
            e = m["params"]["entry"]
            # A failed request while deliberately offline is expected, not an app error.
            # Also not script errors: an HTTP status the test provokes on purpose (409 for the
            # losing side of a race) or the console's long-missing favicon.ico.
            if ("ERR_INTERNET_DISCONNECTED" not in e.get("text", "") and "net::ERR" not in e.get("text", "")
                    and not e.get("text", "").startswith("Failed to load resource")):
                self.errors.append(e.get("text", "")[:300])
        elif meth == "Runtime.consoleAPICalled" and m["params"]["type"] == "error":
            self.errors.append(json.dumps(m["params"]["args"])[:300])

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                return m.get("result", m.get("error"))
            self._note(m)

    def pump(self, seconds):
        end = time.time() + seconds
        self.ws.settimeout(0.2)
        while time.time() < end:
            try:
                self._note(json.loads(self.ws.recv()))
            except Exception:  # noqa: BLE001 - timeout between messages
                pass
        self.ws.settimeout(60)

    def js(self, expr, await_promise=True):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=await_promise)
        if "exceptionDetails" in r:
            return {"__error__": r["exceptionDetails"].get("text")}
        return r["result"].get("value")

    def wait(self, expr, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js(expr)
            if v and not (isinstance(v, dict) and "__error__" in v):
                return v
            self.pump(0.25)
        return None

    def front(self):
        # Headless Chrome stops rendering background tabs, and scroll observers need frames.
        self.call("Page.bringToFront")

    def go(self, url, settle=1.5):
        self.front()
        self.call("Page.navigate", url=url)
        self.pump(settle)

    def size(self, w, h, mobile=True):
        self.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=2 if mobile else 1,
                  mobile=mobile)
        self.call("Emulation.setTouchEmulationEnabled", enabled=mobile)

    def shot(self, path):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        Path(path).write_bytes(base64.b64decode(data))

    def offline(self, on):
        self.call("Network.emulateNetworkConditions", offline=on, latency=0,
                  downloadThroughput=-1, uploadThroughput=-1)


def new_action(db: str, used: set) -> tuple[int, int]:
    """A refused-by-design proposal (target prometheus) on one of the newest resolved incidents, so
    the web console - which lists the latest 100 - shows it too."""
    c = sqlite3.connect(db)
    iid = next(r[0] for r in c.execute(
        "SELECT id FROM incidents i WHERE status='resolved' AND NOT EXISTS (SELECT 1 FROM remediation_actions a "
        "WHERE a.incident_id = i.id AND a.target_service = 'prometheus') ORDER BY id DESC")
               if r[0] not in used)
    used.add(iid)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    aid = c.execute(
        "INSERT INTO remediation_actions (incident_id, attempt, proposed_at, action, target_service, params, "
        "risk, reasoning, expected_effect, reversal, status, updated_at) VALUES (?, 9, ?, 'restart_container', "
        "'prometheus', '{}', 'medium', 'pwa_ui_check proposal: the safety layer always refuses prometheus', "
        "'none - test', 'nothing to reverse', 'proposed', ?)", (iid, now, now)).lastrowid
    c.commit(); c.close()
    return aid, iid


def action_row(db, aid):
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    r = dict(c.execute("SELECT * FROM remediation_actions WHERE id=?", (aid,)).fetchone()); c.close()
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8011")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    base = a.base.rstrip("/")
    api = lambda p: json.load(urllib.request.urlopen(base + p))  # noqa: E731
    used: set = set()

    chrome = next(p for p in CHROME if os.path.exists(p))
    port = 9340
    profile = tempfile.mkdtemp(prefix="aira-ui-")
    proc = subprocess.Popen([chrome, "--headless=new", f"--remote-debugging-port={port}", "--no-first-run",
                             f"--user-data-dir={profile}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version"); break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

        app, con = Page(port), Page(port)
        app.size(390, 844); con.size(1440, 900, mobile=False)

        print("\nInstallable app")
        app.go(base + "/app/", 4)
        check("app loads its dashboard", app.wait("document.querySelectorAll('.stat').length >= 6", 15))
        check("Chrome reports the app installable (no installability errors)",
              app.call("Page.getInstallabilityErrors").get("installabilityErrors") == [],
              app.call("Page.getInstallabilityErrors"))
        check("manifest parses without errors", not (app.call("Page.getAppManifest") or {}).get("errors"))
        check("service worker registered for /app/",
              app.wait("navigator.serviceWorker.getRegistration('/app/').then(r => !!(r && r.active))", 15))
        app.go(base + "/app/", 3)
        check("after a reload the service worker controls the app",
              app.wait("!!navigator.serviceWorker.controller", 10))
        check("the live connection is up", app.wait("document.querySelector('#conn').classList.contains('live')", 10))

        print("\nReal numbers")
        s = api("/api/summary")
        # The dashboard repaints live, so read it once it has settled, and compare with the summary
        # fetched at the same moment (a few tries, in case a change lands in between).
        same, tv = False, {}
        for _ in range(6):
            app.wait("document.querySelectorAll('.stat .v').length >= 6", 10)
            tv = dict(app.js("[...document.querySelectorAll('.stat')].map(t => [t.querySelector('.k').textContent, t.querySelector('.v').textContent])") or [])
            s = api("/api/summary")
            same = (tv.get("Active incidents") == str(s["firing"]) and tv.get("Pending approvals") == str(s["pending_approvals"])
                    and tv.get("Total incidents") == str(s["total"]) and tv.get("Resolved") == str(s["resolved"]))
            if same:
                break
            app.pump(1)
        check("dashboard tiles equal /api/summary", same, f"page {tv} vs api {s}")
        h = api("/health")
        check("system status lists every /health check",
              app.js("document.querySelectorAll('.health .hrow').length") == len(h["checks"]))
        overlap = app.js("""[...document.querySelectorAll('.health .hrow')].filter(r => {
            const n = r.querySelector('b').getBoundingClientRect(), d = r.querySelector('.hd').getBoundingClientRect(),
                  dot = r.querySelector('.dot').getBoundingClientRect();
            const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
            return hit(n, d) || hit(dot, n) || hit(dot, d) || n.width < 40;
          }).map(r => r.querySelector('b').textContent)""")
        check("health rows: dot, name and detail never overlap", overlap == [], overlap)
        app.shot(out / "phone-dashboard.png")

        print("\nIncidents")
        app.go(base + "/app/#/incidents", 2)
        total = api("/api/incidents?limit=1")["total"]
        check("first page shows 30 and the true total",
              app.wait(f"document.querySelector('#count').textContent === '30 of {total} incidents'", 10),
              app.js("document.querySelector('#count').textContent"))
        app.front(); app.pump(1)
        app.js("window.scrollTo(0, document.body.scrollHeight - innerHeight * 2)"); app.pump(0.5)
        app.js("window.scrollTo(0, document.body.scrollHeight)"); app.pump(2)
        check("scrolling to the end loads the next page", app.wait("document.querySelectorAll('#items .row').length >= 60", 10),
              app.js("document.querySelectorAll('#items .row').length"))
        app.shot(out / "phone-incidents.png")
        some = api("/api/incidents?limit=1&offset=40&sort=latest")["items"][0]["id"]
        app.js(f"{{ const q=document.querySelector('#q'); q.value='#{some}'; q.dispatchEvent(new Event('input')); }}")
        check("search by #id narrows to that incident",
              app.wait(f"[...document.querySelectorAll('#items .row')].some(r => r.getAttribute('href') === '#/incident/{some}')", 10))
        app.js("{ const q=document.querySelector('#q'); q.value=''; q.dispatchEvent(new Event('input')); }")
        app.wait("!/^1 of 1 /.test(document.querySelector('#count').textContent)", 5)
        app.js("[...document.querySelectorAll('#c-state .chip')].find(b => b.dataset.v === 'resolved').click()")
        n_res = api("/api/incidents?state=resolved&limit=1")["total"]
        check("Resolved filter shows the backend's count",
              app.wait(f"document.querySelector('#count').textContent.endsWith('of {n_res} incidents')", 10))
        app.js("[...document.querySelectorAll('#c-state .chip')].find(b => b.dataset.v === '').click()"); app.pump(1)

        print("\nIncident detail")
        rich = api("/api/incidents?limit=50&sort=latest")["items"]
        rich_id = next((i["id"] for i in rich if i.get("last_action") and i["last_action"]["status"] == "executed"), rich[0]["id"])
        app.go(base + f"/app/#/incident/{rich_id}", 2.5)
        inc = api(f"/incidents/{rich_id}?include_events=false")
        check("detail header shows the backend's service and alert",
              app.wait(f"document.querySelector('.hero h2') && document.querySelector('.hero h2').textContent === {json.dumps(inc['service'])}", 10)
              and app.js(f"document.querySelector('.hero .alert').textContent === {json.dumps(inc['alertname'])}"))
        check("diagnosis matches the stored conclusion",
              app.js("document.querySelector('.conc .t') && document.querySelector('.conc .t').textContent") ==
              next((f["output"]["statement"] for f in sorted(inc["findings"], key=lambda f: -f["round"]) if f["agent"] == "conclusion" and f.get("output")), None)
              or not any(f["agent"] == "conclusion" for f in inc["findings"]))
        check("remediation progress shows the recorded lifecycle",
              app.js("document.querySelectorAll('.steps li').length") >= 3)
        app.js("document.querySelector('details[data-sec=postmortem]').open = true")
        check("postmortem section loads the report", app.wait("!!document.querySelector('details[data-sec=postmortem] .md')", 15))
        app.js("document.querySelector('details[data-sec=timeline]').open = true")
        check("execution trace loads the recorded events", app.wait("document.querySelectorAll('details[data-sec=timeline] .tl li').length > 0", 15))
        app.js("document.querySelector('details[data-sec=investigation]').open = true"); app.pump(0.5)
        check("investigation lists the agents' findings", app.js("document.querySelectorAll('details[data-sec=investigation] .agent').length") >= 3)
        app.shot(out / "phone-detail.png")

        print("\nApproval from the app shows in the console")
        aid, iid = new_action(a.db, used)
        con.go(base + "/", 3)
        con.wait(f"!!document.querySelector('.row[data-id=\"{iid}\"]')", 15)
        con.js(f"document.querySelector('.row[data-id=\"{iid}\"]').click()"); con.pump(1)
        check("console shows the proposal awaiting approval", con.wait(f"!!document.querySelector('[data-approve=\"{aid}\"]')", 10))
        app.go(base + "/app/#/approvals", 2)
        check("app lists the same proposal", app.wait(f"!!document.querySelector('[data-decide=approve][data-id=\"{aid}\"]')", 10))
        app.shot(out / "phone-approvals.png")
        app.js(f"document.querySelector('[data-decide=approve][data-id=\"{aid}\"]').click()")
        check("Approve opens a confirmation naming the exact action",
              app.wait("!document.querySelector('#sheet').hidden && document.querySelector('#sheet .exact').textContent.includes('prometheus')", 5))
        app.shot(out / "phone-confirm.png")
        check("nothing is decided before confirming", action_row(a.db, aid)["status"] == "proposed")
        t0 = time.time()
        app.js("document.querySelector('#sheet [data-value=approve]').click()")
        app.wait(f"!document.querySelector('[data-decide][data-id=\"{aid}\"]')", 15)
        r = action_row(a.db, aid)
        check("the decision is stored as developer (app), from the app",
              r["decided_by"] == "developer (app)" and r["decision_source"] == "app", r["decided_by"])
        seen = con.wait("[...document.querySelectorAll('.act-rev')].some(e => e.textContent.includes('developer (app)'))", 15)
        dt = time.time() - t0
        check("the console shows 'Approved by developer (app)' without a reload", seen)
        check(f"...within a few seconds ({dt:.1f}s)", seen and dt < 8)
        check("the console's approve button is gone", not con.js(f"!!document.querySelector('[data-approve=\"{aid}\"]')"))
        con.shot(out / "console-after-app-approval.png")

        print("\nApproval from the console shows in the app")
        aid2, iid2 = new_action(a.db, used)
        app.go(base + f"/app/#/incident/{iid2}", 2.5)
        check("app shows the proposal on the incident", app.wait(f"!!document.querySelector('[data-decide=approve][data-id=\"{aid2}\"]')", 10))
        con.go(base + "/", 3)
        con.wait(f"!!document.querySelector('.row[data-id=\"{iid2}\"]')", 15)
        con.js(f"document.querySelector('.row[data-id=\"{iid2}\"]').click()"); con.pump(1)
        con.wait(f"!!document.querySelector('[data-approve=\"{aid2}\"]')", 10)
        t0 = time.time()
        con.js(f"document.querySelector('[data-approve=\"{aid2}\"]').click()")
        seen = app.wait("[...document.querySelectorAll('.steps b')].some(e => e.textContent.includes('Approved by developer (console)'))", 15)
        dt = time.time() - t0
        for _ in range(40):
            if action_row(a.db, aid2)["status"] != "proposed":
                break
            time.sleep(0.25)
        app.wait(f"!document.querySelector('[data-decide][data-id=\"{aid2}\"]')", 10)
        r = action_row(a.db, aid2)
        check("stored as developer (console)", r["decided_by"] == "developer (console)" and r["decision_source"] == "console")
        check("the app shows 'Approved by developer (console)' without a reload", seen)
        check(f"...within a few seconds ({dt:.1f}s)", seen and dt < 8)
        check("the app shows the safety layer's refusal as the outcome",
              app.wait("[...document.querySelectorAll('.steps li.bad b')].some(e => e.textContent.includes('Refused'))", 10))
        check("the app's decision buttons for it are gone", not app.js(f"!!document.querySelector('[data-decide][data-id=\"{aid2}\"]')"))
        app.shot(out / "phone-progress.png")

        print("\nBoth press at once")
        aid3, iid3 = new_action(a.db, used)
        app.go(base + f"/app/#/incident/{iid3}", 2.5)
        app.wait(f"!!document.querySelector('[data-decide=approve][data-id=\"{aid3}\"]')", 10)
        con.go(base + "/", 3); con.wait(f"!!document.querySelector('.row[data-id=\"{iid3}\"]')", 15)
        con.js(f"document.querySelector('.row[data-id=\"{iid3}\"]').click()"); con.pump(1)
        con.wait(f"!!document.querySelector('[data-approve=\"{aid3}\"]')", 10)
        app.js(f"document.querySelector('[data-decide=approve][data-id=\"{aid3}\"]').click()")
        app.wait("!document.querySelector('#sheet').hidden", 5)
        app.js("document.querySelector('#sheet [data-value=approve]').click()")      # both fire together
        con.js(f"document.querySelector('[data-approve=\"{aid3}\"]').click()")
        app.pump(4); con.pump(1)
        c = sqlite3.connect(a.db)
        since = action_row(a.db, aid3)["proposed_at"]
        approved = c.execute("SELECT COUNT(*) FROM agent_events WHERE incident_id=? AND kind='action_approved' AND at >= ?", (iid3, since)).fetchone()[0]
        refused = c.execute("SELECT COUNT(*) FROM agent_events WHERE incident_id=? AND kind='action_refused' AND at >= ?", (iid3, since)).fetchone()[0]
        c.close()
        r = action_row(a.db, aid3)
        check("one decision recorded, executed once", approved == 1 and refused == 1, f"approved={approved} refused={refused} by {r['decided_by']}")
        both = (app.wait("document.body.innerText.includes('Approved by " + r["decided_by"] + "')", 10),
                con.wait("document.body.innerText.includes('Approved by')", 10) or con.wait("document.body.innerText.includes('already')", 3))
        check(f"both screens show the winner ({r['decided_by']})", all(both))

        print("\nOffline and reconnection")
        aid4, iid4 = new_action(a.db, used)
        app.go(base + "/app/#/approvals", 3)
        app.wait(f"!!document.querySelector('[data-decide=approve][data-id=\"{aid4}\"]')", 10)
        app.offline(True); app.js("window.dispatchEvent(new Event('offline'))"); app.pump(2)
        # The banner says which it is: "Your phone is offline." or "AIRA can't be reached right now."
        check("offline is shown clearly", app.wait("!document.querySelector('#banner').hidden && /offline|can't be reached/i.test(document.querySelector('#banner').textContent)", 8),
              app.js("document.querySelector('#banner').textContent"))
        check("decision buttons are disabled offline", app.js(f"document.querySelector('[data-decide=approve][data-id=\"{aid4}\"]').disabled") is True)
        app.go(base + "/app/#/incidents", 3)
        check("offline, the app still opens (service worker) with the last data, marked stale",
              app.wait("document.querySelectorAll('#items .row').length > 0", 8)
              and "may be out of date" in (app.js("document.querySelector('#banner').textContent") or ""),
              app.js("document.querySelector('#banner').textContent"))
        app.shot(out / "phone-offline.png")
        check("nothing was decided while offline", action_row(a.db, aid4)["status"] == "proposed")
        app.offline(False); app.js("window.dispatchEvent(new Event('online'))")
        check("back online: live again and the banner clears",
              app.wait("document.querySelector('#conn').classList.contains('live') && document.querySelector('#banner').hidden", 15))
        # A change made while it was away must show after reconnecting.
        app.go(base + "/app/#/approvals", 2)
        check("after reconnecting the app reads the authoritative state", app.wait(f"!!document.querySelector('[data-id=\"{aid4}\"]')", 10))
        urllib.request.urlopen(urllib.request.Request(base + f"/actions/{aid4}/reject", method="POST",
                               data=json.dumps({"source": "console"}).encode(), headers={"Content-Type": "application/json"}))
        check("a rejection made elsewhere removes it from the app's list", app.wait(f"!document.querySelector('[data-id=\"{aid4}\"]')", 10))

        print("\nLayouts")
        for w, hgt, mob, name in ((360, 780, True, "small-phone"), (412, 915, True, "phone"),
                                  (768, 1024, True, "tablet"), (1280, 800, False, "desktop")):
            app.size(w, hgt, mob)
            for route in ("#/", "#/incidents", f"#/incident/{rich_id}", "#/activity", "#/settings"):
                app.go(base + "/app/" + route, 2)
                over = app.js("document.documentElement.scrollWidth - window.innerWidth")
                check(f"{name} {w}px {route}: no sideways overflow", over is not None and over <= 1, over)
            app.go(base + "/app/#/", 2); app.shot(out / f"{name}-dashboard.png")
            app.go(base + f"/app/#/incident/{rich_id}", 2); app.shot(out / f"{name}-detail.png")
        tap = app.js("Math.min(...[...document.querySelectorAll('.tabbar a')].map(a => a.getBoundingClientRect().height))")
        app.size(390, 844)
        app.go(base + "/app/#/", 2)
        tap = app.js("Math.min(...[...document.querySelectorAll('.tabbar a')].map(a => a.getBoundingClientRect().height))")
        check("tab bar targets are at least 48px tall", tap and tap >= 48, tap)

        print("\nNotifications")
        # Allow notifications for the app's origin, as a person tapping "Allow" would.
        bws = websocket.create_connection(json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version"))["webSocketDebuggerUrl"], timeout=30)
        bws.send(json.dumps({"id": 1, "method": "Browser.grantPermissions",
                             "params": {"origin": base, "permissions": ["notifications"]}}))
        while json.loads(bws.recv()).get("id") != 1:
            pass
        # Kept open until the end: Chrome drops a permission override when the session that set it
        # disconnects, and the app would then (correctly) report notifications as blocked.
        app.size(390, 844)
        app.go(base + "/app/#/settings", 3)
        check("notification permission is granted for the test", app.js("Notification.permission") == "granted",
              app.js("Notification.permission"))
        check("settings offers to turn notifications on", app.wait("!!document.querySelector('#push-on')", 10))
        app.js("document.querySelector('#push-on').click()")
        # Headless Chrome has no push service, exactly like a phone whose push service refuses the
        # registration: the app must still turn alerts on and explain the rest.
        state = app.wait("""(() => { const t = document.querySelector('.card .note') && document.body.innerText;
            return t && (t.includes('On while AIRA is open') || t.includes('including when AIRA is closed')) ? t : null; })()""", 20)
        check("turning them on succeeds even when the push service refuses", bool(state),
              app.js("document.body.innerText.slice(0, 400)"))
        # Then the background-push attempt finishes (registered, refused, or 15 s timeout): either
        # alerts are on even when closed, or the app says why not, how to fix it, and offers a retry.
        final = app.wait("""(() => { const t = document.body.innerText;
            return t.includes('Setting up alerts') ? null
                 : t.includes('including when AIRA is closed') ? 'background'
                 : document.querySelector('.pushhelp') ? 'foreground' : null; })()""", 25)
        check("background alerts end registered, or explained with the fix and a retry",
              final == "background" or (final == "foreground" and app.js("!!document.querySelector('#push-retry')")),
              final)
        app.shot(out / "phone-settings-notifications.png")
        app.js("document.querySelector('#push-test').click()")
        check("'Send a test' shows a real system notification",
              app.wait("navigator.serviceWorker.getRegistration('/app/').then(r => r.getNotifications())"
                       ".then(ns => ns.some(n => n.title === 'AIRA test notification'))", 10))
        app.go(base + "/app/#/", 3)
        app.wait("document.querySelector('#conn').classList.contains('live')", 10)
        c = sqlite3.connect(a.db)
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        new_iid = c.execute(
            "INSERT INTO incidents (fingerprint, starts_at, status, alertname, service, severity, summary, "
            "description, labels, annotations, first_received_at, updated_at) VALUES (?, ?, 'firing', "
            "'PwaUiAlert', 'pwa-ui-svc', 'critical', 'pwa_ui_check incident', 'test', '{}', '{}', ?, ?)",
            (os.urandom(8).hex(), now, now, now)).lastrowid
        c.commit(); c.close()
        got = app.wait("navigator.serviceWorker.getRegistration('/app/').then(r => r.getNotifications())"
                       ".then(ns => ns.filter(n => n.title === 'Critical incident').map(n => n.body))", 12)
        check("a new incident raises a system notification on the phone",
              bool(got) and any("pwa-ui-svc" in b for b in got), got)
        check("...and the dashboard counts it as active without a reload",
              app.wait("[...document.querySelectorAll('.stat')].some(t => t.querySelector('.k').textContent === 'Active incidents' && t.querySelector('.v').textContent !== '0')", 10))
        c = sqlite3.connect(a.db)
        c.execute("UPDATE incidents SET status='resolved', ends_at=?, updated_at=? WHERE id=?", (now, now, new_iid))
        c.commit(); c.close()
        app.shot(out / "phone-dashboard-final.png")

        print("\nThe web console")
        con.go(base + "/", 3)
        check("console still renders its incident list", con.wait("document.querySelectorAll('#list .row').length > 0", 10))
        check("console has no script errors", not con.errors, con.errors[:3])
        check("app has no script errors", not app.errors, app.errors[:3])
    finally:
        proc.kill()
        # The browser profile is a few hundred MB; never leave it behind.
        time.sleep(1)
        import shutil  # noqa: PLC0415
        shutil.rmtree(profile, ignore_errors=True)

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed (screenshots in {out})")
    for _, name, d in failed:
        print(f"  FAILED: {name} {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
