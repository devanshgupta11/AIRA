/* Notifications about incidents, approvals and results.
 *
 * Two routes, so an alert reaches the phone whichever one this device supports:
 *
 *  1. While AIRA is open or in recent apps, the live connection already delivers every event within
 *     a second. Those are shown as system notifications from here, through the service worker. This
 *     needs nothing but notification permission - no push service.
 *  2. When AIRA is fully closed, only Web Push can wake it. That needs the browser's push service
 *     (Google's, on Android) to accept a registration, which some phones block (battery or
 *     background restrictions on Chrome, restricted Google Play services, VPNs/ad blockers).
 *
 * Both use the event's key as the notification tag, so if both deliver the same event it is shown
 * once, not twice.
 */

const PREF = "aira-notify";

// Worth interrupting a person for. Investigation progress stays in the Activity tab.
export const ALERT_KINDS = new Set([
  "incident_opened", "incident_resolved", "action_proposed", "action_approved", "action_rejected",
  "action_executed", "action_refused", "action_failed", "verification_recovered",
  "verification_not_recovered", "remediation_exhausted",
]);

export const supported = () => "Notification" in window && "serviceWorker" in navigator;
export const wanted = () => localStorage.getItem(PREF) === "on";
export const setWanted = on => localStorage.setItem(PREF, on ? "on" : "off");
export const enabled = () => supported() && wanted() && Notification.permission === "granted";

export async function show(item) {
  if (!enabled()) return false;
  const opts = {
    body: item.body || "",
    icon: "/app/icons/icon-192.png",
    badge: "/app/icons/icon-192.png",
    tag: item.key,                                   // same event from push and from here = one
    renotify: false,
    requireInteraction: item.level === "approval",
    data: { url: item.incident_id ? `/app/#/incident/${item.incident_id}` : "/app/#/activity" },
  };
  try {
    const reg = await navigator.serviceWorker.getRegistration("/app/");
    if (reg) { await reg.showNotification(item.title, opts); return true; }
    new Notification(item.title, opts);
    return true;
  } catch { return false; }
}

/* Called for every live change message. */
export function fromLive(activity) {
  for (const a of activity || []) {
    if (ALERT_KINDS.has(a.kind)) show(a);
  }
}
