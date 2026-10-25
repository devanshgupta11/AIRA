"""RCA agent — built with Google ADK, the project's second agent framework.

Until now each investigator reported separately and a human joined the dots. This agent reads the
whole shared incident context and produces ranked root-cause hypotheses, each tied back to the
agent whose finding supports it.

It reasons only over what the investigators found. It never queries anything itself, and it must
not introduce a cause that no agent reported - a model asked to explain an incident will otherwise
supply a plausible story from general knowledge about microservices rather than from this incident.
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from . import adk_base, base

log = logging.getLogger("aira.rca")

AGENT_NAME = "rca"
SEQ = 60

KNOWN_AGENTS = ("triage", "metrics", "log", "change")


class Hypothesis(BaseModel):
    statement: str = Field(min_length=10, description="the proposed root cause, one sentence")
    supporting_evidence: list[str] = Field(
        min_length=1, max_length=4,
        description="findings that support this, copied from what the agents reported")
    from_agents: list[str] = Field(
        min_length=1, description="which agents contributed that evidence")
    confidence: Literal["low", "medium", "high"]
    actionable: bool = Field(description="true if a concrete action could address this cause")


class RCAResult(BaseModel):
    hypotheses: list[Hypothesis] = Field(
        min_length=1, max_length=3, description="ranked, most likely first")
    summary: str = Field(min_length=10, description="one sentence stating the most likely cause")
    confidence: Literal["low", "medium", "high"] = Field(
        description="confidence in the top hypothesis")


# Placeholders are written as <...> rather than "a | b". Observed on 2026-10-23: from_agents came
# back as the single string "metrics | triage" - the model copied the pipe list instead of choosing
# from it, exactly as the Critic once returned the literal "accept | insufficient". Angle brackets
# read as "replace this"; a bare pipe list reads as a value. One name per list entry is spelled out
# because from_agents is a list of names, where a copied list is easiest to mistake for valid.
SCHEMA_HINT = base.schema_hint({
    "hypotheses": [{
        "statement": "<the proposed root cause, one sentence>",
        "supporting_evidence": ["<a finding an agent reported, copied from above>"],
        "from_agents": ["<one agent name per entry: triage, metrics, log or change>"],
        "confidence": "<write one of low, medium or high>",
        "actionable": "<write true or false>",
    }],
    "summary": "<one sentence naming the most likely cause>",
    "confidence": "<write one of low, medium or high>",
})

INSTRUCTION = (
    "You are AIRA's root-cause analyst. Several specialist agents have each investigated one "
    "aspect of an incident. Your job is to combine their findings into ranked hypotheses about "
    "what actually caused it. You never investigate anything yourself and you never introduce a "
    "cause that no agent reported."
)

RULES = f"""Rules, in priority order:

1. AN AGENT'S FLAGGED CAUSE OUTRANKS EVERYTHING. If any finding is marked
   "[THIS AGENT FLAGGED THIS AS THE LIKELY CAUSE]", your FIRST hypothesis must be about it. That
   agent examined the configuration directly; do not demote it in favour of a symptom.

2. RANK BY EVIDENCE, NOT BY PLAUSIBILITY. Order hypotheses by how much of the reported evidence
   supports them. A cause that explains three findings outranks one that explains a single symptom.

3. USE ONLY WHAT THE AGENTS REPORTED. Every "supporting_evidence" entry must come from the findings
   above. Do not invent a deployment, a code change, a log line or a measurement. If the evidence
   is thin, say so with low confidence and a single hypothesis rather than padding to three.

4. SYMPTOMS ARE NOT CAUSES. "The error rate is high" is what happened, not why. Neither is an error
   message: a service reporting a rejected token tells you what it did, not why it started doing
   it. A root cause explains the symptoms - a configuration change, a dependency failing, resource
   exhaustion, a stopped process. If the evidence only shows symptoms, say so plainly and keep
   confidence low.

5. WHEN NO AGENT FLAGGED A CAUSE, YOU ARE GUESSING, AND MUST SAY SO.
   - Confidence must be "low" or "medium", never "high". Nothing was confirmed at the
     configuration level, so certainty is unjustified.
   - Give at least TWO hypotheses offering genuinely different explanations, so the alternatives
     are visible rather than hidden behind one confident-sounding sentence.
   Only when an agent explicitly flagged a likely cause may you answer with a single hypothesis.

