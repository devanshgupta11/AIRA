"""Critic agent — challenges the RCA before anyone acts on it.

This closes the reflection loop. The RCA agent (ADK, reached over A2A) proposes root causes; this
agent, built with CrewAI, reviews them against the evidence the investigators actually gathered and
decides whether the conclusion is earned. If it is not, it says precisely what is missing and which
investigator should go and look - which is what drives a second round.

A critic that accepts everything is decoration, so the prompt and the checks both push against
agreement: a hypothesis resting on one agent's finding, or on symptoms alone, or asserted while an
investigator was never run, is exactly the case this agent exists to catch.
"""

from typing import Literal

from pydantic import BaseModel, Field

from . import base

AGENT_NAME = "critic"
SEQ = 70

INVESTIGATORS = ("metrics", "log", "change")


class Gap(BaseModel):
    missing: str = Field(min_length=5, description="the specific evidence that is absent")
    agent: Literal["metrics", "log", "change"] = Field(
        description="which investigator should go and find it")


class CriticResult(BaseModel):
    verdict: Literal["accept", "insufficient"]
    reasoning: str = Field(min_length=10, description="one or two sentences, why")
    gaps: list[Gap] = Field(
        default_factory=list, max_length=3,
        description="empty when accepting; what is missing when not")
    alternative_overlooked: str | None = Field(
        default=None, description="an explanation the RCA did not consider, if any")


# Placeholders are written as <...> rather than "a | b". Observed on 2026-10-23: the model returned
# the literal string "accept | insufficient" as its verdict, copying the hint instead of choosing
# from it - the same mistake as reading an example number as data. Angle brackets read as "replace
# this", a bare pipe list reads as a value.
SCHEMA_HINT = base.schema_hint({
    "verdict": "<write either accept or insufficient>",
    "reasoning": "<one or two sentences explaining the verdict>",
    "gaps": [{"missing": "<the specific evidence that is absent>",
              "agent": "<write one of metrics, log or change>"}],
    "alternative_overlooked": "<an explanation the RCA did not consider, or null>",
})

RULES = f"""Rules:

1. START BY ASKING WHETHER IT IS ALREADY SETTLED. Your default is "accept". Only reject when a
   second investigation round would realistically change the answer.

2. RETURN "accept" WHENEVER AN INVESTIGATOR ESTABLISHED THE CAUSE DIRECTLY. In particular, if any
   finding was flagged as the likely cause - an agent inspected the running configuration and found
   a change affecting this service or a service it calls - that is sufficient. Accept it. You do
   not need the mechanism spelled out: a configuration change affecting an upstream dependency
   explains downstream failures, and demanding proof of how it propagates wastes a round.
   Accept also when several agents independently point at the same cause.

3. RETURN "insufficient" ONLY when a round of further investigation could genuinely settle it, and
   one of these holds:
   - The top hypothesis rests only on symptoms - an error rate, a latency figure, an error message -
     with nothing at all explaining why they began, AND no agent flagged a cause.
   - The conclusion names a cause that no agent reported anywhere in the findings.
   - An investigator that could have settled the question was never run.
   Do not manufacture doubt. "It could be explored further" is true of every incident and is not a
   reason to reject.

4. EVERY GAP MUST BE ACTIONABLE AND SPECIFIC. Name the evidence that is missing and the one
   investigator who could find it. "More information" or "further investigation" is not a gap;
   "whether any configuration changed on the upstream service it calls" is. Only these
   investigators exist: metrics, log, change. You may name an investigator that already ran - a
   second pass asks it a sharper question, and time has moved on - but say what it should look for
   this time.

5. WHEN YOU ACCEPT, "gaps" MUST BE EMPTY. When you return insufficient, give at least one gap.

{base.NUMBER_RULES}
- Keep reasoning under 40 words."""


