/* The app's screens other than the incident detail: Dashboard, Incidents, Approvals, Activity,
 * Settings. Every number and row comes from the backend; loading, empty, offline and error states
 * are shown as such. */

import { getJSON, postJSON, deleteJSON, HttpError, clearCache } from "./api.js";
import { esc, ago, clock, statusPill, sevPill, outcomePill, actionPill, loading, empty, failed,
         toast, coalesce } from "./ui.js";
import { approvalCard, wireDecisions } from "./detail.js";
import * as notify from "./notify.js";

function staleOf(results) {
  const stale = results.filter(r => r && r.stale).map(r => r.at);
  return stale.length ? Math.min(...stale) : null;
}

function errorState(e, what, app) {
  if (e instanceof HttpError && e.status === 401) { app.needSignIn(); return failed("Sign-in needed", "This backend needs its access token. Open Settings."); }
  return failed(`Could not load ${what}`, e.message || String(e));
}

export function incidentRow(i) {
  const waiting = i.pending_approvals > 0;
  const dot = waiting ? "approval" : i.status;
  return `<a class="row" href="#/incident/${i.id}">
    <span class="dot ${dot}"></span>
    <div class="main">
      <div class="t1"><span class="svc">${esc(i.service || "unknown")}</span><span class="faint" style="font-weight:500;font-size:12px">#${i.id}</span></div>
      <div class="t2">${esc(i.alertname)}</div>
      <div class="t3">${i.status === "firing" ? statusPill(i) : ""}${sevPill(i.severity)}
        ${waiting ? `<span class="pill amber">approval needed</span>` : ""}
        ${i.investigating ? `<span class="pill blue">investigating</span>` : ""}
        ${i.conclusion ? outcomePill(i.conclusion.outcome) : ""}
        ${i.last_action && i.last_action.status !== "proposed" && i.last_action.action !== "none" ? actionPill(i.last_action) : ""}</div>
    </div>
    <div class="side"><span style="color:${i.status === "firing" ? "var(--red)" : "var(--green)"}">${i.status === "firing" ? "active" : "resolved"}</span><br>${ago(i.first_received_at || i.starts_at)}</div>
  </a>`;
}

/* One row per /health dependency: status dot, name, and what the backend said about it. */
function healthRows(checks) {
  return `<div class="list health">${checks.map(([k, v]) => `<div class="hrow">
      <span class="dot ${v.ok ? "good" : "bad"}" aria-label="${v.ok ? "healthy" : "unhealthy"}"></span>
      <div class="hx"><b>${esc(k)}</b><span class="hd">${esc(v.detail || (v.ok ? "ok" : "unreachable"))}</span></div>
    </div>`).join("")}</div>`;
}

function activityRow(a, unread) {
  return `<a class="row act-row${unread ? " unread" : ""}" href="#/incident/${a.incident_id}">
    <span class="dot ${esc(a.level === "approval" ? "approval" : a.level === "critical" ? "critical" : a.level)}"></span>
    <div class="main"><div class="t1">${esc(a.title)}</div><div class="t2" style="white-space:normal">${esc(a.body)}</div></div>
    <div class="side">${ago(a.at)}</div></a>`;
}

// ---------------------------------------------------------------- dashboard