6. A SERVICE CAN FAIL BECAUSE ANOTHER ONE DID. If the evidence says a change affects a service this
   one calls, the cause belongs to that upstream service; name it rather than blaming this one.

7. "from_agents" must only contain names that actually appear in the findings above.

{base.NUMBER_RULES}
- Set actionable true only when a concrete, reversible step could address the cause - for example
  reverting a configuration change or restarting a stopped service.
- Keep every statement under 25 words."""


def _agent(local: bool = False):
    """The RCA agent.

    By default this is a *proxy* that reaches the real agent over A2A - the CrewAI side of AIRA
    crosses a protocol boundary to talk to the ADK side, rather than importing it. `local=True`
    runs the same agent in-process, which exists only so the agent can be tested without the A2A
    server running; the production path is always A2A.
    """
    if local:
        return adk_base.make_adk_agent("aira_rca", INSTRUCTION)
    return adk_base.a2a_proxy("aira_rca_remote")


def _flagged_causes(findings: list[dict]) -> list[str]:
    """Findings an investigator marked as the likely cause after reading configuration directly."""
    return [
        item.get("statement", "")
        for f in findings
        if f.get("status") == "done"
        for item in ((f.get("output") or {}).get("findings") or [])
        if item.get("likely_cause")
    ]


def evidence_standing(findings: list[dict]) -> str:
    """What this particular incident's evidence does and does not establish.

    Rule 5 covers the thin-evidence case in general, but the model applied it inconsistently -
    observed on 2026-10-23: on a stopped-container incident it answered "high" confidence with a
    single hypothesis twice in a row, and the consistency check turned that into an error state
    rather than an answer. Stating the fact for this incident, rather than leaving the model to
    notice the rule applies, is the same technique that fixed the Change agent.
    """
    flagged = _flagged_causes(findings)
    if flagged:
        return ("\n\nTHIS INCIDENT: an investigator flagged a likely cause after examining "
                f"configuration directly - \"{flagged[0]}\". Rule 1 applies: that must be your "
                "first hypothesis.")
    return ("\n\nTHIS INCIDENT: NO agent flagged a likely cause. Nothing has been confirmed at the "
            "configuration level, so every hypothesis you give is inference from symptoms. Rule 5 "
            "applies and is not optional: set \"confidence\" to \"low\" or \"medium\" (never "
            "\"high\"), and give at least TWO hypotheses with genuinely different explanations.")


# Words that carry no identity. Used when deciding whether a hypothesis has borrowed its
# content from a past case rather than from this incident's evidence.
_STOP = frozenset("""about above after again against because been before being below between both
during each from have into more most other over same some such than that them then there these
they this those through under until were what when where which while with would your cause caused
causing error errors failed failing failure service agent incident""".split())


def _tokens(text: str) -> set[str]:
    """The distinctive words of a sentence: long ones, and anything that looks like an identifier.

    Flag and operation names - paymentFailure, PaymentService/Charge - are the tokens that actually
    identify a cause, so an inner capital or a slash is enough to keep a word even when it is short.
    """
    out = set()
    for raw in text.replace("/", " ").replace(".", " ").split():
        w = raw.strip("\"'`,;:()[]{}<>!?").lower()
        if not w or w in _STOP:
            continue
        if len(w) > 4 or any(c.isupper() for c in raw[1:]):
            out.add(w)
    return out


def _findings_tokens(findings: list[dict]) -> set[str]:
    """Everything this incident's own investigators actually said."""
    out = set()
    for f in findings:
        if f.get("status") != "done":
            continue
        o = f.get("output") or {}
        texts = [o.get("summary") or ""]
        for item in o.get("findings") or []:
            texts += [item.get("statement") or "", str(item.get("measurement") or ""),
                      str(item.get("quoted_line") or ""), str(item.get("detail") or "")]
        out |= _tokens(" ".join(texts))
    return out


