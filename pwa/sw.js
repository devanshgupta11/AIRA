/* AIRA service worker.
 *
 * Deliberately minimal. It exists so the console can be installed as an app, and it does one
 * thing beyond that: when a page cannot be loaded at all (no network, or the server is down) it
 * shows a small offline page instead of the browser's error screen.
 *
 * It NEVER caches incident data, API responses or approvals. Every request other than a page
 * navigation goes straight to the network untouched, and page navigations do too whenever the
 * network works. An incident console that showed stale data - or let someone approve an action
 * against a cached view - would be worse than one that says it is offline.
 */

const VERSION = "aira-pwa-v1";
const OFFLINE_URL = "/pwa/offline.html";
const SHELL = [OFFLINE_URL, "/pwa/icons/icon-192.png"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(VERSION).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const req = event.request;
  // Only full page loads are handled. Everything else - /incidents polling, approve/reject POSTs,
  // PDFs, metrics - is not intercepted at all, so it behaves exactly as it does in the browser.
  if (req.method !== "GET" || req.mode !== "navigate") return;
  event.respondWith(
    fetch(req).catch(() =>
      caches.match(OFFLINE_URL).then(r => r || new Response("AIRA is offline", {
        status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" },
      })))
  );
});