export const dashboardView = {
  title: "Overview",
  async render(ctx) {
    this.ctx = ctx;
    if (!this.data) ctx.el.innerHTML = loading(8);
    else this.paint();
    await this.load();
  },
  async load() {
    const app = this.ctx.app;
    // /health probes every dependency in turn and can take seconds (longer when something is
    // down), so it is not allowed to hold up the rest: the numbers, approvals and recent incidents
    // are shown as soon as they arrive, and system status fills in when its answer does.
    const health = getJSON("/health", "health");
    const [s, ap, rec, act] = await Promise.allSettled([
      getJSON("/api/summary", "summary"),
      getJSON("/api/approvals", "approvals"),
      getJSON("/api/incidents?limit=5&sort=active", "recent"),
      getJSON("/api/activity?limit=6", "activity6"),
    ]);
    if (s.status === "rejected") { this.ctx.el.innerHTML = errorState(s.reason, "the dashboard", app); return; }
    const val = r => r.status === "fulfilled" ? r.value : null;
    const prevHealth = this.data && this.data.health;
    this.data = { summary: val(s).data, approvals: val(ap)?.data, recent: val(rec)?.data,
                  activity: val(act)?.data, health: prevHealth, healthPending: !prevHealth };
    app.setStale(staleOf([val(s), val(ap), val(rec), val(act)]));
    app.setSummary(this.data.summary);
    this.paint();
    const data = this.data;
    try {
      const h = await health;
      if (this.data === data) { data.health = h.data; data.healthPending = false; this.paint(); }
    } catch {
      if (this.data === data) { data.healthPending = false; this.paint(); }
    }
  },
  paint() {
    const d = this.data; if (!d) return;
    const s = d.summary;
    const ap = d.approvals || [];
    const checks = Object.entries((d.health && d.health.checks) || {});
    const bad = checks.filter(([, v]) => !v.ok);
    const stat = (k, v, sub, cls, href) => `<a class="stat ${cls || ""}" href="${href}"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub || "&nbsp;"}</div></a>`;
    this.ctx.el.innerHTML = `
      ${s.pending_approvals ? `<a class="callout sec" href="#/approvals"><div><b>${s.pending_approvals} remediation${s.pending_approvals > 1 ? "s" : ""}</b> waiting for your decision</div><span class="go">Review ›</span></a>` : ""}
      <section class="sec"><h3 class="sh">Now <span class="right faint">as of ${clock(s.as_of)}</span></h3>
        <div class="stats">
          ${stat("Active incidents", s.firing, s.critical_firing ? `${s.critical_firing} critical` : "firing now", s.firing ? "hot" : "", "#/incidents?state=firing")}
          ${stat("Pending approvals", s.pending_approvals, "need a human", s.pending_approvals ? "wait" : "", "#/approvals")}
          ${stat("Under investigation", s.investigating, "agents working", s.investigating ? "busy" : "", "#/incidents?state=investigating")}
          ${stat("Remediations running", s.executing + s.verifying, `${s.executing} executing · ${s.verifying} verifying`, (s.executing + s.verifying) ? "busy" : "", "#/activity")}
          ${stat("Resolved", s.resolved, `${s.recovered} by remediation`, "", "#/incidents?state=resolved")}
          ${stat("Total incidents", s.total, "all time", "", "#/incidents")}
        </div></section>
      <section class="sec"><h3 class="sh">System status <span class="right ${bad.length ? "" : "faint"}" style="${bad.length ? "color:var(--red)" : ""}">${
          !checks.length ? (d.healthPending ? "checking…" : "unavailable") : bad.length ? `${bad.length} of ${checks.length} need attention` : `all ${checks.length} healthy`}</span></h3>
        ${checks.length ? healthRows(checks)
          : d.healthPending ? `<div class="list pad">${loading(3)}</div>`
          : empty("System status unavailable", "The backend's /health could not be read.")}</section>
      ${ap.length ? `<section class="sec"><h3 class="sh">Needs your decision <a href="#/approvals">All ›</a></h3>
        ${ap.slice(0, 2).map(a => approvalCard(a, { id: a.incident_id, status: a.incident_status, service: a.service, alertname: a.alertname }, { compact: true })).join("")}</section>` : ""}
      <section class="sec"><h3 class="sh">Recent incidents <a href="#/incidents">All ›</a></h3>
        ${d.recent && d.recent.items.length ? `<div class="list">${d.recent.items.map(incidentRow).join("")}</div>` : empty("No incidents yet", "Inject a fault and an alert arrives within about 90 seconds.")}</section>
      <section class="sec"><h3 class="sh">Recent activity <a href="#/activity">All ›</a></h3>
        ${d.activity && d.activity.length ? `<div class="list">${d.activity.map(a => activityRow(a, false)).join("")}</div>` : empty("No activity yet")}</section>`;
    wireDecisions(this.ctx.el, id => {
      const a = (this.data.approvals || []).find(x => x.id === id);
      return a ? { action: a, incident: { id: a.incident_id, status: a.incident_status, service: a.service, alertname: a.alertname } } : null;
    }, this.ctx.app);
  },
  onChange(msg) {
    if (msg.summary && this.data) { this.data.summary = msg.summary; this.paint(); }
    if (!msg.hello) (this._soon ||= coalesce(() => this.load(), 1500))();
  },
  onResync() { this.load(); },
};

