/* Small shared helpers: escaping, time, labels, the confirmation sheet and the toast. */

export const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const $ = (sel, root = document) => root.querySelector(sel);

export function parseTime(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

export function ago(iso) {
  const t = parseTime(iso);
  if (t == null) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString();
}

export function clock(iso) {
  const t = parseTime(iso);
  if (t == null) return "—";
  const d = new Date(t);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
                 : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function duration(a, b) {
  const ta = parseTime(a), tb = b ? parseTime(b) : Date.now();
  if (ta == null || tb == null) return "—";
  const s = Math.max(0, Math.round((tb - ta) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export const unavailable = (what = "Not available") => `<span class="faint">${esc(what)}</span>`;

/* Run `fn` at most once per `ms`, and once more after a burst ends, never two at a time. While an
   investigation runs the backend reports a change about every second; screens re-read through this
   so they stay current without re-fetching on every single message. */
export function coalesce(fn, ms = 1200) {
  let running = false, again = false, last = 0;
  const run = async () => {
    if (running) { again = true; return; }
    running = true;
    const wait = Math.max(0, last + ms - Date.now());
    if (wait) await new Promise(r => setTimeout(r, wait));
    try { await fn(); } finally {
      last = Date.now(); running = false;
      if (again) { again = false; run(); }
    }
  };
  return run;
}

// ---------------------------------------------------------------- labels

export function statusPill(inc) {
  return inc.status === "firing"
    ? `<span class="pill red">Firing</span>` : `<span class="pill green">Resolved</span>`;
}

export function sevPill(sev) {
  if (!sev) return "";
  return `<span class="pill ${sev === "critical" ? "red" : sev === "warning" ? "amber" : ""}">${esc(sev)}</span>`;
}

export function outcomePill(outcome) {
  if (!outcome) return "";
  const map = { accepted: ["purple", "cause accepted"], insufficient: ["amber", "cause unconfirmed"],
                error: ["red", "reasoning error"] };
  const [cls, label] = map[outcome] || ["", outcome];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

const ACTION_STATUS = {
  proposed: ["amber", "awaiting approval"], approved: ["blue", "executing"],
  executed: ["green", "executed"], rejected: ["", "rejected"], refused: ["red", "refused by safety layer"],
  failed: ["red", "failed"], declined: ["", "no safe action"], superseded: ["", "superseded"],
};

export function actionPill(a) {
  if (!a) return "";
  if (a.status === "executed" && a.outcome === "not_yet") return `<span class="pill blue">verifying</span>`;
  if (a.status === "executed" && a.outcome === "recovered") return `<span class="pill green">recovered</span>`;
  if (a.status === "executed" && a.outcome === "not_recovered") return `<span class="pill red">not recovered</span>`;
  const [cls, label] = ACTION_STATUS[a.status] || ["", a.status];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

/* The exact thing an action will do, in words, from the stored action. Never paraphrased by a model. */
export function actionSentence(a) {
  const p = a.params || {};
  if (a.action === "reset_feature_flag")
    return `Set feature flag <code>${esc(p.flag || a.flag || "?")}</code> to <code>${esc(p.to_variant || "off")}</code> for <b>${esc(a.target_service)}</b>`;
  if (a.action === "restart_container")
    return `Restart the <b>${esc(a.target_service)}</b> container`;
  if (a.action === "none") return "No action: AIRA judged that no safe action applies";
  return `${esc(a.action)} on <b>${esc(a.target_service || "?")}</b>`;
}

export const confPill = c => c ? `<span class="pill ${c === "high" ? "green" : c === "medium" ? "amber" : ""}">confidence ${esc(c)}</span>` : "";

// ---------------------------------------------------------------- loading / empty / error

export const loading = (rows = 4) =>
  `<div aria-busy="true">${Array.from({ length: rows }, (_, i) =>
    `<div class="skel" style="width:${90 - i * 12}%"></div>`).join("")}</div>`;

export const empty = (title, text = "") =>
  `<div class="state"><b>${esc(title)}</b>${esc(text)}</div>`;

export const failed = (title, text = "") =>
  `<div class="state err"><b>${esc(title)}</b>${esc(text)}</div>`;

// ---------------------------------------------------------------- Markdown (postmortem reports)

export function md(src) {
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  const lines = String(src || "").split(/\r?\n/);
  const out = [];
  let list = null, table = null;
  const flush = () => {
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (table) {
      const [head, ...body] = table.filter(r => !/^\s*\|?\s*:?-{2,}/.test(r));
      const cells = r => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(c => c.trim());
      out.push(`<table><thead><tr>${cells(head).map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
        body.map(r => `<tr>${cells(r).map(c => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      table = null;
    }
  };
  for (const line of lines) {
    if (/^\s*\|/.test(line)) { if (list) flush(); (table ||= []).push(line); continue; }
    if (table) flush();
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) { flush(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) { (list ||= []).push(`<li>${inline(li[1])}</li>`); continue; }
    if (/^\s*>/.test(line)) { flush(); out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`); continue; }
    if (/^\s*---\s*$/.test(line)) { flush(); out.push("<hr>"); continue; }
    if (!line.trim()) { flush(); continue; }
    flush(); out.push(`<p>${inline(line)}</p>`);
  }
  flush();
  return `<div class="md">${out.join("")}</div>`;
}

// ---------------------------------------------------------------- toast

let toastTimer = null;
export function toast(text, bad = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 3500);
}

// ---------------------------------------------------------------- bottom sheet (confirmations)

/* Opens a sheet with the given HTML. Buttons with data-value resolve the promise with that value;
   Escape, the backdrop and data-value="" cancel (resolve null). `collect` reads form fields. */
export function sheet(html, collect = null) {
  const s = $("#sheet"), bd = $("#sheet-backdrop");
  s.innerHTML = html;
  s.hidden = false; bd.hidden = false;
  const prevFocus = document.activeElement;
  const first = s.querySelector("[data-focus]") || s.querySelector("button");
  if (first) first.focus();
  return new Promise(resolve => {
    const done = value => {
      s.hidden = true; bd.hidden = true; s.innerHTML = "";
      document.removeEventListener("keydown", onKey);
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      resolve(value);
    };
    const onKey = e => { if (e.key === "Escape") done(null); };
    document.addEventListener("keydown", onKey);
    bd.onclick = () => done(null);
    s.querySelectorAll("[data-value]").forEach(b => {
      b.onclick = () => {
        const v = b.dataset.value;
        if (!v) return done(null);
        done(collect ? { value: v, ...collect(s) } : { value: v });
      };
    });
  });
}
