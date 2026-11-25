/* One incident, in full: what fired, what every agent found, the root cause and the Critic's
 * verdict, every remediation with its approval and execution progress, the trace and the report.
 * Everything comes from GET /incidents/{id} (and, when opened, its events and postmortem). A field
 * the backend does not have is shown as unavailable, never filled in.
 *
 * Also home to the approval flow, which the Approvals screen shares: one confirmation, one request
 * to the same endpoint the web console uses, and a result only once the backend has answered.
 */

import { getJSON, postJSON, requestId, HttpError, net } from "./api.js";
import { esc, ago, clock, duration, statusPill, sevPill, outcomePill, actionPill, actionSentence,
         confPill, loading, failed, md, toast, sheet, unavailable, coalesce } from "./ui.js";

const AGENT = {
  triage: "Triage", metrics: "Metrics", log: "Log", change: "Change", rca: "Root cause",
  critic: "Critic", remediation: "Remediation", conclusion: "Conclusion", postmortem: "Postmortem",
};

// ---------------------------------------------------------------- approval (shared)

const inFlight = new Set();       // action ids with a decision being sent right now

export function canDecide() { return navigator.onLine && net.reachable; }

export function approvalCard(a, inc, { compact = false } = {}) {
  const busy = inFlight.has(a.id);
  const resolved = inc && inc.status === "resolved";
  return `<div class="act waiting" data-action="${a.id}">
    <div class="act-h"><b>Remediation awaiting approval</b>${riskPill(a.risk)}
      ${compact ? `<a class="faint" href="#/incident/${a.incident_id}" style="margin-left:auto">#${a.incident_id} ›</a>` : ""}</div>
    <div class="what">${actionSentence(a)}</div>
    ${a.reasoning ? `<div class="why">${esc(a.reasoning)}</div>` : ""}
    ${a.expected_effect ? `<div class="small">Expected effect: ${esc(a.expected_effect)}</div>` : ""}
    ${a.reversal ? `<div class="small">Reversal: ${esc(a.reversal)}</div>` : ""}
    ${resolved ? `<div class="small" style="color:var(--amber)">This incident has already resolved, so the
      condition this was proposed for may be gone. The safety layer re-checks it before anything runs.</div>` : ""}
    <div class="btns">
      <button class="btn danger" data-decide="reject" data-id="${a.id}" ${busy || !canDecide() ? "disabled" : ""}>Reject</button>
      <button class="btn primary" data-decide="approve" data-id="${a.id}" ${busy || !canDecide() ? "disabled" : ""}>${busy ? "Sending…" : "Approve…"}</button>
    </div>
    ${!canDecide() ? `<div class="small" style="color:var(--red)">Offline: a decision needs a live connection to AIRA.</div>` : ""}
  </div>`;
}

const riskPill = r => r ? `<span class="pill ${r === "low" ? "green" : r === "medium" ? "amber" : "red"}">${esc(r)} risk</span>` : "";

/* Wire every Approve/Reject button inside `root`. `lookup(id)` returns {action, incident}. */
export function wireDecisions(root, lookup, app) {
  root.querySelectorAll("[data-decide]").forEach(b => {
    b.onclick = async () => {
      const found = lookup(Number(b.dataset.id));
      if (found) await decide(found.action, found.incident, b.dataset.decide, app);
    };
  });
}

export async function decide(action, incident, kind, app) {
  if (!canDecide()) { toast("You're offline. A decision needs a live connection to AIRA.", true); return; }
  const incLine = incident
    ? `#${incident.id} · ${esc(incident.service || "unknown")} · ${esc(incident.alertname)} · ${incident.status}`
    : `#${action.incident_id}`;
  const facts = `<dl class="kv">
      <dt>Incident</dt><dd>${incLine}</dd>
      <dt>Target</dt><dd>${esc(action.target_service || "—")}</dd>
      <dt>Risk</dt><dd>${esc(action.risk || "—")}</dd>
      ${action.expected_effect ? `<dt>Expected</dt><dd>${esc(action.expected_effect)}</dd>` : ""}
      ${action.reversal ? `<dt>Reversal</dt><dd>${esc(action.reversal)}</dd>` : ""}
    </dl>`;
  const html = kind === "approve"
    ? `<h3 id="sheet-title">Approve and run this remediation?</h3>
       <p class="muted" style="margin:0">This changes the running system as soon as you confirm.</p>
       <div class="exact">${actionSentence(action)}</div>${facts}
       <p class="note">AIRA re-checks the action against its allowlist at the moment it runs. Monitoring,
         flagd, the ingress proxy and stateful stores can never be acted on. If someone has already
         decided on this from the web console or another device, nothing will run twice.</p>
       <p class="note">Recorded as <b>developer (app)</b>.</p>
       <div class="btns"><button class="btn quiet" data-value="">Cancel</button>
         <button class="btn primary" data-value="approve" data-focus>Approve and run</button></div>`
    : `<h3 id="sheet-title">Reject this remediation?</h3>
       <div class="exact">${actionSentence(action)}</div>${facts}
       <div class="field"><label for="reject-note">Reason (optional, kept on the record)</label>
         <textarea id="reject-note" maxlength="400"></textarea></div>
       <p class="note">Nothing will be executed. Recorded as <b>developer (app)</b>.</p>
       <div class="btns"><button class="btn quiet" data-value="">Cancel</button>
         <button class="btn danger" data-value="reject" data-focus>Reject</button></div>`;
  const res = await sheet(html, s => ({ note: (s.querySelector("textarea") || {}).value || "" }));
  if (!res) return;

  inFlight.add(action.id);
  app.rerender();
  let r;
  try {
    r = await postJSON(`/actions/${action.id}/${kind}`, {
      source: "app", incident_id: action.incident_id, request_id: requestId(),
      client_time: new Date().toISOString(), note: res.note || "",
    });
  } catch (e) {
    inFlight.delete(action.id); app.rerender();
    toast(e.message, true);
    return;
  }
  inFlight.delete(action.id);
  const d = r.data || {};
  if (r.status === 401) { toast("Sign in first: this AIRA backend needs its access token.", true); app.go("#/settings"); return; }
  if (r.ok) {
    const s = d.status;
    if (d.duplicate) toast("Already recorded. Nothing was done twice.");
    else if (s === "executed") toast(d.ok ? "Approved and executed. Verifying recovery now." : "Approved and executed, but not confirmed at the source.", !d.ok);
    else if (s === "refused") toast(`Approved, but the safety layer refused it: ${d.detail || ""}`, true);
    else if (s === "failed") toast(`Approved, but execution failed: ${d.detail || ""}`, true);
    else if (s === "rejected") toast("Rejected. Nothing was executed.");
    else toast(`Recorded: ${s}`);
  } else if (r.status === 409) {
    toast(d.detail || "Someone else decided first.", true);
  } else {
    toast(`Could not ${kind}: ${d.detail || `HTTP ${r.status}`}`, true);
  }
  app.resync("decision sent");
}