def _agent():
    return base.make_agent(
        role="Incident Review Critic",
        goal="Decide whether a proposed root cause is actually supported by the evidence, and say "
             "exactly what is missing when it is not.",
        backstory=(
            "You are AIRA's reviewer. You read a proposed root-cause analysis alongside the "
            "evidence it was built from, and you are willing to say the conclusion is not yet "
            "earned. You are equally willing to accept a well-supported one."
        ),
    )


def _rca_block(rca: dict) -> str:
    lines = ["PROPOSED ROOT-CAUSE ANALYSIS",
             f"- summary: {rca.get('summary')}",
             f"- overall confidence: {rca.get('confidence')}", "- hypotheses, in the order proposed:"]
    for n, h in enumerate(rca.get("hypotheses") or [], 1):
        lines.append(f"    {n}. {h.get('statement')} "
                     f"(confidence {h.get('confidence')}, from: {', '.join(h.get('from_agents') or [])})")
        for ev in (h.get("supporting_evidence") or []):
            lines.append(f"       supported by: {ev}")
    return "\n".join(lines)


def _consistency_check(findings: list[dict]):
    """Keep the verdict internally coherent and the gaps actionable.

    A verdict of "insufficient" with no gap is an opinion nobody can act on, and an "accept" that
    still lists gaps is contradictory. Both would silently break the replanning loop, which decides
    what to do next by reading exactly these fields.
    """
    # Vague gaps cannot drive a second round: the Orchestrator has to turn each one into a concrete
    # instruction for an investigator.
    VAGUE = ("more information", "further investigation", "more evidence", "more data",
             "additional information", "investigate further", "more details")
    # Claiming the analysis "rests only on symptoms" is simply false when an investigator inspected
    # configuration and flagged a cause. Observed on 2026-10-23: the critic rejected incidents on
    # that basis while the Change agent's flag sat in the evidence it had just been shown.
    SYMPTOM_CLAIM = ("only on symptoms", "solely on symptoms", "relies on symptoms",
                     "symptoms without", "no evidence of a cause", "no cause was")
    cause_flagged = any(
        item.get("likely_cause")
        for f in findings if f.get("status") == "done"
        for item in ((f.get("output") or {}).get("findings") or [])
    )

    def check(result: CriticResult):
        if result.verdict == "insufficient" and not result.gaps:
            raise ValueError(
                "You returned 'insufficient' without naming a gap. State the specific missing "
                "evidence and which investigator should find it, or return 'accept'."
            )
        if result.verdict == "accept" and result.gaps:
            raise ValueError(
                "You returned 'accept' but also listed gaps. Either the evidence is sufficient "
                "(accept, with no gaps) or it is not (insufficient)."
            )
        if (cause_flagged and result.verdict == "insufficient"
                and any(p in result.reasoning.lower() for p in SYMPTOM_CLAIM)):
            raise ValueError(
                "You said the analysis rests only on symptoms, but an investigator flagged a "
                "likely cause in the evidence you were shown - it inspected the running "
                "configuration directly. Either accept it, or give a different and specific "
                "reason why that finding does not settle the question."
            )
        vague = [g.missing for g in result.gaps
                 if any(v in g.missing.lower() for v in VAGUE) or len(g.missing.split()) < 4]
        if vague:
            raise ValueError(
                f"These gaps are too vague to act on: {'; '.join(vague)}. Each gap must name the "
                "specific evidence that is missing, not ask for more investigation in general."
            )

    return check


def run(inc: dict, findings: list[dict], rca: dict) -> tuple[CriticResult, int, float]:
    """Review an RCA against the evidence. Returns (result, attempts, seconds)."""
    from . import adk_base  # the shared findings formatter, reused so both sides see one view

    context = adk_base.findings_block(inc, findings) + "\n\n" + _rca_block(rca)
    prompt = (
        "Review this root-cause analysis against the evidence the agents actually gathered.\n\n"
        + context + "\n\n" + RULES
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, CriticResult, context,
                          check=_consistency_check(findings))
