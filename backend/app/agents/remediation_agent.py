"""Remediation agent — proposes a recovery action, and never executes one.

This agent is the narrowest in the project on purpose. It does not decide *how* to fix anything;
it chooses one name from a fixed enumeration and one target service, and `remediation.py` decides
whether that is permitted. The model cannot construct a command, cannot name an action that does
not exist, and cannot reach a service the action layer forbids.

Three things make "no safe action applies" a first-class answer rather than a failure:

* It is a value of the `action` field, not an error, so the schema can express it.
* It is the *required* answer whenever the cause lies in a service AIRA must never touch - a CPU
  alert on `grafana` has no safe remedy here, and saying so is correct, not a shortfall.
* The consistency check accepts it without argument. Only a *positive* proposal has to survive
  validation against the real allowlist.

The check is the important part. A proposal is run through `remediation.validate()` - the same
function that would gate a real execution - with nothing executed. If the action layer would refuse
it, the refusal text becomes the retry message, so the agent is corrected by the actual safety rule
rather than by a reworded prompt.
"""

from typing import Literal

from pydantic import BaseModel, Field

from . import base
from .. import remediation

AGENT_NAME = "remediation"
SEQ = 90

# "none" is listed first so the schema reads as "decline unless an action clearly applies".
PROPOSABLE = ("none", *remediation.ACTION_NAMES)


class ActionProposal(BaseModel):
    action: Literal["none", "reset_feature_flag", "restart_container"]
    target_service: str | None = Field(
        default=None, description="the service the action would be applied to, or null for none")
    flag: str | None = Field(
        default=None, description="the feature flag to reset, only for reset_feature_flag")
    reasoning: str = Field(min_length=10, description="one or two sentences, why this and not more")
    risk: Literal["low", "medium", "high"]
    expected_effect: str | None = Field(
        default=None, description="what should change if this works, in one sentence")


SCHEMA_HINT = base.schema_hint({
    "action": "<write exactly one of: none, reset_feature_flag, restart_container>",
    "target_service": "<the service this action applies to, or null if the action is none>",
    "flag": "<the feature flag name, only when the action is reset_feature_flag, else null>",
    "reasoning": "<one or two sentences explaining the choice>",
    "risk": "<write one of low, medium or high>",
    "expected_effect": "<what should change if this works, or null>",
})

RULES = f"""Rules, in priority order:

1. YOU MAY ONLY CHOOSE FROM THESE ACTIONS. There is no other action available to you, and
   inventing one achieves nothing - it will be refused before anything runs.
   - "reset_feature_flag": turn a feature flag off. This is the correct action when an agent found
     a feature flag that is not off and affects the alerting service or one it calls. It is the
     most common correct remedy in this environment and the safest.
   - "restart_container": restart a stopped or misbehaving application container. This is the
     correct action when the evidence says the service's container stopped or is not reporting.
   - "none": no safe action applies. This is a complete, correct answer - see rule 3.

2. PREFER THE FLAG RESET WHEN A FLAG WAS FLAGGED AS THE CAUSE. If a finding is marked as the likely
   cause and names a feature flag, reset that flag. Name the flag exactly as the evidence spells
   it, and set "target_service" to the service that flag affects - which the evidence states.

3. RESTORING A STOPPED SERVICE DOES NOT REQUIRE KNOWING WHY IT STOPPED. If the evidence says the
   container is not running, has stopped reporting, or its metrics have gone, then
   "restart_container" is the correct action even when the root cause was never established and
   even when the reviewer rejected the analysis. Restoring service and explaining a failure are
   different jobs, and a restart is safe and reversible.

4. ANSWER "none" WHENEVER ANY OF THESE IS TRUE. Declining is not a failure:
   - The cause lies in a monitoring or infrastructure service. AIRA must never act on those,
     because doing so blinds it instead of fixing anything.
   - The incident appears to have resolved already - for example a feature flag that caused it is
     now off. There is nothing to fix, and acting would change nothing.
   - No cause was established AND the service is still running, so there is nothing to restore and
     nothing confirmed to undo. Acting on a guess is worse than not acting.
   - The cause is understood but neither action above would address it.
   When you answer "none", set "target_service" and "flag" to null and explain which of these
   applies.

5. ONE ACTION, NOT A PLAN. Propose the single smallest action that could plausibly fix this
   incident. Do not propose a sequence, and do not propose something "as well as" something else.

6. THE TARGET MUST BE A SERVICE THIS INVESTIGATION ACTUALLY LOOKED AT. Naming a service that does
   not appear in the findings is incoherent and will be refused.

7. RISK IS ABOUT BLAST RADIUS, NOT CONFIDENCE. Resetting a feature flag is "low": it is reversible
   and affects one behaviour. Restarting a container is "medium": it is brief but interrupts live
   requests. Use "high" only if you believe an action is needed that you are not permitted to
   propose, and then answer "none" with that in your reasoning.

8. IF YOU ARE TOLD AN ACTION WOULD CHANGE NOTHING, DO NOT PROPOSE IT AGAIN. A safety layer reply
   saying a flag is already off means the fault is no longer present: answer "none" and say so.
   Repeating the same refused action wastes the only retry you get.

{base.NUMBER_RULES}
- Keep "reasoning" under 40 words. Do not describe how to perform the action; you are choosing
  what should happen, not how."""