// ---------------------------------------------------------------- execution progress

/* The lifecycle of one action, built only from what the backend recorded on it. */
export function progress(a, inc) {
  const steps = [];
  const add = (cls, title, when, detail) => steps.push({ cls, title, when, detail });
  add("done", "Proposed", a.proposed_at, a.risk ? `${a.risk} risk` : "");
  switch (a.status) {
    case "proposed": add("now", "Waiting for a human decision"); break;
    case "superseded": add("skip", "Superseded", a.decided_at, a.decision_note); break;
    case "declined": add("skip", "No safe action applies", a.proposed_at, a.reasoning); break;
    case "rejected": add("done", `Rejected by ${a.decided_by || "a developer"}`, a.decided_at, a.decision_note); break;
    default: add("done", `Approved by ${a.decided_by || "a developer"}`, a.decided_at, a.decision_note);
  }
  if (a.status === "approved") add("now", "Safety re-check and execution in progress");
  if (a.status === "refused") add("bad", "Refused by the safety layer when it came to run", a.executed_at, a.result_detail);
  if (a.status === "failed") {
    add("done", "Safety re-check passed");
    add("bad", "Execution failed", a.executed_at, a.result_detail);
  }
  if (a.status === "executed") {
    add("done", "Safety re-check passed");
    add(a.result_ok ? "done" : "bad", a.result_ok ? "Executed and confirmed at the source" : "Executed, but not confirmed",
        a.executed_at, [a.result_detail, a.verified_by && `checked: ${a.verified_by}`].filter(Boolean).join(" · "));
    const v = a.verification;
    if (v && v.outcome === "not_yet") add("now", "Verifying recovery against live metrics", null, v.detail);
    if (v && v.outcome === "recovered") add("done", "Recovery verified", null, v.detail);
    if (v && v.outcome === "not_recovered") add("bad", "Not recovered", null, v.detail);
  }
  if (inc && inc.status === "resolved" && a.status !== "proposed")
    add("done", "Incident resolved", inc.ends_at);
  return `<ol class="steps">${steps.map(s => `<li class="${s.cls}"><b>${esc(s.title)}</b>${
    s.when ? `<span class="when">${clock(s.when)}</span>` : ""}${
    s.detail ? `<span class="d">${esc(s.detail)}</span>` : ""}</li>`).join("")}</ol>`;
}

