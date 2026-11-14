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
   reason to reject. In particular, asking for evidence of HOW a confirmed change caused the
   symptoms - "show that the flag actually caused the failures", "show how it affected the error
   rate" - is not a gap. It is the demand rule 2 forbids, and the second round comes back with
   the same answer and the incident is half a minute older.

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


def _flagged_causes(findings: list[dict]) -> list[str]:
    """Findings an investigator marked as the likely cause after reading configuration directly."""
    return [item.get("statement", "")
            for f in findings if f.get("status") == "done"
            for item in ((f.get("output") or {}).get("findings") or [])
            if item.get("likely_cause")]


# Words that carry no identity here. Kept local rather than imported from rca_agent: that module
# pulls in ADK at import time, and this one is CrewAI-only.
_STOP = frozenset("""about above after again against because been before being below between both
during each from have into more most other over same some such than that them then there these
they this those through under until were what when where which while with would your cause caused
causing error errors failed failing failure service services agent agents incident flag""".split())


def _terms(text: str) -> tuple[set[str], set[str]]:
    """The distinctive words of a sentence, split into identifiers and ordinary words.

    An identifier - paymentFailure, PaymentService/Charge - names the cause on its own, so one
    match is decisive. An ordinary word like "payment" is not: the RCA naming the same service is
    no evidence it named the same cause, so those have to agree in pairs.
    """
    ident, plain = set(), set()
    for raw in text.replace("/", " ").replace(".", " ").split():
        w = raw.strip("\"'`,;:()[]{}<>!?%").lower()
        if not w or w in _STOP:
            continue
        if any(c.isupper() for c in raw[1:]):
            ident.add(w)
        elif len(w) > 4:
            plain.add(w)
    return ident, plain


def _settles_it(findings: list[dict], rca: dict | None) -> tuple[list[str], bool]:
    """Did an investigator settle this at the configuration level, and is the analysis built on it?

    Both halves matter. A flagged cause the analysis ignored is not settled - that is a dropped
    fact, and rejecting it is correct. A flagged cause the analysis is built on is rule 2's case
    exactly, and there is nothing a second round could add.

    Matched on whole words rather than substrings: the first version compared with `in`, and
    "service" from the flagged finding matched "services" in an unrelated hypothesis about network
    latency, which would have forced an acceptance the evidence never supported.
    """
    flagged = _flagged_causes(findings)
    if not flagged:
        return [], False
    ident, plain = _terms(flagged[0])
    top = ((rca or {}).get("hypotheses") or [{}])[0]
    said_i, said_p = _terms(
        f"{top.get('statement', '')} {' '.join(top.get('supporting_evidence') or [])}")
    said = said_i | said_p
    settled = bool(ident & said) or len(plain & said) >= 2
    return flagged, settled


def evidence_standing(findings: list[dict], rca: dict | None) -> str:
    """What this incident already settles, stated rather than left for the model to notice.

    Rule 2 covers the configuration-confirmed case in general, and the model applied it
    inconsistently: measured over 185 concluded incidents, it returned "insufficient" on 54% and
    sent 127 of them round a second time, while rule 1 says its default is "accept". The
    rejections were not the wording rule 3 anticipated - they asked for "evidence showing HOW the
    paymentFailure flag affected the error rate" on incidents where the Change agent had already
    read that flag off flagd at 100%, which is the very demand rule 2 calls a wasted round.

    Stating the fact for this incident, rather than leaving the model to notice that a rule
    applies, is the same technique that fixed the Change agent and the RCA agent.
    """
    flagged, settled = _settles_it(findings, rca)
    if settled:
        return ("\n\nTHIS INCIDENT: an investigator examined the running configuration directly "
                f"and established a cause - \"{flagged[0]}\" - and the analysis you are reviewing "
                "is built on exactly that finding. Rule 2 applies and is not optional: this is "
                "settled, and your verdict must be \"accept\". Do NOT ask for evidence of how it "
                "caused the symptoms, how it propagates, or proof that it \"actually\" did: rule 2 "
                "says that is not required, and a second round would return the same answer. "
                "Reject only if the analysis contradicts that finding or names a different cause.")
    if flagged:
        return ("\n\nTHIS INCIDENT: an investigator flagged a likely cause - "
                f"\"{flagged[0]}\" - but the analysis you are reviewing is not built on it. That "
                "is a dropped fact, and naming it is a legitimate reason to return "
                "\"insufficient\".")
    return ("\n\nTHIS INCIDENT: no investigator established a cause at the configuration level, so "
            "judge the analysis on the evidence as it stands.")


def _consistency_check(findings: list[dict], rca: dict | None = None):
    """Keep the verdict internally coherent and the gaps actionable.

    A verdict of "insufficient" with no gap is an opinion nobody can act on, and an "accept" that
    still lists gaps is contradictory. Both would silently break the replanning loop, which decides
    what to do next by reading exactly these fields.

    It also enforces rule 2, because prompt wording alone did not. The earlier guard matched a
    handful of phrasings ("rests only on symptoms"), and the rejections that actually happened
    were worded differently - "evidence that the flag actually caused the failures" - so they went
    straight past it. The condition is checked now rather than the sentence.
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
    flagged, settled = _settles_it(findings, rca)
    cause_flagged = bool(flagged)

    def check(result: CriticResult):
        # Rule 2, enforced. An investigator read the running configuration and the analysis is
        # built on what it found; there is no question left for another round to answer.
        if settled and result.verdict == "insufficient":
            raise ValueError(
                f"An investigator established this directly: \"{flagged[0]}\", and the analysis "
                "you reviewed is built on that finding. Rule 2 applies: that is sufficient, and "
                "asking how it caused the symptoms or for proof that it actually did is "
                "explicitly not required. Return \"accept\", unless you can give a reason that "
                "contradicts the finding itself rather than asking for more proof of it."
            )
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
        + evidence_standing(findings, rca)
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, CriticResult, context,
                          check=_consistency_check(findings, rca))
