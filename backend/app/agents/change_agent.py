"""Change Agent: works out what changed around the incident.

Evidence comes from change_tool.py: feature flags flagd is currently serving, container restart
history and image versions. In this environment a feature flag toggle is what actually causes every
injected fault, so an active flag affecting the alerting service is usually the real cause - the
analogue of a config change in production.

The agent must distinguish a flag that affects the alerting service from one that affects a
different service, and must not claim a change simply because the incident exists.
"""

from typing import Literal

from pydantic import BaseModel, Field

from . import base
from .. import change_tool

AGENT_NAME = "change"


class ChangeFinding(BaseModel):
    statement: str = Field(min_length=5, description="what changed, in one short sentence")
    detail: str = Field(min_length=1, description="the exact evidence line this is based on")
    likely_cause: bool = Field(description="true only if this change plausibly explains THIS incident")


class ChangeResult(BaseModel):
    changes_found: bool
    findings: list[ChangeFinding] = Field(min_length=1, max_length=3)
    confidence: Literal["low", "medium", "high"]


SCHEMA_HINT = base.schema_hint({
    "changes_found": "true | false",
    "findings": [{
        "statement": "short sentence describing the change",
        "detail": "the exact line from the evidence this is based on",
        "likely_cause": "true | false",
    }],
    "confidence": "low | medium | high",
})

RULES = f"""Rules, in priority order:

1. FEATURE FLAGS FIRST. Look at the feature flag section of the evidence before anything else.
   Each flag line states its relationship to the alerting service. Use that, do not reason about it:
       "which is this service"              -> changes_found true, likely_cause true
       "which is a service this one calls"  -> changes_found true, likely_cause true
                                               (its failure propagates into this service)
       "which is an unrelated service"      -> report it as a finding with likely_cause false;
                                               on its own it does not make changes_found true
   - Whenever a flag affects this service or one it calls, your FIRST finding MUST be about that
     flag, and you must never answer "no changes detected".
   - Only if the evidence says "every flag is currently off" may you move on to the container lines.

2. CONTAINER STATE SECOND, and only when no flag is set. A container restarted shortly before the
   incident, or with a restart count above 0, is a change worth reporting.

3. If every flag is off AND the container was not recently restarted AND its restart count is 0,
   then set changes_found false with one finding saying no change was detected. Do not invent a
   deployment, release or code change: none happen in this environment.

{base.NUMBER_RULES}
- "detail" must be copied from the change evidence above. Never describe a change that is not listed.
- Name the service a flag affects exactly as the evidence states it. If the line says a flag affects
  "payment", do not write that it is set for the alerting service when that service is something
  else; say the flag is set on payment, which this service depends on.
- Keep every statement under 20 words. Do not suggest fixes."""


def collect(service: str | None, starts_at: str) -> dict:
    """Gather this agent's evidence. Deterministic, no LLM, safe to run in parallel."""
    return change_tool.collect(service, starts_at)


def _agent():
    return base.make_agent(
        role="Change Investigator",
        goal="Identify what configuration or deployment change could explain an incident, or state that nothing changed.",
        backstory=(
            "You are AIRA's change specialist. You examine feature flags, container restarts and "
            "image versions, and you report only changes that are actually recorded in the evidence."
        ),
    )


def _consistency_check(ev: dict):
    """Reject output that contradicts the evidence it was given.

    A flag that is not "off" is a recorded fact, so answering changes_found=false while one is
    listed is simply wrong. Observed on 2026-10-06: the agent reported "no changes detected" for
    payment while paymentFailure was set to 100%. Checking it here forces the retry instead of
    hoping stronger wording lands.
    """
    # Only flags touching this service or something it calls make changes_found true. A flag on an
    # unrelated service is still reported as a finding, but it is not a change to *this* incident,
    # and forcing changes_found true for it would make every incident look caused by configuration.
    relevant = [f for f in (ev.get("active_flags") or [])
                if f.get("relation") in ("this service", "a service this one calls")]

    def check(result: ChangeResult):
        if not relevant:
            return
        names = ", ".join(f["flag"] for f in relevant)
        if not result.changes_found:
            raise ValueError(
                f"changes_found was false, but these feature flags are not off and affect this "
                f"service or one it calls: {names}. A flag that is not off is a configuration "
                "change, so changes_found must be true and the first finding must describe it."
            )
        # A relevant flag must actually appear in the findings. Observed on 2026-10-07: with both a
        # set flag and a container that had restarted 13 minutes earlier, the agent reported only
        # the restart and dropped the flag entirely - the likelier cause, silently omitted.
        text = " ".join(f"{f.statement} {f.detail}" for f in result.findings).lower()
        missing = [f["flag"] for f in relevant if f["flag"].lower() not in text]
        if missing:
            raise ValueError(
                f"these feature flags affect this service or one it calls but are not mentioned in "
                f"any finding: {', '.join(missing)}. A flag that is not off is the most likely "
                "cause, so it must be your FIRST finding with likely_cause true. Report the "
                "container restart as a separate, additional finding if it is also relevant."
            )

    return check


def run(inc: dict, evidence: dict | None = None, focus: str | None = None) -> tuple[ChangeResult, int, float]:
    """Interpret change evidence for this incident. Returns (result, attempts, seconds)."""
    ev = evidence if evidence is not None else collect(inc.get("service"), inc["starts_at"])
    context = base.incident_block(inc, evidence_text=ev.get("text"),
                                  evidence_label="EVIDENCE (configuration and deployment state)")
    prompt = (
        "Determine what changed around this incident.\n\n"
        + context + "\n\n" + RULES
        + base.focus_block(focus)
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, ChangeResult, context,
                          check=_consistency_check(ev))
