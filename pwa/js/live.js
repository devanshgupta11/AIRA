/* The live connection: Server-Sent Events from /api/stream (backend/app/live.py).
 *
 * A message only says which incidents changed. The app then re-reads them through the normal API,
 * so nothing shown ever comes from the stream itself - a missed, repeated or out-of-order message
 * cannot leave wrong state on screen. After any gap (reconnect, backend restart, phone asleep) the
 * app is told to resync and re-reads everything it is showing.
 *
 * Phones pause apps in the background and the connection can die without any error reaching the
 * page. So the backend sends a `ping` every 15 s, and if nothing at all has arrived for 40 s the
 * connection is replaced. Coming back to the app always replaces it straight away.
 */

const STALE_MS = 40000;

export class Live {
  constructor({ onChange, onResync, onState }) {
    this.onChange = onChange; this.onResync = onResync; this.onState = onState;
    this.es = null; this.boot = null; this.state = "connecting"; this.hadError = false;
    this.lastMessageAt = 0;
    this.hiddenAt = 0;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") { this.hiddenAt = Date.now(); return; }
      // Back in the app: a stream that survived the pause may still be a dead socket, so start a
      // fresh one rather than trust it, and re-read what is on screen.
      if (Date.now() - this.hiddenAt > 3000 || !this.es || this.es.readyState !== EventSource.OPEN) this.start();
      this.onResync("returned to the app");
    });
    window.addEventListener("online", () => { this.start(); this.onResync("back online"); });
    setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (this.es && this.es.readyState === EventSource.OPEN && this.lastMessageAt
          && Date.now() - this.lastMessageAt > STALE_MS) {
        this.set("reconnecting");
        this.start();
      }
    }, 10000);
  }

  set(state) {
    if (this.state !== state) { this.state = state; this.onState(state); }
  }

  start() {
    if (this.es) this.es.close();
    this.set(this.state === "live" ? "reconnecting" : "connecting");
    const es = new EventSource("/api/stream");
    this.es = es;
    this.lastMessageAt = Date.now();
    const alive = () => { this.lastMessageAt = Date.now(); if (this.es === es) this.set("live"); };
    es.onopen = () => {
      alive();
      if (this.hadError) { this.hadError = false; this.onResync("reconnected"); }
    };
    es.onerror = () => {
      if (this.es !== es) return;
      // EventSource reconnects by itself (with Last-Event-ID); until then the app is not live.
      this.hadError = true;
      this.set(navigator.onLine ? "reconnecting" : "offline");
    };
    es.addEventListener("ping", alive);
    es.addEventListener("hello", e => {
      alive();
      const m = JSON.parse(e.data);
      if (this.boot && this.boot !== m.boot) this.onResync("the backend restarted");
      this.boot = m.boot;
      this.onChange({ incidents: [], activity: [], summary: m.summary, hello: true });
    });
    es.addEventListener("change", e => {
      alive();
      try { this.onChange(JSON.parse(e.data)); } catch {}
    });
    es.addEventListener("resync", () => { alive(); this.onResync("missed updates"); });
  }
}
