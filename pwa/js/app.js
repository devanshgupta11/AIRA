/* AIRA mobile app: routing, the shell (top bar, tabs, badges, banners), the live connection and
 * the service worker. Screens live in views.js and detail.js. */

import { getJSON, net, onNet } from "./api.js";
import { Live } from "./live.js";
import { $, clock, toast } from "./ui.js";
import { dashboardView, incidentsView, approvalsView, activityView, settingsView, activitySeen } from "./views.js";
import { incidentView } from "./detail.js";
import * as notify from "./notify.js";

const ROUTES = [
  { re: /^\/?$/, view: dashboardView, tab: "dashboard" },
  { re: /^\/incidents\/?$/, view: incidentsView, tab: "incidents" },
  { re: /^\/incident\/(\d+)\/?$/, view: incidentView, tab: "incidents", keys: ["id"], back: true },
  { re: /^\/approvals\/?$/, view: approvalsView, tab: "approvals" },
  { re: /^\/activity\/?$/, view: activityView, tab: "activity" },
  { re: /^\/settings\/?$/, view: settingsView, tab: "settings" },
];

let current = null;
let liveState = "connecting";
let staleAt = null;
let installEvent = null;
let summary = null;

// ---------------------------------------------------------------- the app object views talk to

const app = {
  go(hash) { if (location.hash === hash) route(); else location.hash = hash; },
  setTitle(t) { $("#title").textContent = t; document.title = `${t} · AIRA`; },
  setStale(at) { staleAt = at; paintBanner(); },
  setSummary(s) { if (s) { summary = s; paintBadges(); } },
  setUnread(on) { $("#badge-activity").hidden = !on; },
  rerender() { if (current && current.view.paint) current.view.paint(); },
  resync(reason) { resync(reason); },
  needSignIn() { paintBanner("This AIRA backend needs its access token. Open Settings to sign in.", true); },
  liveState: () => liveState,
  lastOk: () => net.lastOk,
  installPrompt: () => installEvent,
  clearInstallPrompt() { installEvent = null; },
};

// ---------------------------------------------------------------- routing

function parse() {
  const raw = (location.hash || "#/").slice(1);
  const [path, qs] = raw.split("?");
  return { path: path || "/", query: Object.fromEntries(new URLSearchParams(qs || "")) };
}

async function route() {
  const { path, query } = parse();
  const r = ROUTES.find(x => x.re.test(path)) || ROUTES[0];
  const m = path.match(r.re) || [];
  const params = Object.fromEntries((r.keys || []).map((k, i) => [k, m[i + 1]]));
  current = { view: r.view, params };
  document.querySelectorAll(".tabbar a").forEach(a => a.classList.toggle("on", a.dataset.tab === r.tab));
  $("#back").hidden = !r.back;
  $("#brand").hidden = !!r.back;
  app.setTitle(r.view.title);
  staleAt = null; paintBanner();
  const el = $("#view");
  if (!r.back) window.scrollTo(0, 0);
  try {
    await r.view.render({ el, params, query, app });
  } catch (e) {
    el.innerHTML = `<div class="state err"><b>Something went wrong</b>${String(e && e.message || e)}</div>`;
  }
}

// ---------------------------------------------------------------- shell state

function paintConn() {
  const c = $("#conn");
  const map = { live: ["live", "live"], connecting: ["wait", "connecting"], reconnecting: ["wait", "reconnecting"],
                offline: ["down", "offline"] };
  const [cls, label] = map[liveState] || ["wait", liveState];
  c.className = `conn ${cls}`;
  c.querySelector("span").textContent = label;
  c.title = liveState === "live" ? "Live: changes appear as they happen" : "Not live: showing the last data read";
}