function actionBlock(a, inc) {
  const src = a.decision_source ? ` <span class="pill">${esc(a.decision_source)}</span>` : "";
  return `<div class="act${a.status === "proposed" ? " waiting" : ""}">
    <div class="act-h"><b>Attempt ${a.attempt}</b>${actionPill({ ...a, outcome: a.verification && a.verification.outcome })}${riskPill(a.risk)}${src}</div>
    <div class="what">${actionSentence(a)}</div>
    ${a.reasoning ? `<div class="why">${esc(a.reasoning)}</div>` : ""}
    ${a.expected_effect ? `<div class="small">Expected effect: ${esc(a.expected_effect)}</div>` : ""}
    ${a.reversal ? `<div class="small">Reversal: ${esc(a.reversal)}</div>` : ""}
    ${(a.before_state && Object.keys(a.before_state).length) ? `<div class="small">Before: <span class="mono">${esc(JSON.stringify(a.before_state))}</span>${
      a.after_state ? ` · After: <span class="mono">${esc(JSON.stringify(a.after_state))}</span>` : ""}</div>` : ""}
    ${progress(a, inc)}
  </div>`;
}

// ---------------------------------------------------------------- findings, per agent

function list(items, render) {
  return items && items.length ? `<ul class="f">${items.map(render).join("")}</ul>` : "";
}