// ---------------------------------------------------------------- incidents

const PAGE = 30;
const STATE_CHIPS = [["", "All"], ["firing", "Firing"], ["pending_approval", "Needs approval"],
                     ["investigating", "Investigating"], ["resolved", "Resolved"]];
const SEV_CHIPS = [["", "Any severity"], ["critical", "Critical"], ["warning", "Warning"]];
const TIME_CHIPS = [["", "Any time"], ["24h", "Last 24h"], ["7d", "Last 7 days"]];

export const incidentsView = {
  title: "Incidents",
  f: { q: "", state: "", severity: "", time: "", sort: "active" },
  items: [], total: 0, busy: false,
  async render(ctx) {
    this.ctx = ctx;
    if (ctx.query.state !== undefined) this.f.state = ctx.query.state;
    ctx.el.innerHTML = `
      <div class="search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
        <input id="q" type="search" inputmode="search" placeholder="Search #id, service, alert…" value="${esc(this.f.q)}" aria-label="Search incidents"></div>
      <div class="chips" id="c-state" role="group" aria-label="Status"></div>
      <div class="chips" id="c-sev" role="group" aria-label="Severity" style="padding-top:6px"></div>
      <div class="toolbar"><span id="count">…</span>
        <select id="sort" aria-label="Sort">
          <option value="active">Active first</option><option value="latest">Newest</option>
          <option value="oldest">Oldest</option><option value="severity">Severity</option></select></div>
      <div id="items">${loading(6)}</div>
      <div id="sentinel" style="height:1px"></div>`;
    const q = ctx.el.querySelector("#q");
    let t = null;
    q.oninput = () => { clearTimeout(t); t = setTimeout(() => { this.f.q = q.value.trim(); this.reload(); }, 300); };
    const sort = ctx.el.querySelector("#sort");
    sort.value = this.f.sort;
    sort.onchange = () => { this.f.sort = sort.value; this.reload(); };
    this.paintChips();
    if (this.io) this.io.disconnect();
    this.io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) this.more(); }, { rootMargin: "400px" });
    this.io.observe(ctx.el.querySelector("#sentinel"));
    await this.reload();
  },
  paintChips() {
    const el = this.ctx.el;
    const chips = (host, list, key) => {
      host.innerHTML = list.map(([v, l]) => `<button class="chip${this.f[key] === v ? " on" : ""}" data-v="${v}" type="button">${l}</button>`).join("");
      host.querySelectorAll(".chip").forEach(b => b.onclick = () => { this.f[key] = b.dataset.v; this.paintChips(); this.reload(); });
    };
    chips(el.querySelector("#c-state"), STATE_CHIPS, "state");
    chips(el.querySelector("#c-sev"), SEV_CHIPS, "severity");
    // The time filter shares the second row, as toggles after the severity chips.
    const sev = el.querySelector("#c-sev");
    sev.insertAdjacentHTML("beforeend", TIME_CHIPS.slice(1).map(([v, l]) =>
      `<button class="chip${this.f.time === v ? " on" : ""}" data-t="${v}" type="button">${l}</button>`).join(""));
    sev.querySelectorAll("[data-t]").forEach(b => b.onclick = () => { this.f.time = this.f.time === b.dataset.t ? "" : b.dataset.t; this.paintChips(); this.reload(); });
  },
  url(offset) {
    const p = new URLSearchParams({ limit: PAGE, offset, sort: this.f.sort });
    if (this.f.state) p.set("state", this.f.state);
    if (this.f.severity) p.set("severity", this.f.severity);
    if (this.f.q) p.set("q", this.f.q);
    if (this.f.time) {
      const ms = this.f.time === "24h" ? 864e5 : 7 * 864e5;
      p.set("since", new Date(Date.now() - ms).toISOString().slice(0, 19) + "+00:00");
    }
    return `/api/incidents?${p}`;
  },
  cacheKey(offset) { return offset === 0 && !this.f.q && !this.f.state && !this.f.severity && !this.f.time && this.f.sort === "active" ? "incidents:first" : null; },
  async reload() {
    const ticket = (this.ticket = (this.ticket || 0) + 1);
    this.busy = true;
    try {
      const res = await getJSON(this.url(0), this.cacheKey(0));
      if (ticket !== this.ticket) return;
      this.items = res.data.items; this.total = res.data.total;
      this.ctx.app.setStale(res.stale ? res.at : null);
      this.paintList();
    } catch (e) {
      if (ticket === this.ticket) this.ctx.el.querySelector("#items").innerHTML = errorState(e, "incidents", this.ctx.app);
    } finally { this.busy = false; }
  },
  async more() {
    if (this.busy || this.items.length >= this.total || !this.items.length) return;
    this.busy = true;
    const ticket = this.ticket;
    try {
      const res = await getJSON(this.url(this.items.length));
      if (ticket !== this.ticket) return;
      const seen = new Set(this.items.map(i => i.id));
      this.items.push(...res.data.items.filter(i => !seen.has(i.id)));
      this.total = res.data.total;
      this.paintList();
    } catch (e) { toast(`Could not load more: ${e.message}`, true); }
    finally { this.busy = false; }
  },
  paintList() {
    const el = this.ctx.el;
    el.querySelector("#count").textContent = `${this.items.length} of ${this.total} incident${this.total === 1 ? "" : "s"}`;
    el.querySelector("#items").innerHTML = this.items.length
      ? `<div class="list">${this.items.map(incidentRow).join("")}</div>${this.items.length < this.total
          ? `<button class="more" id="more" type="button">Load more (${this.total - this.items.length} left)</button>` : ""}`
      : empty("No incidents match", this.f.q || this.f.state || this.f.severity || this.f.time ? "Try clearing a filter." : "Nothing has fired yet.");
    const m = el.querySelector("#more");
    if (m) m.onclick = () => this.more();
  },
  async onChange(msg) {
    if (!msg.incidents || !msg.incidents.length || !this.items.length && !msg.incidents.length) return;
    const known = new Map(this.items.map((i, n) => [i.id, n]));
    const maxId = Math.max(0, ...this.items.map(i => i.id));
    if (msg.incidents.some(id => !known.has(id) && id > maxId)) return this.reload();   // a new incident
    for (const id of msg.incidents.filter(i => known.has(i))) {
      try { const { data } = await getJSON(`/api/incidents/${id}/row`); this.items[known.get(id)] = data; } catch {}
    }
    this.paintList();
  },
  onResync() { this.reload(); },
};

