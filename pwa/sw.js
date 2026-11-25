/* AIRA mobile app - service worker. Scope: /app/ only, so it never touches the web console.
 *
 * What it does:
 *   - Keeps the app shell (HTML, CSS, scripts, icons) so the app opens without a connection.
 *     Shell files are fetched from the network first and the copy is refreshed each time, so an
 *     updated app reaches the phone on its next load; the cached copy is only the fallback.
 *   - Shows Web Push notifications and opens the right incident when one is tapped.
 *
 * What it deliberately does NOT do: touch any data request. /api/*, /incidents/*, /actions/* and
 * /health are never intercepted or cached here. The app keeps its own clearly-labelled stale copy
 * for offline viewing (js/api.js), and an approval can only ever succeed against the live backend.
 */

const VERSION = "aira-app-v3";
const SHELL = [
  "/app/",
  "/app/app.css",
  "/app/offline.html",
  "/app/manifest.webmanifest",
  "/app/js/app.js",
  "/app/js/api.js",
  "/app/js/live.js",
  "/app/js/ui.js",
  "/app/js/views.js",
  "/app/js/detail.js",
  "/app/js/notify.js",
  "/app/icons/icon-192.png",
  "/app/icons/icon-512.png",
  "/app/icons/apple-touch-icon.png",
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const req = event.request;
  const url = new URL(req.url);
  // Only GETs of the app's own files. Everything else goes to the network untouched.
  if (req.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/app/")) return;

  event.respondWith((async () => {
    const cache = await caches.open(VERSION);
    try {
      const fresh = await fetch(req, { cache: "no-cache" });
      if (fresh.ok) cache.put(req.mode === "navigate" ? "/app/" : req, fresh.clone());
      return fresh;
    } catch {
      if (req.mode === "navigate") {
        return (await cache.match("/app/")) || (await cache.match("/app/offline.html"));
      }
      const hit = await cache.match(req, { ignoreSearch: true });
      return hit || new Response("offline", { status: 503, statusText: "offline" });
    }
  })());
});

// ---------------------------------------------------------------- push notifications

self.addEventListener("push", event => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch { msg = {}; }
  const title = msg.title || "AIRA";
  const options = {
    body: msg.body || "Something changed in AIRA.",
    icon: "/app/icons/icon-192.png",
    badge: "/app/icons/icon-192.png",
    tag: msg.key || undefined,            // the same event twice replaces, it does not stack
    renotify: msg.level === "approval" || msg.level === "critical",
    requireInteraction: msg.level === "approval",
    data: { url: msg.url || "/app/#/activity" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/app/#/",
                         location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.startsWith(location.origin + "/app/")) {
        await w.focus();
        w.postMessage({ type: "navigate", url: target });
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});