function agentBody(f) {
  if (f.status === "skipped") return `<div class="small" style="color:var(--amber)">Not run: ${esc(f.skip_reason || "no reason recorded")}</div>`;
  if (f.status === "error") return `<div class="small" style="color:var(--red)">Failed: ${esc(f.error || "unknown error")}</div>`;
  if (f.status === "running" || f.status === "pending") return `<div class="small" style="color:var(--amber)">${f.status === "running" ? "Working on it now…" : "Queued"}</div>`;
  const o = f.output || {};
  switch (f.agent) {
    case "triage":
      return `${o.summary ? `<p style="margin:6px 0 0">${esc(o.summary)}</p>` : ""}
        <dl class="kv">${o.likely_cause_category ? `<dt>Category</dt><dd>${esc(o.likely_cause_category)}</dd>` : ""}
          ${o.affected_service ? `<dt>Service</dt><dd>${esc(o.affected_service)}</dd>` : ""}
          ${o.confidence ? `<dt>Confidence</dt><dd>${esc(o.confidence)}</dd>` : ""}</dl>
        ${o.observed_symptoms ? `<div class="round-h">Observed</div>${list(o.observed_symptoms, s => `<li>${esc(s)}</li>`)}` : ""}
        ${o.suggested_next_investigation ? `<div class="round-h">Suggested next</div>${list(o.suggested_next_investigation, s => `<li>${esc(s)}</li>`)}` : ""}`;
    case "metrics":
      return `<div class="t3" style="margin-top:6px">${o.assessment ? `<span class="pill ${o.assessment === "critical" ? "red" : o.assessment === "degraded" ? "amber" : "green"}">${esc(o.assessment)}</span>` : ""}${confPill(o.confidence)}</div>
        ${list(o.findings, x => `<li class="${x.abnormal ? "key" : ""}">${esc(x.statement)}${x.measurement ? `<span class="q">${esc(x.measurement)}</span>` : ""}</li>`)}`;
    case "log":
      return `<div class="t3" style="margin-top:6px">${o.logs_available === false ? `<span class="pill amber">no logs</span>` : ""}${confPill(o.confidence)}</div>
        ${list(o.findings, x => `<li class="${x.related_to_alert ? "note" : ""}">${esc(x.statement)}${x.quoted_line && x.quoted_line !== "none" ? `<span class="q">${esc(x.quoted_line)}</span>` : ""}</li>`)}`;
    case "change":
      return `<div class="t3" style="margin-top:6px">${o.changes_found ? `<span class="pill amber">changes found</span>` : `<span class="pill">no changes</span>`}${confPill(o.confidence)}</div>
        ${list(o.findings, x => `<li class="${x.likely_cause ? "key" : ""}">${esc(x.statement)}${x.likely_cause ? ` <span class="pill red">likely cause</span>` : ""}${x.detail ? `<span class="q">${esc(x.detail)}</span>` : ""}</li>`)}`;
    case "rca":
      return `${o.summary ? `<p style="margin:6px 0 0">${esc(o.summary)}</p>` : ""}<div class="t3" style="margin-top:6px">${confPill(o.confidence)}</div>${hypotheses(o)}`;
    case "critic":
      return `<div class="t3" style="margin-top:6px"><span class="pill ${o.verdict === "accept" ? "green" : "amber"}">${esc(o.verdict || "—")}</span></div>
        ${o.reasoning ? `<p style="margin:6px 0 0">${esc(o.reasoning)}</p>` : ""}
        ${o.gaps && o.gaps.length ? `<div class="round-h">What was missing</div>${list(o.gaps, g => `<li class="note">${esc(g.missing)} <span class="faint">→ ${esc(g.agent)}</span></li>`)}` : ""}
        ${o.alternative_overlooked ? `<div class="small">Overlooked alternative: ${esc(o.alternative_overlooked)}</div>` : ""}`;
    case "remediation":
      return `<div class="what" style="margin-top:6px">${o.action === "none" ? "No safe action applies" : actionSentence({ ...o, params: { flag: o.flag } })}</div>
        ${o.reasoning ? `<div class="small">${esc(o.reasoning)}</div>` : ""}`;
    case "postmortem":
      return o.title ? `<p style="margin:6px 0 0"><b>${esc(o.title)}</b></p>${o.summary ? `<p class="muted" style="margin:4px 0 0">${esc(o.summary)}</p>` : ""}` : "";
    default:
      return Object.keys(o).length ? `<pre class="ev">${esc(JSON.stringify(o, null, 2))}</pre>` : "";
  }
}

function hypotheses(o) {
  const hs = o.hypotheses || [];
  if (!hs.length) return "";
  return `<ol class="hyp">${hs.map(h => `<li><b>${esc(h.statement)}</b>
    <div class="t3" style="margin-top:4px">${confPill(h.confidence)}${h.actionable ? `<span class="pill cyan">actionable</span>` : ""}
      ${(h.from_agents || []).map(a => `<span class="pill">${esc(a)}</span>`).join("")}</div>
    ${list(h.supporting_evidence, e => `<li><span class="q" style="margin:0">${esc(e)}</span></li>`)}</li>`).join("")}</ol>`;
}

