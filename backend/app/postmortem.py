"""Assemble and render the post-incident report.

The facts here are read from the database and never generated: the alert, the measurements the
evidence collector recorded, which agents ran and what each concluded, how many rounds it took,
what action was proposed, who approved it, what happened when it ran, and whether the service
actually recovered. `postmortem_agent` writes the narrative around them.

Two outputs, from one structure: Markdown for the download, and the same content rendered in the
console. Having one builder means the file somebody saves and the page somebody reads cannot
disagree, which is the whole point of a document that outlives the incident.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from . import db, memory

log = logging.getLogger("aira.postmortem")

AGENT_NAME = "postmortem"

AGENT_TITLES = {
    "triage": "Triage", "metrics": "Metrics", "log": "Log", "change": "Change",
    "rca": "Root cause", "critic": "Critic", "remediation": "Remediation",
    "conclusion": "Outcome", "postmortem": "Postmortem",
}

ACTION_TITLES = {
    "reset_feature_flag": "Reset a feature flag",
    "restart_container": "Restart a container",
    "none": "No action",
}


# ---------------------------------------------------------------- small formatters

def _clock(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        return iso


def _secs(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _dur(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s:.0f}s"
    m, r = divmod(int(s), 60)
    return f"{m}m {r:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v, unit: str = "", dp: int = 2) -> str:
    return "—" if v is None else f"{v:.{dp}f}{unit}"


def _mib(v) -> str:
    return "—" if v is None else f"{v / 1048576:.0f} MiB"


# ---------------------------------------------------------------- the record

def facts(incident_id: int) -> dict | None:
    """Everything recorded about this incident, in the shape the report needs."""
    inc = db.get(incident_id)
    if not inc:
        return None
    findings = db.get_findings(incident_id)
    actions = db.get_actions(incident_id)

    def latest(agent: str) -> dict | None:
        rows = [f for f in findings if f["agent"] == agent and f["status"] == "done"]
        return max(rows, key=lambda f: f.get("round") or 1) if rows else None

    rca = latest("rca")
    critic = latest("critic")
    conclusion = latest("conclusion")
    hyps = ((rca or {}).get("output") or {}).get("hypotheses") or []
    executed = [a for a in actions if a["status"] == "executed"]

    starts = [f["started_at"] for f in findings if f.get("started_at")]
    ends = [f["finished_at"] for f in findings if f.get("finished_at")]

    return {
        "incident": inc,
        "findings": findings,
        "actions": actions,
        "executed": executed,
        "rounds": len({f.get("round") or 1 for f in findings if f["agent"] != "conclusion"}) or 1,
        "cause": hyps[0]["statement"] if hyps else None,
        "cause_confidence": hyps[0].get("confidence") if hyps else None,
        "alternatives": [h["statement"] for h in hyps[1:]],
        "verdict": ((critic or {}).get("output") or {}).get("verdict"),
        "critic_reasoning": ((critic or {}).get("output") or {}).get("reasoning"),
        "outcome": ((conclusion or {}).get("output") or {}).get("outcome"),
        "detection_s": _secs(inc.get("starts_at"), inc.get("first_received_at")),
        "investigation_s": _secs(min(starts), max(ends)) if starts and ends else None,
        "total_s": _secs(inc.get("starts_at"), inc.get("ends_at")),
        "prior_cases": ((rca or {}).get("output") or {}).get("prior_cases") or [],
    }


def record_for_model(f: dict) -> str:
    """The incident as plain text, for the agent to write about. Facts only, no instructions."""
    inc = f["incident"]
    out = [
        f"ALERT: {inc['alertname']} on {inc.get('service')} (severity {inc.get('severity')})",
        f"WHAT FIRED: {inc.get('summary') or ''}",
        f"DESCRIPTION: {inc.get('description') or ''}",
        f"INVESTIGATION ROUNDS: {f['rounds']}",
    ]
    out.append("\nWHAT EACH AGENT CONCLUDED:")
    for row in f["findings"]:
        if row["agent"] in ("conclusion", "postmortem"):
            continue
        name = AGENT_TITLES.get(row["agent"], row["agent"])
        if row["status"] == "skipped":
            out.append(f"- {name}: did not run - {row.get('skip_reason') or 'no reason recorded'}")
            continue
        if row["status"] != "done":
            out.append(f"- {name}: {row['status']}")
            continue
        o = row.get("output") or {}
        if o.get("summary"):
            out.append(f"- {name} (round {row.get('round') or 1}): {o['summary']}")
        for item in (o.get("findings") or [])[:4]:
            flag = " [FLAGGED AS THE LIKELY CAUSE]" if item.get("likely_cause") else ""
            out.append(f"    * {item.get('statement')}{flag}")
        if row["agent"] == "critic":
            out.append(f"    * verdict: {o.get('verdict')} - {o.get('reasoning', '')}")

    if f["cause"]:
        out.append(f"\nROOT CAUSE REACHED: {f['cause']} (confidence {f['cause_confidence']})")
        out.append(f"REVIEWER VERDICT: {f['verdict'] or 'not reviewed'}")
    else:
        out.append("\nROOT CAUSE: none was established.")

    out.append("\nWHAT WAS DONE:")
    if not f["actions"]:
        out.append("- nothing was proposed")
    for a in f["actions"]:
        title = ACTION_TITLES.get(a["action"], a["action"])
        out.append(f"- {title} on {a.get('target_service')}: {a['status']}"
                   f"{' (approved by ' + a['decided_by'] + ')' if a.get('decided_by') else ''}")
        if a.get("reasoning"):
            out.append(f"    reason: {a['reasoning']}")
        v = a.get("verification") or {}
        if v:
            out.append(f"    verification: {v.get('outcome')} - {v.get('detail', '')}")
    if f["prior_cases"]:
        out.append(f"\nSIMILAR PAST INCIDENTS RETRIEVED FROM MEMORY: "
                   f"{len(f['prior_cases'])} (used as context only)")
    return "\n".join(out)


# ---------------------------------------------------------------- rendering

def markdown(incident_id: int) -> str | None:
    """The full report. Narrative from the agent, every fact from the record."""
    f = facts(incident_id)
    if not f:
        return None
    inc = f["incident"]
    row = next((x for x in f["findings"] if x["agent"] == AGENT_NAME and x["status"] == "done"), None)
    n = (row.get("output") or {}) if row else {}

    # Figures in the narrative are checked against the record rather than removed outright.
    #
    # Deleting them all was the first attempt and it produced unreadable prose: "of calls failed
    # over the last , impacting a significant portion of users". Worse, the figure it had deleted
    # was correct - the model had quoted "66.67% of calls failed over the last 3 minutes" straight
    # out of the alert description. The risk was never that the model writes numbers; it is that
    # it writes numbers nobody recorded. So a figure that appears in the source record is left
    # alone, and one that does not is dropped. That is a guarantee about fabrication, which is the
    # thing that actually matters in a document people trust after the fact.
    source = record_for_model(f)

    def narrate(text: str) -> str:
        text = text or ""
        kept = []
        for token in re.findall(r"\d+(?:[.,]\d+)?", text):
            if token in source:
                kept.append(token)
        if len(kept) == len(re.findall(r"\d+(?:[.,]\d+)?", text)):
            return text.strip()
        # At least one figure is not in the record: strip every figure rather than try to guess
        # which sentence is the dishonest one.
        return memory.scrub_magnitudes(text)

    title = narrate(n.get("title") or "") or f"{inc['alertname']} on {inc.get('service')}"
    md: list[str] = [
        f"# Postmortem — {title}",
        "",
        f"**Incident** #{inc['id']} · **Service** {inc.get('service')} · "
        f"**Alert** {inc['alertname']} · **Severity** {inc.get('severity')}",
        f"**Detected** {_clock(inc.get('starts_at'))} · "
        f"**Status** {inc.get('status')}"
        + (f" · **Resolved** {_clock(inc.get('ends_at'))}" if inc.get("ends_at") else ""),
        "",
        "> Written by AIRA. The narrative sections were generated by a language model; every",
        "> figure, timestamp and agent finding below is read from the recorded investigation.",
        "",
        "## Summary",
        "",
        narrate(n.get("summary")) or "_No narrative was generated for this incident._",
        "",
        "## Customer impact",
        "",
        narrate(n.get("customer_impact")) or "_Not assessed._",
        "",
        "## Timeline",
        "",
        "| Stage | When | Duration |",
        "| --- | --- | --- |",
        f"| Alert started firing | {_clock(inc.get('starts_at'))} | — |",
        f"| AIRA received the alert | {_clock(inc.get('first_received_at'))} | {_dur(f['detection_s'])} |",
    ]
    for stage in f["findings"]:
        if stage["status"] != "done" or stage["agent"] in ("conclusion", "postmortem"):
            continue
        md.append(f"| {AGENT_TITLES.get(stage['agent'], stage['agent'])} agent finished"
                  f"{' (round ' + str(stage['round']) + ')' if (stage.get('round') or 1) > 1 else ''}"
                  f" | {_clock(stage.get('finished_at'))} | {_dur(stage.get('runtime_s'))} |")
    for a in f["actions"]:
        if a.get("decided_at"):
            md.append(f"| {ACTION_TITLES.get(a['action'], a['action'])} "
                      f"{'approved' if a['status'] != 'rejected' else 'rejected'} by "
                      f"{a.get('decided_by') or 'unknown'} | {_clock(a.get('decided_at'))} | — |")
        if a.get("executed_at"):
            md.append(f"| Action executed | {_clock(a['executed_at'])} | {_dur(a.get('duration_s'))} |")
    if inc.get("ends_at"):
        md.append(f"| Alert stopped firing | {_clock(inc['ends_at'])} | {_dur(f['total_s'])} |")

    # Measurements, straight from the evidence the agents were given.
    v = (inc.get("evidence") or {}).get("values") or {}
    if v:
        md += ["", "## What was measured", "",
               "| Signal | At the time | Baseline |", "| --- | --- | --- |"]
        for label, now_k, base_k, fmt in (
                ("Error ratio", "error_ratio", "error_ratio_baseline", _pct),
                ("Requests/s", "request_rate_rps", "request_rate_rps_baseline",
                 lambda x: _num(x, "/s", 3)),
                ("p95 latency", "latency_p95_ms", "latency_p95_ms_baseline",
                 lambda x: _num(x, " ms", 1)),
                ("CPU", "cpu_percent_of_core", "cpu_percent_of_core_baseline",
                 lambda x: _num(x, "% of a core", 1)),
                ("Memory", "memory_bytes", "memory_bytes_baseline", _mib)):
            if v.get(now_k) is not None:
                md.append(f"| {label} | {fmt(v.get(now_k))} | {fmt(v.get(base_k))} |")

    md += ["", "## Root cause", ""]
    if f["cause"]:
        md.append(f"**{f['cause']}**")
        md.append("")
        md.append(f"Stated with {f['cause_confidence'] or 'unstated'} confidence by the Root cause "
                  f"agent after {f['rounds']} investigation round"
                  f"{'s' if f['rounds'] > 1 else ''}. "
                  + {"accept": "The Critic accepted it.",
                     "insufficient": "**The Critic did not accept it** — treat this as unconfirmed.",
                     }.get(f["verdict"], "It was not reviewed."))
        if f["alternatives"]:
            md += ["", "Alternatives that were considered and ranked lower:", ""]
            md += [f"- {a}" for a in f["alternatives"]]
    else:
        md.append("_No root cause was established. The evidence described the symptoms but nothing "
                  "was found that explained them._")

    md += ["", "## What was done", ""]
    if not f["actions"]:
        md.append("_No remediation was proposed._")
    for a in f["actions"]:
        md.append(f"### {ACTION_TITLES.get(a['action'], a['action'])}"
                  + (f" on `{a['target_service']}`" if a.get("target_service") else ""))
        md.append("")
        md.append(f"- **Status** {a['status']}"
                  + (f" · **Risk** {a['risk']}" if a.get("risk") else ""))
        if a.get("reasoning"):
            md.append(f"- **Why** {a['reasoning']}")
        if a.get("decided_by"):
            md.append(f"- **Decided by** {a['decided_by']} at {_clock(a.get('decided_at'))}")
        if a.get("result_detail"):
            md.append(f"- **Result** {a['result_detail']}")
        ver = a.get("verification") or {}
        if ver:
            md.append(f"- **Verification** {ver.get('outcome')} — {ver.get('detail', '')}")
        if a.get("reversal"):
            md.append(f"- **Reversal** {a['reversal']}")
        md.append("")

    if n.get("what_went_well"):
        md += ["## What went well", ""] + [f"- {narrate(x)}" for x in n["what_went_well"]] + [""]
    if n.get("what_went_badly"):
        md += ["## What went badly", ""] + [f"- {narrate(x)}" for x in n["what_went_badly"]] + [""]
    if n.get("follow_ups"):
        md += ["## Follow-up actions", "", "| Priority | Action | Why |", "| --- | --- | --- |"]
        for x in n["follow_ups"]:
            md.append(f"| {x.get('priority', '—')} | {narrate(x.get('action'))} | {narrate(x.get('why'))} |")
        md.append("")

    if f["prior_cases"]:
        md += ["## Prior incidents AIRA recalled", "",
               "Retrieved from incident memory as context for the Root cause agent. They were a "
               "hint about where to look, not evidence about this incident.", "",
               "| Incident | Similarity | Cause recorded then |", "| --- | --- | --- |"]
        for c in f["prior_cases"]:
            md.append(f"| #{c.get('incident_id')} | {c.get('score')} | {c.get('cause', '')} |")
        md.append("")

    md += ["---", "",
           f"_Generated by AIRA from the recorded investigation of incident #{inc['id']}._"]
    return "\n".join(md)
