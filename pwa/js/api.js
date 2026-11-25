/* Talking to the AIRA backend.
 *
 * Everything is same-origin and relative: the app is served by the backend it talks to, so it works
 * at whatever address the phone used to open it (localhost through adb, a LAN address, a domain).
 *
 * Reads (getJSON) remember their last good answer so the app can still show something offline -
 * and always says so: a cached answer comes back with stale=true and the time it was fetched.
 * Writes (postJSON) are never cached and never queued. An approval either succeeds against the
 * live backend now, or it did not happen.
 */

const PREFIX = "aira-cache:";
const MAX_ENTRY = 1_500_000;    // characters; larger answers are simply not kept offline
const DETAIL_KEEP = 15;         // incident details kept for offline viewing

export class HttpError extends Error {
  constructor(status, body) {
    super((body && body.detail) || `HTTP ${status}`);
    this.status = status; this.body = body;
  }
}
export class OfflineError extends Error {}

// ---------------------------------------------------------------- connectivity

export const net = { reachable: navigator.onLine, lastOk: 0 };
const listeners = new Set();
export const onNet = fn => { listeners.add(fn); return () => listeners.delete(fn); };

function setReachable(ok) {
  if (ok) net.lastOk = Date.now();
  if (net.reachable !== ok) {
    net.reachable = ok;
    listeners.forEach(fn => { try { fn(net); } catch {} });
  }
}
window.addEventListener("offline", () => setReachable(false));

// ---------------------------------------------------------------- offline copies

function save(key, data) {
  try {
    const text = JSON.stringify({ at: Date.now(), data });
    if (text.length > MAX_ENTRY) return;
    localStorage.setItem(PREFIX + key, text);
    if (key.startsWith("incident:")) {
      const idx = JSON.parse(localStorage.getItem(PREFIX + "_details") || "[]").filter(k => k !== key);
      idx.unshift(key);
      idx.slice(DETAIL_KEEP).forEach(k => localStorage.removeItem(PREFIX + k));
      localStorage.setItem(PREFIX + "_details", JSON.stringify(idx.slice(0, DETAIL_KEEP)));
    }
  } catch { /* storage full or unavailable: offline copies are a convenience */ }
}

function load(key) {
  try { return JSON.parse(localStorage.getItem(PREFIX + key) || "null"); } catch { return null; }
}

// ---------------------------------------------------------------- requests

export async function getJSON(path, cacheKey = null) {
  let r;
  try {
    r = await fetch(path, { credentials: "same-origin", cache: "no-store",
                            headers: { Accept: "application/json" } });
  } catch {
    setReachable(false);
    const c = cacheKey && load(cacheKey);
    if (c) return { data: c.data, stale: true, at: c.at };
    throw new OfflineError("The AIRA backend could not be reached.");
  }
  setReachable(true);
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new HttpError(r.status, body);
  if (cacheKey) save(cacheKey, body);
  return { data: body, stale: false, at: Date.now() };
}

/* State-changing request. Returns {status, ok, data}; throws OfflineError if it never reached the
   backend, in which case nothing happened. */
export async function postJSON(path, body) {
  let r;
  try {
    r = await fetch(path, { method: "POST", credentials: "same-origin",
                            headers: { "Content-Type": "application/json", Accept: "application/json" },
                            body: JSON.stringify(body ?? {}) });
  } catch {
    setReachable(false);
    throw new OfflineError("Not sent: the AIRA backend could not be reached. Nothing was changed.");
  }
  setReachable(true);
  const data = await r.json().catch(() => ({}));
  return { status: r.status, ok: r.ok, data };
}

export async function deleteJSON(path) {
  const r = await fetch(path, { method: "DELETE", credentials: "same-origin" });
  return r.ok;
}

export function requestId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
}

export function clearCache() {
  Object.keys(localStorage).filter(k => k.startsWith(PREFIX)).forEach(k => localStorage.removeItem(k));
}