function agentRow(f) {
  const meta = [f.runtime_s != null ? `${Number(f.runtime_s).toFixed(1)}s` : "", f.attempts > 1 ? `${f.attempts} attempts` : ""].filter(Boolean).join(" · ");
  return `<div class="agent"><div class="agent-h"><b>${esc(AGENT[f.agent] || f.agent)}</b>
    ${f.status !== "done" ? `<span class="pill ${f.status === "error" ? "red" : f.status === "skipped" ? "" : "amber"}">${esc(f.status)}</span>` : ""}
    ${meta ? `<span class="meta">${esc(meta)}</span>` : ""}</div>${agentBody(f)}</div>`;
}

// ---------------------------------------------------------------- measurements

const METRICS = [
  ["error_ratio", "Error ratio", v => `${(v * 100).toFixed(1)}%`, v => v > 0.05],
  ["error_rate_rps", "Errors", v => `${v.toFixed(3)}/s`, v => v > 0.01],
  ["request_rate_rps", "Requests", v => `${v.toFixed(3)}/s`],
  ["latency_p95_ms", "p95 latency", v => `${v.toFixed(1)} ms`, v => v > 500],
  ["cpu_percent_of_core", "CPU", v => `${v.toFixed(1)}%`, v => v > 100],
  ["memory_bytes", "Memory", v => `${(v / 1048576).toFixed(0)} MiB`],
  ["container_seconds_since_last_report", "Last report", v => `${v.toFixed(0)} s ago`, v => v > 45],
];

function measurements(ev) {
  const vals = (ev && ev.values) || {};
  const cards = METRICS.filter(([k]) => vals[k] != null).map(([k, label, fmt, hot]) => {
    const base = vals[`${k}_baseline`];
    const extra = k === "memory_bytes" && vals.memory_limit_bytes ? `of ${(vals.memory_limit_bytes / 1048576).toFixed(0)} MiB limit`
      : base != null ? `baseline ${fmt(base)}` : "no baseline recorded";
    return `<div class="metric${hot && hot(vals[k]) ? " hot" : ""}"><div class="k">${esc(label)}</div>
      <div class="v">${esc(fmt(vals[k]))}</div><div class="b">${esc(extra)}</div></div>`;
  });
  const ops = (ev && ev.failing_operations) || [];
  if (!cards.length && !ops.length) return `<p class="note">${unavailable("No measurements were recorded for this incident.")}</p>`;
  return `${ev.collected_at ? `<p class="note" style="margin-top:10px">Collected ${clock(ev.collected_at)} from Prometheus</p>` : ""}
    <div class="metrics">${cards.join("")}</div>
    ${ops.length ? `<div class="round-h">Failing operations</div>${list(ops, o => `<li class="key"><span class="mono">${esc(o.operation)}</span> · ${(o.error_ratio * 100).toFixed(1)}% failing</li>`)}` : ""}`;
}

// ---------------------------------------------------------------- the view

const openState = new Map();      // incident id -> Set of open section ids (survives re-renders)

function box(id, title, sub, body, inc, defaultOpen = false) {
  const open = openState.get(inc.id);
  const isOpen = open ? open.has(id) : defaultOpen;
  return `<details class="box" data-sec="${id}"${isOpen ? " open" : ""}>
    <summary><span class="st">${esc(title)}</span>${sub ? `<span class="sub">${sub}</span>` : ""}</summary>
    <div class="body">${body}</div></details>`;
}

function byRound(findings) {
  // Latest attempt of each (agent, round); rounds as the outer grouping, in pipeline order.
  const order = ["triage", "metrics", "log", "change", "rca", "critic"];
  const rounds = new Map();
  for (const f of findings) {
    if (!order.includes(f.agent)) continue;
    const r = f.agent === "triage" ? 1 : (f.round || 1);
    if (!rounds.has(r)) rounds.set(r, []);
    rounds.get(r).push(f);
  }
  return [...rounds.entries()].sort((a, b) => a[0] - b[0])
    .map(([r, fs]) => [r, fs.sort((a, b) => order.indexOf(a.agent) - order.indexOf(b.agent))]);
}