let bannerOverride = null;
function paintBanner(text, bad) {
  if (text !== undefined) bannerOverride = text ? { text, bad } : null;
  const b = $("#banner");
  let msg = null, isBad = false;
  if (bannerOverride) { msg = bannerOverride.text; isBad = bannerOverride.bad; }
  else if (!navigator.onLine || !net.reachable) {
    // Say which it is: the phone has no connection, or the phone is fine and AIRA is not answering
    // (laptop asleep, backend restarting, adb reverse or Tailscale dropped).
    const why = !navigator.onLine ? "Your phone is offline." : "AIRA can't be reached right now.";
    msg = staleAt ? `${why} Showing what AIRA reported at ${clock(new Date(staleAt).toISOString())}; it may be out of date. Decisions are disabled until it is back.`
                  : `${why} Decisions are disabled until it is back.`;
    isBad = true;
  } else if (staleAt) {
    msg = `Showing data from ${clock(new Date(staleAt).toISOString())}; it may be out of date.`;
  } else if (liveState === "reconnecting") {
    msg = "Reconnecting to live updates… the data shown was current when it was read.";
  }
  b.hidden = !msg;
  if (msg) { b.textContent = msg; b.className = "banner" + (isBad ? " bad" : ""); }
}

function paintBadges() {
  const n = summary ? summary.pending_approvals : 0;
  const b = $("#badge-approvals");
  b.hidden = !n; b.textContent = n > 99 ? "99+" : String(n);
}

async function refreshBadges() {
  try { const { data } = await getJSON("/api/summary", "summary"); app.setSummary(data); } catch {}
  try {
    const { data } = await getJSON("/api/activity?limit=1");
    const newest = data.length ? Date.parse(data[0].at) || 0 : 0;
    app.setUnread(newest > activitySeen() && !(current && current.view === activityView));
  } catch {}
}

// ---------------------------------------------------------------- live updates

let resyncTimer = null;
function resync(reason) {
  clearTimeout(resyncTimer);
  resyncTimer = setTimeout(() => {
    if (current && current.view.onResync) current.view.onResync(reason);
    refreshBadges();
  }, 150);
}

const live = new Live({
  onChange(msg) {
    if (msg.summary) app.setSummary(msg.summary);
    // Alerts: a system notification (works whenever AIRA is open or in recent apps, no push
    // service needed), plus a short in-app note when the app is on screen.
    if (msg.activity && msg.activity.length) {
      notify.fromLive(msg.activity);
      if (document.visibilityState === "visible") {
        const urgent = msg.activity.find(a => a.kind === "incident_opened" || a.kind === "action_proposed");
        if (urgent) toast(`${urgent.title}: ${urgent.body}`, urgent.level === "critical");
      }
    }
    if (msg.activity && msg.activity.length && !(current && current.view === activityView)) app.setUnread(true);
    if (current && current.view.onChange && !msg.hello) current.view.onChange(msg);
  },
  onResync: reason => resync(reason),
  onState(s) {
    liveState = s;
    paintConn(); paintBanner();
    app.rerender();          // decision buttons follow connectivity
  },
});

onNet(() => { paintBanner(); app.rerender(); });
window.addEventListener("offline", () => { liveState = "offline"; paintConn(); paintBanner(); app.rerender(); });

// ---------------------------------------------------------------- wiring

window.addEventListener("hashchange", route);
$("#back").onclick = () => { if (history.length > 1) history.back(); else app.go("#/incidents"); };
$("#refresh").onclick = async () => {
  const b = $("#refresh");
  b.classList.add("spin");
  if (!live.es || live.es.readyState === EventSource.CLOSED) live.start();
  resync("manual refresh");
  setTimeout(() => b.classList.remove("spin"), 700);
};
$("#conn").onclick = () => app.go("#/settings");

window.addEventListener("beforeinstallprompt", e => { e.preventDefault(); installEvent = e; });
window.addEventListener("appinstalled", () => { installEvent = null; toast("AIRA is installed."); });

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch(() => {});
  navigator.serviceWorker.addEventListener("message", e => {
    if (e.data && e.data.type === "navigate" && e.data.url) {
      const u = new URL(e.data.url);
      if (u.hash) location.hash = u.hash;
    }
  });
}

paintConn();
live.start();
route();
refreshBadges();
getJSON("/api/client-config").then(({ data }) => {
  if (data.auth_required && !data.signed_in) app.needSignIn();
}).catch(() => {});