// ---------------------------------------------------------------- approvals

export const approvalsView = {
  title: "Approvals",
  async render(ctx) {
    this.ctx = ctx;
    if (!this.items) ctx.el.innerHTML = loading(6); else this.paint();
    await this.load();
  },
  async load() {
    try {
      const res = await getJSON("/api/approvals", "approvals");
      this.items = res.data;
      this.ctx.app.setStale(res.stale ? res.at : null);
      this.paint();
    } catch (e) { this.ctx.el.innerHTML = errorState(e, "pending approvals", this.ctx.app); }
  },
  paint() {
    const items = this.items || [];
    if (!items.length) {
      this.ctx.el.innerHTML = `<section class="sec">${empty("Nothing waiting for a decision", "When AIRA proposes a remediation it appears here, and on the web console, until someone decides.")}</section>`;
      return;
    }
    this.ctx.el.innerHTML = `<p class="note" style="margin-top:2px">${items.length} remediation${items.length > 1 ? "s" : ""} waiting. The first decision from any device or the web console is the one that counts.</p>` +
      items.map(a => {
        const inc = { id: a.incident_id, status: a.incident_status, service: a.service, alertname: a.alertname };
        const c = a.conclusion, r = a.rca, cr = a.critic;
        return `<section class="sec card pad">
          <div class="t3" style="display:flex;gap:6px;flex-wrap:wrap">${statusPill(inc)}${sevPill(a.severity)}<span class="faint" style="font-size:12px">#${a.incident_id} · proposed ${ago(a.proposed_at)}</span></div>
          <h3 style="margin:8px 0 0;font-size:17px">${esc(a.service || "unknown")} <span class="mono faint" style="font-size:13px;font-weight:400">${esc(a.alertname)}</span></h3>
          <div class="conc ${esc(c ? c.outcome : "")}" style="margin-top:12px">
            <div class="k">${c ? (c.outcome === "accepted" ? "Diagnosis · accepted by the Critic" : c.outcome === "insufficient" ? "Diagnosis · not confirmed" : "Diagnosis") : "Diagnosis"}</div>
            <div class="t" style="font-size:14.5px">${esc((c && c.statement) || (r && r.summary) || "No diagnosis was recorded.")}</div>
            ${r && r.confidence ? `<div class="small faint" style="margin-top:4px">Root-cause confidence: ${esc(r.confidence)}${cr ? ` · Critic: ${esc(cr.verdict)}` : ""}</div>` : ""}
          </div>
          <div style="margin-top:12px">${approvalCard(a, inc)}</div>
          <a class="more" style="display:grid;place-items:center" href="#/incident/${a.incident_id}">Open the full incident</a>
        </section>`;
      }).join("");
    wireDecisions(this.ctx.el, id => {
      const a = (this.items || []).find(x => x.id === id);
      return a ? { action: a, incident: { id: a.incident_id, status: a.incident_status, service: a.service, alertname: a.alertname } } : null;
    }, this.ctx.app);
  },
  onChange(msg) { if (msg.incidents && msg.incidents.length) (this._soon ||= coalesce(() => this.load(), 1000))(); },
  onResync() { this.load(); },
};