export const incidentView = {
  title: "Incident",
  async render(ctx) {
    const id = Number(ctx.params.id);
    this.id = id; this.ctx = ctx;
    if (!this.data || this.data.id !== id) ctx.el.innerHTML = loading(8);
    await this.load();
  },
  async load() {
    const ctx = this.ctx, id = this.id;
    try {
      const res = await getJSON(`/incidents/${id}?include_events=false`, `incident:${id}`);
      if (this.id !== id) return;
      this.data = res.data;
      ctx.app.setStale(res.stale ? res.at : null);
      this.paint();
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) ctx.el.innerHTML = failed("No such incident", `Incident #${id} does not exist on this backend.`);
      else if (e instanceof HttpError && e.status === 401) ctx.app.needSignIn();
      else ctx.el.innerHTML = failed("Could not load this incident", e.message);
    }
  },
  paint() {
    const inc = this.data, ctx = this.ctx;
    ctx.app.setTitle(`#${inc.id} ${inc.service || ""}`);
    const findings = inc.findings || [];
    const actions = inc.actions || [];
    const latest = agent => findings.filter(f => f.agent === agent).sort((a, b) => (b.round || 1) - (a.round || 1))[0];
    const conclusion = latest("conclusion"), rca = latest("rca"), pmRow = latest("postmortem");
    const pending = actions.filter(a => a.status === "proposed");
    const c = (conclusion && conclusion.output) || null;
    const investigating = findings.some(f => f.status === "running" || f.status === "pending");

    const conclusionHtml = c
      ? `<div class="conc ${esc(c.outcome)}"><div class="k">${c.outcome === "accepted" ? "Root cause · accepted by the Critic"
          : c.outcome === "insufficient" ? "Best hypothesis · not confirmed" : "Reasoning did not complete"}</div>
          <div class="t">${esc(c.statement)}</div>
          <div class="small faint" style="margin-top:6px">${c.rounds_run || 1} round${(c.rounds_run || 1) > 1 ? "s" : ""}${c.reasoning_s ? ` · ${Number(c.reasoning_s).toFixed(1)}s of reasoning` : ""}</div>
          ${rca && rca.output ? hypotheses(rca.output) : ""}</div>`
      : `<div class="conc"><div class="k">Root cause</div><div class="t" style="font-size:14px;font-weight:400;color:var(--ink-2)">${
          investigating ? "The investigation is still running." : "No conclusion was recorded for this incident."}</div></div>`;

    const prior = rca && rca.output && rca.output.prior_cases;
    const rounds = byRound(findings);
    const investigation = rounds.map(([r, fs]) =>
      `${rounds.length > 1 || r > 1 ? `<div class="round-h">Round ${r}</div>` : ""}${fs.map(agentRow).join("")}`).join("")
      + findings.filter(f => f.agent === "remediation").map(agentRow).join("")
      || `<p class="note">${unavailable("No agent findings were recorded.")}</p>`;

    const evidenceBlocks = findings.filter(f => f.evidence_text).map(f =>
      `<div class="round-h">${esc(AGENT[f.agent] || f.agent)}${(f.round || 1) > 1 ? ` · round ${f.round}` : ""}</div><pre class="ev">${esc(f.evidence_text)}</pre>`).join("");

    const labels = Object.entries(inc.labels || {});
    ctx.el.innerHTML = `
      <section class="hero sec">
        <div class="eyebrow">${statusPill(inc)}${sevPill(inc.severity)}${investigating ? `<span class="pill blue">investigating</span>` : ""}
          ${pending.length ? `<span class="pill amber">approval needed</span>` : ""}<span class="faint" style="font-size:12px">#${inc.id}</span></div>
        <h2>${esc(inc.service || "unknown service")}</h2>
        <div class="alert">${esc(inc.alertname)}</div>
        ${inc.description || inc.summary ? `<p>${esc(inc.description || inc.summary)}</p>` : ""}
        <dl class="facts">
          <div><dt>Detected</dt><dd>${clock(inc.starts_at)}</dd></div>
          <div><dt>${inc.status === "resolved" ? "Resolved" : "Active for"}</dt><dd>${inc.status === "resolved" ? `${clock(inc.ends_at)} · after ${duration(inc.starts_at, inc.ends_at)}` : duration(inc.starts_at)}</dd></div>
          <div><dt>Last updated</dt><dd>${ago(inc.updated_at)}</dd></div>
          <div><dt>Triage</dt><dd>${esc(inc.agent_status || "—")}${inc.agent_runtime_s ? ` · ${Number(inc.agent_runtime_s).toFixed(1)}s` : ""}</dd></div>
        </dl>
      </section>
      ${pending.length ? `<section class="sec"><h3 class="sh">Needs your decision</h3>${pending.map(a => approvalCard(a, inc)).join("")}</section>` : ""}
      <section class="sec"><h3 class="sh">Diagnosis</h3>${conclusionHtml}</section>
      <section class="sec">
        ${box("remediation", "Remediation & progress", actions.length ? `${actions.length} action${actions.length > 1 ? "s" : ""}` : "none",
              actions.length ? actions.map(a => actionBlock(a, inc)).join("") : `<p class="note">${unavailable("No remediation was proposed for this incident.")}</p>`,
              inc, actions.length > 0)}
        ${box("measurements", "Measured at the time", "", measurements(inc.evidence), inc)}
        ${box("investigation", "Investigation", `${rounds.length || 0} round${rounds.length === 1 ? "" : "s"}`, investigation, inc)}
        ${prior && prior.length ? box("memory", "Similar past incidents", `${prior.length}`,
            list(prior, p => `<li><a href="#/incident/${p.incident_id}">#${p.incident_id}</a> ${esc(p.alertname)} on ${esc(p.service)} · match ${(p.score * 100).toFixed(0)}%<span class="q">${esc(p.cause || "")}</span></li>`), inc) : ""}
        ${box("evidence", "Raw evidence", "", evidenceBlocks || `<p class="note">${unavailable("No raw evidence was recorded.")}</p>`, inc)}
        ${box("timeline", "Execution trace", `${inc.event_count ?? 0} events`, `<div data-lazy="timeline">${loading(3)}</div>`, inc)}
        ${box("postmortem", "Postmortem", pmRow ? esc(pmRow.status) : "not started", `<div data-lazy="postmortem">${loading(3)}</div>`, inc)}
        ${box("details", "Alert details", "", `<dl class="kv">
            <dt>Alert</dt><dd>${esc(inc.alertname)}</dd><dt>Severity</dt><dd>${esc(inc.severity || "—")}</dd>
            ${labels.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}
            <dt>Fingerprint</dt><dd class="mono">${esc(inc.fingerprint)}</dd>
            <dt>Received</dt><dd>${clock(inc.first_received_at)}</dd>
            ${inc.agent_model ? `<dt>Model</dt><dd>${esc(inc.agent_model)}</dd>` : ""}
            ${inc.trace_id ? `<dt>Trace id</dt><dd class="mono">${esc(inc.trace_id)}</dd>` : ""}
          </dl>`, inc)}
      </section>`;

    wireDecisions(ctx.el, aid => {
      const a = actions.find(x => x.id === aid);
      return a ? { action: a, incident: inc } : null;
    }, ctx.app);
    ctx.el.querySelectorAll("details.box").forEach(d => {
      d.addEventListener("toggle", () => {
        if (!openState.has(inc.id)) openState.set(inc.id, new Set(
          [...ctx.el.querySelectorAll("details.box[open]")].map(x => x.dataset.sec)));
        const s = openState.get(inc.id);
        d.open ? s.add(d.dataset.sec) : s.delete(d.dataset.sec);
        if (d.open) this.fillLazy(d);
      });
      if (d.open) this.fillLazy(d);
    });
  },
  /* The trace and the report are fetched only when their section is open. Their last result is
     shown straight away on a repaint and refreshed at most every few seconds, so a live incident's
     page does not re-download its whole trace on every update. */
  lazy: new Map(),
  async fillLazy(d, force = false) {
    const slot = d.querySelector("[data-lazy]");
    if (!slot) return;
    const id = this.id, kind = slot.dataset.lazy, key = `${id}:${kind}`;
    const cached = this.lazy.get(key);
    if (cached) { slot.innerHTML = cached.html; this.wireLazy(slot, cached); }
    if (!force && cached && Date.now() - cached.at < (kind === "timeline" ? 5000 : 10000)) return;
    let entry;
    try {
      if (kind === "timeline") {
        const { data } = await getJSON(`/incidents/${id}/events?limit=2000`);
        entry = { at: Date.now(), html: timeline(data), data };
      } else {
        const { data } = await getJSON(`/incidents/${id}/postmortem`);
        entry = { at: Date.now(), html: (data.ready ? "" : `<p class="note">The report is written once the response settles (status: ${esc(data.status)}${data.error ? `, ${esc(data.error)}` : ""}). Below is what is already known.</p>`)
          + md(data.markdown)
          + `<div class="btns"><a class="btn small" href="/incidents/${id}/postmortem.pdf" download>Download PDF</a>
             <a class="btn small quiet" href="/incidents/${id}/postmortem.md" download>Markdown</a></div>` };
      }
    } catch (e) {
      if (!cached) slot.innerHTML = `<p class="note">${esc(e.message)}</p>`;
      return;
    }
    if (this.id !== id) return;
    this.lazy.set(key, entry);
    const live = this.ctx.el.querySelector(`details.box[data-sec="${kind}"] [data-lazy]`);
    if (live) { live.innerHTML = entry.html; this.wireLazy(live, entry); }
  },
  wireLazy(slot, entry) {
    const t = slot.querySelector("[data-all]");
    if (t && entry.data) t.onclick = () => { slot.innerHTML = timeline(entry.data, true); };
  },
  onChange(msg) {
    if (msg.incidents && msg.incidents.includes(this.id)) (this._soon ||= coalesce(() => this.load(), 1200))();
  },
  onResync() { this.load(); },
};

const FLOW = new Set(["investigation_started", "plan_decided", "agent_started", "agent_completed",
  "investigation_finished", "action_proposed", "action_approved", "action_rejected", "action_executed",
  "action_refused", "action_failed", "verification_started", "verification_recovered",
  "verification_not_recovered", "remediation_exhausted", "postmortem_written", "postmortem_failed",
  "memory_recalled", "memory_stored", "retry_started", "retry_failed", "output_validation_failed",
  "injection_neutralised", "a2a_request_sent"]);

function timeline(events, all = false) {
  if (!events || !events.length) return `<p class="note">${unavailable("No events were recorded.")}</p>`;
  const shown = all ? events : events.filter(e => FLOW.has(e.kind));
  const describe = e => {
    const d = e.detail || {};
    const bits = [d.approved_by && `by ${d.approved_by}`, d.rejected_by && `by ${d.rejected_by}`, d.action && `${d.action}`,
                  d.target && `on ${d.target}`, d.result, d.reason, d.error, d.detail, d.title,
                  e.kind === "plan_decided" && d.run && `run: ${d.run.join(", ")}`].filter(Boolean);
    return bits.join(" · ").slice(0, 240);
  };
  return `<ul class="tl">${shown.map(e => `<li><span class="tt">${clock(e.at).replace(/^.*?(\d\d:\d\d:\d\d).*$/, "$1")}</span>
      <div><span class="tk${e.ok === false ? " bad" : e.kind.startsWith("model_") ? " model" : ""}">${esc(e.kind.replace(/_/g, " "))}</span>
      ${e.agent ? `<span class="faint"> · ${esc(e.agent)}${e.round > 1 ? ` r${e.round}` : ""}</span>` : ""}
      ${e.duration_ms ? `<span class="faint"> · ${(e.duration_ms / 1000).toFixed(1)}s</span>` : ""}
      ${describe(e) ? `<div class="td">${esc(describe(e))}</div>` : ""}</div></li>`).join("")}</ul>
    ${!all && shown.length < events.length ? `<button class="more" data-all>Show all ${events.length} events (model and tool calls too)</button>` : ""}`;
}