def prior_block(prior: list[dict]) -> str:
    """Past cases, stated as what they are: a place to look, not a finding about this incident.

    Deliberately no measurements - not even inside the remembered cause, which is why the text is
    scrubbed rather than quoted. "The paymentFailure flag set to 100%" is a true statement about
    that incident and a trap for this one: the flag identity transfers, the figure does not, and
    this project has already watched a model copy example numbers out of a prompt instead of
    reading its own data. What survives the scrub is exactly the part that generalises.
    """
    from .. import memory
    if not prior:
        return ""
    lines = []
    for c in prior:
        fix = ""
        if c.get("action"):
            worked = c.get("action_worked")
            fix = (f'; was fixed by "{c["action"]}"'
                   + (" (confirmed to work)" if worked is True
                      else " (did not fix it)" if worked is False else " (unverified)"))
        cause = memory.scrub_magnitudes(c.get("cause") or "")
        lines.append(f'- Incident #{c["incident_id"]} ({c["alertname"]} on {c["service"]}): '
                     f'the cause was "{cause}"{fix}')
    return (
        "\n\nPREVIOUSLY SEEN - CONTEXT ONLY, NOT EVIDENCE FOR THIS INCIDENT:\n"
        "Past incidents whose symptoms resembled this one. They tell you where it is worth "
        "looking. They are NOT observations about this incident, and nothing here has been "
        "checked against the system as it is now.\n" + "\n".join(lines)
    )


PRIOR_RULE = """

8. A PAST CASE IS A HINT, NOT A FINDING. The "PREVIOUSLY SEEN" section describes other incidents.
   - Never put a past incident, or anything taken from one, in "supporting_evidence". That field
     is only for what the agents reported about THIS incident.
   - You may only name a past cause as your hypothesis if THIS incident's findings independently
     support it. If they do not, the resemblance is a lead that nobody has checked, and your
     confidence must stay low.
   - If the agents found nothing pointing that way, say so instead of repeating what happened last
     time. The last incident is not this one."""


def _consistency_check(findings: list[dict], prior: list[dict] | None = None):
    """Reject an analysis that ignores what an investigator already established.

    The Change agent can mark a finding as the likely cause after reading configuration directly.
    An RCA that omits it is not a different opinion, it is a dropped fact - the same class of
    mistake that prompting alone failed to prevent in the Change agent itself.
    """
    flagged = _flagged_causes(findings)
    # Compare on distinctive words rather than whole sentences: the RCA legitimately rephrases.
    keywords = set()
    for s in flagged:
        keywords |= {w.strip(".,'\"").lower() for w in s.split()
                     if len(w) > 4 and any(c.isupper() for c in w[1:]) or w.lower().endswith("failure")}

    # The tokens a past cause contributes that this incident's own evidence never mentions. If a
    # hypothesis is built out of these, it came from memory rather than from the investigation -
    # which is the one failure mode retrieval introduces, and the reason this is a validator and
    # not a line of prompt wording.
    own = _findings_tokens(findings)
    borrowed: list[tuple[str, set[str]]] = []
    for c in prior or []:
        unsupported = _tokens(c.get("cause") or "") - own
        if unsupported:
            borrowed.append((f'#{c["incident_id"]}', unsupported))

    def check(result: RCAResult):
        # Checked first, and for every answer: citing an agent that never ran is a fabricated
        # source whether or not anything was confirmed, and the thin-evidence branch below returns
        # early, so a later check would never see those answers at all.
        named = {a.lower() for h in result.hypotheses for a in h.from_agents}
        unknown = named - set(KNOWN_AGENTS)
        if unknown:
            raise ValueError(
                f"from_agents contains names that did not investigate this incident: "
                f"{', '.join(sorted(unknown))}. Use only: {', '.join(KNOWN_AGENTS)}."
            )

        # A past incident may never be cited as evidence for this one, however it is phrased.
        for h in result.hypotheses:
            for e in h.supporting_evidence:
                low = e.lower()
                if re.search(r"incident\s*#?\d+", low) or any(
                        w in low for w in ("previously seen", "past incident", "previous incident",
                                           "earlier incident", "similar incident", "last time")):
                    raise ValueError(
                        "supporting_evidence cites a past incident: "
                        f"\"{e}\". That field is only for what the agents reported about THIS "
                        "incident. Remove it, or replace it with a finding from this "
                        "investigation."
                    )

        # Borrowing a remembered cause that nothing here supports.
        if borrowed:
            top = result.hypotheses[0]
            top_tokens = _tokens(f"{top.statement} {' '.join(top.supporting_evidence)}")
            for ref, unsupported in borrowed:
                hit = top_tokens & unsupported
                if len(hit) >= 2:
                    raise ValueError(
                        f"Your first hypothesis repeats the cause of past incident {ref} "
                        f"({', '.join(sorted(hit))}), but no agent reported anything of the kind "
                        "about THIS incident. A past case is a hint, not a finding: either base "
                        "the hypothesis on what the agents actually reported here, or say the "
                        "evidence does not establish a cause."
                    )

        if not flagged:
            # Nothing was confirmed at the configuration level, so the analysis is inference from
            # symptoms. Observed on 2026-10-23: the agent restated a log message as the cause with
            # high confidence and offered no alternative, which reads as certainty it has not
            # earned. Enforced here because the same class of overreach survived prompt wording
            # alone in the Change agent.
            if result.confidence == "high":
                raise ValueError(
                    "No agent flagged a likely cause, so nothing was confirmed at the "
                    "configuration level. Confidence must be 'low' or 'medium', and you must give "
                    "at least two genuinely different hypotheses."
                )
            if len(result.hypotheses) < 2:
                raise ValueError(
                    "No agent flagged a likely cause, so alternatives must be visible. Give at "
                    "least two hypotheses offering different explanations, not one."
                )
            return
        top = result.hypotheses[0]
        text = f"{top.statement} {' '.join(top.supporting_evidence)}".lower()
        if keywords and not any(k in text for k in keywords):
            raise ValueError(
                "An investigator flagged a likely cause and your first hypothesis does not mention "
                f"it: {flagged[0]}. That finding came from examining configuration directly, so it "
                "must be your first hypothesis unless the evidence contradicts it."
            )
    return check