// ---------------------------------------------------------------- activity

const SEEN_KEY = "aira-activity-seen";
export const activitySeen = () => Number(localStorage.getItem(SEEN_KEY) || 0);
const ACT_CHIPS = [["", "All"], ["approval", "Approvals"], ["results", "Results"], ["incidents", "Incidents"]];
const matches = (a, f) => !f
  || (f === "approval" && a.kind.startsWith("action_"))
  || (f === "results" && /executed|refused|failed|verification|exhausted|postmortem/.test(a.kind))
  || (f === "incidents" && a.kind.startsWith("incident_"));

export const activityView = {
  title: "Activity",
  filter: "",
  async render(ctx) {
    this.ctx = ctx;
    this.seenBefore = activitySeen();
    if (!this.items) ctx.el.innerHTML = loading(8); else this.paint();
    await this.load();
  },
  async load() {
    try {
      const res = await getJSON("/api/activity?limit=50", "activity");
      this.items = res.data;
      this.ctx.app.setStale(res.stale ? res.at : null);
      this.paint();
      const newest = Math.max(0, ...this.items.map(a => Date.parse(a.at) || 0));
      if (newest > activitySeen()) localStorage.setItem(SEEN_KEY, String(newest));
      this.ctx.app.setUnread(false);
    } catch (e) { this.ctx.el.innerHTML = errorState(e, "activity", this.ctx.app); }
  },
  paint() {
    const items = (this.items || []).filter(a => matches(a, this.filter));
    this.ctx.el.innerHTML = `<div class="chips" style="padding-top:0">${ACT_CHIPS.map(([v, l]) =>
        `<button class="chip${this.filter === v ? " on" : ""}" data-v="${v}" type="button">${l}</button>`).join("")}</div>
      <section class="sec">${items.length ? `<div class="list">${items.map(a => activityRow(a, (Date.parse(a.at) || 0) > this.seenBefore)).join("")}</div>
        ${this.items.length >= 50 ? `<button class="more" id="older" type="button">Older activity</button>` : ""}`
        : empty("No activity", this.filter ? "Nothing of this kind yet." : "Incidents, approvals and results will appear here.")}</section>`;
    this.ctx.el.querySelectorAll(".chip").forEach(b => b.onclick = () => { this.filter = b.dataset.v; this.paint(); });
    const older = this.ctx.el.querySelector("#older");
    if (older) older.onclick = async () => {
      const last = this.items[this.items.length - 1];
      try {
        const { data } = await getJSON(`/api/activity?limit=50&before=${encodeURIComponent(last.at)}`);
        const keys = new Set(this.items.map(a => a.key));
        this.items.push(...data.filter(a => !keys.has(a.key)));
        this.paint();
      } catch (e) { toast(e.message, true); }
    };
  },
  onChange(msg) { if (msg.activity && msg.activity.length) (this._soon ||= coalesce(() => this.load(), 1000))(); },
  onResync() { this.load(); },
};