def _agent():
    return base.make_agent(
        role="Remediation Planner",
        goal=("Choose the single safest action that could resolve an incident, or state plainly "
              "that no safe action applies."),
        backstory=(
            "You are AIRA's remediation planner. You work from what the investigation established "
            "and choose from a short list of reversible actions. You are deliberately "
            "conservative: proposing nothing is a respectable answer, and acting on an "
            "unconfirmed cause is not."
        ),
    )


def _incident_services(findings: list[dict]) -> set[str]:
    """Services this investigation actually looked at."""
    return {f["agent"] and f.get("service") for f in findings} - {None}


def _consistency_check(inc: dict, findings: list[dict]):
    """Reject a proposal the action layer would refuse, without executing anything.

    The same `remediation.validate()` that gates a real execution is used as the check, so the
    agent is corrected by the actual safety rule and its real wording rather than by a paraphrase
    in a prompt. Nothing is executed: validate() only reads state.
    """
    services = {inc.get("service")} | {
        f.get("affects") for f in (
            (next((x for x in findings if x["agent"] == "change"), {}) or {}).get("evidence") or {}
        ).get("active_flags", [])
    }
    services |= set((
        (next((x for x in findings if x["agent"] == "change"), {}) or {}).get("evidence") or {}
    ).get("dependencies") or [])
    services -= {None}

    def check(result: ActionProposal):
        if result.action == "none":
            # A decline needs no validation, but it does need a reason worth reading.
            if len(result.reasoning.split()) < 4:
                raise ValueError(
                    "You answered 'none' without explaining why. State which of the cases in "
                    "rule 3 applies.")
            return

        if not result.target_service:
            raise ValueError(
                f"You proposed '{result.action}' without naming a target service. Name the service "
                "the action applies to.")

        params = {"flag": result.flag, "to_variant": "off"} if result.flag else {}
        try:
            remediation.validate(result.action, result.target_service, params,
                                 incident_services=services or None)
        except remediation.ActionRefused as e:
            # The action layer's own words. If it would not allow this, the agent must choose again.
            raise ValueError(
                f"That action would be refused by AIRA's safety layer: {e}\n"
                "Choose a different action, or answer 'none' if no safe action applies."
            ) from e

    return check


def run(inc: dict, findings: list[dict], rca: dict | None,
        critic: dict | None = None,
        current_state: dict | None = None) -> tuple[ActionProposal, int, float]:
    """Propose a recovery action for this incident. Executes nothing.

    `current_state` matters on a second attempt. The findings describe what the investigation
    established when it ran, which may have been long before - and before an action was taken.
    Observed on incident 162: the evidence was collected at 14:23, a flag was turned off at 14:42,
    and the second proposal at 14:46 still read "the flag is set to 100%", because that was true
    when the evidence was gathered. The agent was not wrong about its input; its input was stale.

    Returns (proposal, attempts, seconds). The proposal still has to be validated and approved
    before anything happens to the running system.
    """
    from . import adk_base  # the shared findings formatter, so every agent sees one view

    context = adk_base.findings_block(inc, findings)
    if rca:
        top = (rca.get("hypotheses") or [{}])[0]
        context += (
            "\n\nROOT CAUSE ANALYSIS\n"
            f"- most likely cause: {rca.get('summary')}\n"
            f"- overall confidence: {rca.get('confidence')}\n"
            f"- supported by: {', '.join(top.get('from_agents') or []) or 'nothing stated'}\n"
            f"- actionable: {top.get('actionable')}"
        )
    else:
        context += "\n\nROOT CAUSE ANALYSIS\n- none was produced for this incident."
    if critic:
        context += (f"\n\nREVIEWER'S VERDICT ON THAT ANALYSIS\n- {critic.get('verdict')}: "
                    f"{critic.get('reasoning')}")

    if current_state:
        flags = current_state.get("active_flags") or []
        flag_lines = ("\n".join(
            f"    - {f['flag']} is '{f['variant']}', affecting {f['affects']}"
            for f in flags) or "    - every feature flag is currently off")
        container = current_state.get("container") or {}
        context += (
            "\n\nCURRENT STATE OF THE SYSTEM, READ JUST NOW\n"
            "This is what is true at this moment. The findings above describe what the "
            "investigation found earlier, which may have been before an action was already taken. "
            "Where the two disagree, this section is the current truth.\n"
            f"  feature flags that are not off:\n{flag_lines}\n"
            f"  container '{inc.get('service')}': {container.get('status', 'unknown')}"
        )
        previous = current_state.get("previous_action")
        if previous:
            context += (
                f"\n  an action has ALREADY been taken on this incident: {previous}\n"
                "  If that action resolved the problem, answer 'none'. Do not propose it again."
            )

    context += (
        "\n\nACTIONS AVAILABLE TO YOU\n"
        f"- reset_feature_flag: risk {remediation.RISK['reset_feature_flag']}, reversible by "
        "setting the flag back\n"
        f"- restart_container: risk {remediation.RISK['restart_container']}, no undo needed - the "
        "container returns to its declared state\n"
        "- none: no safe action applies\n"
        "AIRA may never act on these services: "
        f"{', '.join(sorted(remediation.FORBIDDEN_SERVICES))}"
    )

    prompt = (
        "Decide what single action, if any, should be taken to resolve this incident.\n\n"
        + context + "\n\n" + RULES
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, ActionProposal, context,
                          check=_consistency_check(inc, findings))
