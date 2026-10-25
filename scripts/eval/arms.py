"""The four arms the project synopsis commits to comparing, as runnable configurations.

    Baseline 1          a fixed rule-based workflow over the same evidence, no LLM at all
    Baseline 2          one LLM call given all the evidence at once
    AIRA without Critic the full pipeline with review and replanning switched off
    Full AIRA           everything, including incident memory

Every arm takes the same input - one incident's stored evidence and investigator findings - and
returns the same shape, so the comparison is between the reasoning and nothing else. An arm that
got different evidence from another would not be a baseline, it would be a different experiment.

Baseline 1 is deliberately a *good* rule-based system rather than a straw man. It checks the
feature flags and the container state directly, which is exactly what a competent engineer would
automate first, and on these five scenarios it is expected to do well - every injected fault is
either a flag or a stopped container, which is precisely the case a lookup table handles. Saying
so in advance is the point: if AIRA cannot beat this, that is the finding, and a baseline built to
lose would have hidden it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ArmResult:
    """What every arm must produce, so they can be scored by one function."""

    arm: str
    incident_id: int
    cause: str = ""                       # the stated root cause
    action: str | None = None             # proposed remediation, or None
    target: str | None = None             # the service it would act on
    params: dict = field(default_factory=dict)
    confidence: str | None = None
    hypotheses: list[str] = field(default_factory=list)
    runtime_s: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- Baseline 1: rules, no model

# The flags this environment can set, and the service each one breaks. Read from the same scenario
# definitions the fault injector uses, so the baseline cannot be given knowledge the injector does
# not have.
def _flag_owner(flag: str, scenarios: dict) -> str | None:
    for s in scenarios.values():
        inj = s.get("injection") or {}
        if inj.get("kind") == "feature_flag" and inj.get("flag") == flag:
            return s.get("faulty_service")
    return None


def baseline_rules(inc: dict, findings: list[dict], scenarios: dict) -> ArmResult:
    """A fixed decision procedure over the evidence. No model, no judgement, no variance.

    Order matters and encodes the priority a runbook would: a changed flag explains a failure and
    a stopped container explains silence, so both are checked before anything is inferred from the
    metrics. Everything after that is a symptom, and the baseline says so rather than guessing -
    an honest "elevated errors, cause unknown" is what a rule engine can actually justify.
    """
    t0 = time.perf_counter()
    r = ArmResult(arm="baseline-rules", incident_id=inc["id"])

    ev = inc.get("evidence") or {}
    values = ev.get("values") or {}

    # 1. A feature flag that is not at its default. The Change agent's evidence carries the flag
    #    state, which is the same source the rules would read directly.
    flag_text = ""
    for f in findings:
        if f.get("agent") == "change":
            flag_text = (f.get("evidence_text") or "") + " " + json.dumps(f.get("evidence") or {})
            break
    hit = re.search(r"([a-zA-Z]+(?:Failure|HighCpu|CacheFailure|Flood[A-Za-z]*))"
                    r"[^\n]{0,60}?(?:=|->|:|\bis\b)\s*['\"]?(on|true|\d+%)", flag_text)
    if hit:
        flag, value = hit.group(1), hit.group(2)
        owner = _flag_owner(flag, scenarios) or inc.get("service")
        r.cause = f"The {flag} feature flag is enabled ({value}) on the {owner} service."
        r.action, r.target, r.params = "reset_feature_flag", owner, {"flag": flag}
        r.confidence = "high"
        r.detail["rule"] = "non-default feature flag found in change evidence"

    # 2. A container that has stopped reporting. Silence is not a symptom to be weighed against
    #    others - it is the whole explanation.
    elif isinstance(values.get("container_seconds_since_last_report"), (int, float)) \
            and values["container_seconds_since_last_report"] > 60:
        svc = inc.get("service")
        r.cause = f"The {svc} container is not running, so it reports no metrics."
        r.action, r.target = "restart_container", svc
        r.confidence = "high"
        r.detail["rule"] = "container stopped reporting for over 60s"

    # 3. Everything below here is a symptom. A rule engine cannot turn one into a cause, and
    #    pretending otherwise is how a baseline gets flattered.
    else:
        moved = []
        for k, base in (("error_ratio", "error_ratio_baseline"),
                        ("cpu_percent_of_core", "cpu_percent_of_core_baseline"),
                        ("latency_p95_ms", "latency_p95_ms_baseline")):
            now, was = values.get(k), values.get(base)
            if now is not None and was is not None and was > 0 and now / was >= 1.25:
                moved.append(k.replace("_", " "))
        r.cause = (f"Elevated {', '.join(moved)} on {inc.get('service')}; no configuration change "
                   "or stopped container found, so the cause is not established."
                   if moved else
                   f"No measurement on {inc.get('service')} deviated from its baseline; "
                   "no cause established.")
        r.confidence = "low"
        r.detail["rule"] = "no decisive signal; reported as symptoms only"

    r.hypotheses = [r.cause]
    r.runtime_s = time.perf_counter() - t0
    return r


# ---------------------------------------------------------------- Baseline 2: one LLM call

SINGLE_LLM_PROMPT = """You are an SRE. An incident has fired and all available evidence is below.
Identify the root cause and the single action that would fix it.

{evidence}

Answer with JSON only, exactly these keys:
{{"root_cause": "one sentence naming what caused this",
  "service": "the service the cause is in",
  "action": one of "reset_feature_flag", "restart_container", "none",
  "target_service": "the service to act on",
  "flag": "the flag name if the action is reset_feature_flag, else null",
  "confidence": "low" | "medium" | "high"}}"""


def _evidence_bundle(inc: dict, findings: list[dict]) -> str:
    """Everything the specialist agents were given, concatenated.

    This is the whole point of the arm: the same raw material, with no orchestration deciding what
    to look at, no agent specialising, and no reviewer checking the answer.
    """
    parts = [f"ALERT: {inc.get('alertname')} on {inc.get('service')} ({inc.get('severity')})",
             f"SUMMARY: {inc.get('summary') or ''}",
             f"DESCRIPTION: {inc.get('description') or ''}"]
    if inc.get("evidence_text"):
        parts.append(f"\nMETRICS:\n{inc['evidence_text']}")
    for f in findings:
        if f.get("evidence_text"):
            parts.append(f"\n{f['agent'].upper()} EVIDENCE:\n{f['evidence_text']}")
    return "\n".join(parts)