async def _run_once(inc: dict, findings: list[dict], prior: list[dict],
                    local: bool) -> tuple[RCAResult, int, float]:
    context = adk_base.findings_block(inc, findings) + prior_block(prior)
    prompt = (
        "Determine the most likely root cause of this incident, using only what the agents found.\n\n"
        + context + "\n\n" + RULES + (PRIOR_RULE if prior else "")
        + evidence_standing(findings)
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return await adk_base.run_adk_agent(
        _agent(local), prompt, SCHEMA_HINT, RCAResult, context,
        check=_consistency_check(findings, prior),
    )


async def run(inc: dict, findings: list[dict], prior: list[dict] | None = None,
              local: bool = False) -> tuple[RCAResult, int, float]:
    """Produce ranked hypotheses from the shared incident context.

    `prior` is what incident memory retrieved: past cases whose symptoms resembled this one. They
    are appended to the context clearly marked as context, governed by rule 8, and enforced by the
    consistency check - prompting is a request, validation is the guarantee.

    If that guarantee keeps failing, the retrieval is dropped and the question is asked again with
    the evidence alone. Observed while evaluating incident #47, whose Change evidence did not name
    a flag: memory offered three payment cases that did, the model built its hypothesis out of
    them, the check rejected it, the retry produced the same answer, and the whole investigation
    ended in an error with no conclusion at all. A hint that the model cannot justify is worse
    than no hint, and "memory is an advantage, never a dependency" has to hold here most of all -
    this is the one place where retrieval can actively make the system worse than it was without
    it. The fallback costs one extra call on a path that was about to fail outright.

    Reached over A2A unless `local=True` (testing only).
    """
    prior = prior or []
    try:
        return await _run_once(inc, findings, prior, local)
    except Exception:
        if not prior:
            raise
        from .. import events
        events.record("aira", "memory_discarded", agent=AGENT_NAME, ok=False,
                      detail={"cases": [c.get("incident_id") for c in prior],
                              "why": "the analysis could not be justified from this incident's "
                                     "own evidence; retrying without the retrieved cases"})
        log.warning("incident %s: retrieved cases led nowhere defensible, retrying without memory",
                    inc.get("id"))
        return await _run_once(inc, findings, [], local)