// ---------------------------------------------------------------- settings

function b64uToBytes(s) {
  const pad = "=".repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

export const settingsView = {
  title: "Settings",
  async render(ctx) {
    this.ctx = ctx;
    ctx.el.innerHTML = loading(6);
    // Shown without waiting for /health (it can take seconds); system health fills in after.
    const health = getJSON("/health", "health");
    this.healthPending = true;
    const cfg = await getJSON("/api/client-config").catch(() => null);
    this.cfg = cfg ? cfg.data : null;
    this.push = await this.pushState();
    this.paint();
    try { this.health = (await health).data; } catch { this.health = null; }
    this.healthPending = false;
    if (this.ctx === ctx) this.paint();
  },
  async pushState() {
    const st = {
      supported: notify.supported(),
      secure: window.isSecureContext,
      pushSupported: "PushManager" in window,
      permission: "Notification" in window ? Notification.permission : "unsupported",
      wanted: notify.wanted(),
      pushError: localStorage.getItem("aira-push-error") || "",
      sub: null,
    };
    try {
      const reg = await navigator.serviceWorker.getRegistration("/app/");
      st.sub = reg && st.pushSupported ? await reg.pushManager.getSubscription() : null;
    } catch {}
    return st;
  },
  paint() {
    if (!this.push) return;          // not loaded yet
    const app = this.ctx.app, cfg = this.cfg, p = this.push, h = this.health;
    const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
    const checks = Object.entries((h && h.checks) || {});
    this.ctx.el.innerHTML = `
      <section class="sec"><h3 class="sh">Connection</h3><div class="card pad">
        <dl class="kv" style="margin:0">
          <dt>Server</dt><dd class="mono">${esc(location.origin)}</dd>
          <dt>Live updates</dt><dd>${esc(app.liveState())}</dd>
          <dt>Last contact</dt><dd>${app.lastOk() ? ago(new Date(app.lastOk()).toISOString()) : "never"}</dd>
          <dt>Mode</dt><dd>${esc(cfg ? cfg.mode : "unknown")}</dd>
          <dt>Decisions</dt><dd>recorded as <b>${esc(cfg ? cfg.decision_label : "developer (app)")}</b></dd>
        </dl></div></section>
      ${cfg && cfg.auth_required ? `<section class="sec"><h3 class="sh">Sign in</h3><div class="card pad">
        ${cfg.signed_in ? `<p style="margin:0">Signed in to this AIRA backend.</p><div class="btns"><button class="btn small" id="signout" type="button">Sign out</button></div>`
        : `<p style="margin:0" class="muted">This backend is reachable from other devices, so it needs its access token (AIRA_ACCESS_TOKEN).</p>
           <div class="field"><label for="tok">Access token</label><input id="tok" type="password" autocomplete="current-password"></div>
           <div class="btns"><button class="btn primary small" id="signin" type="button">Sign in</button></div>`}
      </div></section>` : ""}
      <section class="sec"><h3 class="sh">Notifications</h3><div class="card pad">${this.notifyCard(p)}</div></section>
      <section class="sec"><h3 class="sh">App</h3><div class="card pad">
        <p style="margin:0">${standalone ? "Running as an installed app." : app.installPrompt()
          ? "AIRA can be installed on this device." : "To install: open the browser menu and choose <b>Install app</b> or <b>Add to Home screen</b>. Installing needs HTTPS or localhost."}</p>
        <div class="btns">${!standalone && app.installPrompt() ? `<button class="btn primary small" id="install" type="button">Install AIRA</button>` : ""}
          <a class="btn small" href="/" style="display:grid;place-items:center">Open the web console</a>
          <button class="btn small quiet" id="clear" type="button">Clear offline copies</button></div>
      </div></section>
      <section class="sec"><h3 class="sh">System health</h3>
        ${checks.length ? healthRows(checks)
          : this.healthPending ? `<div class="list pad">${loading(3)}</div>` : empty("Health unavailable")}</section>`;

    const on = (id, fn) => { const b = this.ctx.el.querySelector(id); if (b) b.onclick = fn; };
    on("#signin", async () => {
      const r = await postJSON("/api/session", { token: this.ctx.el.querySelector("#tok").value }).catch(e => ({ ok: false, data: { detail: e.message } }));
      if (r.ok) { toast("Signed in."); app.resync("signed in"); this.render(this.ctx); } else toast(r.data.detail || "That token was not accepted.", true);
    });
    on("#signout", async () => { await deleteJSON("/api/session"); toast("Signed out."); this.render(this.ctx); });
    on("#install", async () => { const ev = app.installPrompt(); if (!ev) return; ev.prompt(); await ev.userChoice.catch(() => null); app.clearInstallPrompt(); this.paint(); });
    on("#clear", () => { clearCache(); toast("Offline copies cleared."); });
    on("#push-on", () => this.enablePush());
    on("#push-retry", () => this.enablePush(true));
    on("#push-off", () => this.disablePush());
    on("#push-test", () => this.testNotify());
  },
  notifyCard(p) {
    const intro = `<p style="margin:0" class="muted">Alerts for new and critical incidents, remediations waiting for your approval, and results (executed, failed, recovered, resolved).</p>`;
    if (!p.supported) return intro + `<p class="note">This browser cannot show notifications. The Activity tab lists the same events.</p>`;
    if (!p.secure) return intro + `<p class="note">Notifications need a secure address: open AIRA as <b>localhost</b> through adb, or over HTTPS (for example Tailscale). This page was opened over plain HTTP.</p>`;
    if (p.permission === "denied") return intro + `<p class="note" style="color:var(--red)">Blocked for this app. Allow them in Android Settings &rarr; Apps &rarr; AIRA (or Chrome) &rarr; Notifications, then come back here.</p>`;
    const on = p.wanted && p.permission === "granted";
    if (!on) return intro + `<p class="note">Status: off</p><div class="btns"><button class="btn primary small" id="push-on" type="button">Turn on notifications</button></div>`;
    const status = p.sub
      ? `<p class="note" style="color:var(--green)">On, including when AIRA is closed.</p>`
      : this.pushing
      ? `<p class="note" style="color:var(--green)">On while AIRA is open or in your recent apps.</p>
         <p class="note">Setting up alerts for when AIRA is closed&hellip; (this can take up to 15 seconds)</p>`
      : `<p class="note" style="color:var(--green)">On while AIRA is open or in your recent apps.</p>
         <div class="pushhelp">
           <b>Alerts when AIRA is fully closed are not available on this phone yet.</b>
           <p class="note">The phone's push service refused the registration${p.pushError ? ` (${esc(p.pushError)})` : ""}. This is a phone setting, not AIRA. On Android it is usually fixed by:</p>
           <ul class="f">
             <li>Settings &rarr; Apps &rarr; <b>Chrome</b> &rarr; Battery &rarr; <b>No restrictions</b>, and allow <b>Autostart</b> / background activity (Xiaomi, Oppo, Vivo and Huawei restrict this).</li>
             <li>Settings &rarr; Apps &rarr; <b>Google Play services</b>: enabled, updated, background data allowed.</li>
             <li>Turning off any VPN, ad blocker or Private DNS that blocks Google's push service.</li>
           </ul>
           <div class="btns"><button class="btn small" id="push-retry" type="button">Try background alerts again</button></div>
         </div>`;
    return intro + status + `<div class="btns"><button class="btn small" id="push-test" type="button">Send a test</button>
      <button class="btn small quiet" id="push-off" type="button">Turn off</button></div>`;
  },
  async enablePush(retry = false) {
    let perm = Notification.permission;
    if (perm !== "granted") perm = await Notification.requestPermission();
    if (perm !== "granted") { toast("Notifications were not allowed.", true); this.push = await this.pushState(); return this.paint(); }
    notify.setWanted(true);
    // Alerts already work from this moment (through the live connection), so say so straight away
    // rather than leave "off" on screen while the phone talks to its push service.
    this.pushing = true;
    this.push = await this.pushState(); this.paint();
    // Background push is attempted on top. If the phone's push service refuses - or does not
    // answer within 15 s - alerts still arrive whenever AIRA is open or in recent apps.
    const within = (promise, ms, what) => Promise.race([promise, new Promise((_, no) =>
      setTimeout(() => no(new Error(`${what} did not answer within ${ms / 1000}s`)), ms))]);
    let pushed = false;
    try {
      if (!("PushManager" in window)) throw new Error("this browser has no push service");
      const reg = await navigator.serviceWorker.ready;
      const old = await reg.pushManager.getSubscription();
      if (old && retry) await old.unsubscribe().catch(() => {});
      const { data } = await getJSON("/api/push/key");
      const sub = (!retry && old) || await within(
        reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uToBytes(data.public_key) }),
        15000, "the phone's push service");
      const r = await postJSON("/api/push/subscribe", { subscription: sub.toJSON() });
      if (!r.ok) throw new Error(r.data.detail || `AIRA answered ${r.status}`);
      localStorage.removeItem("aira-push-error");
      pushed = true;
    } catch (e) {
      localStorage.setItem("aira-push-error", String((e && e.message) || e).slice(0, 160));
    }
    this.pushing = false;
    toast(pushed ? "Notifications on, including when AIRA is closed."
                 : "Notifications on while AIRA is open. Background alerts need a phone setting, see below.", !pushed && retry);
    this.push = await this.pushState(); this.paint();
  },
  async testNotify() {
    const shown = await notify.show({ title: "AIRA test notification", body: "This is how incident and approval alerts will look.",
                                      key: `test-${Date.now()}`, level: "info" });
    let pushNote = "";
    if (this.push.sub) {
      const r = await postJSON("/api/push/test", { endpoint: this.push.sub.endpoint }).catch(e => ({ ok: false, data: { detail: e.message } }));
      pushNote = r.ok && r.data.ok ? " A background push was also sent; it can take a few seconds."
        : ` The background push was not delivered (${r.data.detail || `push service answered ${r.data.push_service_status}`}).`;
    }
    toast((shown ? "Test notification shown." : "The phone did not show it; check its notification settings for AIRA.") + pushNote, !shown);
  },
  async disablePush() {
    notify.setWanted(false);
    try {
      const sub = this.push.sub;
      if (sub) { await postJSON("/api/push/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); }
    } catch {}
    toast("Notifications are off for this device.");
    this.push = await this.pushState(); this.paint();
  },
  onChange() {},
  onResync() {},
};
