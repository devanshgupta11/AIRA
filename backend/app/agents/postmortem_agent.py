"""The postmortem: what happened, what was done about it, and what to do next.

Written after the response is over - once an action has been carried out and verified, or once it
is settled that none will be. It is the last step of the workflow and the one a human actually
reads afterwards.

A deliberate split runs through this module. Everything factual - when the alert fired, which
agents ran, what each concluded, what action was approved and by whom, whether the service
recovered - is already recorded in the database, and `postmortem.py` assembles it verbatim. The
model is never asked for any of it. What the model writes is the part that is genuinely writing:
a readable summary, what the incident revealed, and what somebody should do about it next.

That split is not stylistic. This project has already watched a 7B model copy example figures out
of a prompt instead of reading its data, and a postmortem is exactly the artifact where an
invented timestamp or a wrong duration would be believed - it is the document that outlives the
incident. So the narrative is generated and the facts are assembled, and the two never swap jobs.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from . import base

log = logging.getLogger("aira.postmortem")

AGENT_NAME = "postmortem"
SEQ = 100   # after remediation (90); the last row in the timeline


class FollowUp(BaseModel):
    action: str = Field(description="one concrete thing somebody should do, in under 20 words")
    why: str = Field(description="what it would prevent or improve, in under 25 words")
    priority: Literal["high", "medium", "low"]


class Postmortem(BaseModel):
    """The written part. Every factual section is assembled from the record, not from here."""

    title: str = Field(description="one line naming the incident, under 12 words, no dates or numbers")
    summary: str = Field(
        description="2-4 sentences: what broke, what caused it, what was done. No figures.")
    customer_impact: str = Field(
        description="one or two sentences on what a user of the shop would have noticed")
    what_went_well: list[str] = Field(
        default_factory=list, description="1-3 things the response got right")
    what_went_badly: list[str] = Field(
        default_factory=list, description="1-3 things that were slow, wrong or missing")
    follow_ups: list[FollowUp] = Field(
        default_factory=list, description="1-4 concrete follow-up actions")


SCHEMA_HINT = base.schema_hint({
    "title": "string",
    "summary": "string",
    "customer_impact": "string",
    "what_went_well": ["string"],
    "what_went_badly": ["string"],
    "follow_ups": [{"action": "string", "why": "string", "priority": "high | medium | low"}],
})

INSTRUCTION_ROLE = "Site reliability engineer writing a postmortem"
INSTRUCTION_GOAL = ("Turn the record of a resolved incident into a short, honest postmortem that "
                    "a colleague who was not involved can read and learn from")
INSTRUCTION_STORY = (
    "You write postmortems that are blameless and specific. You never pad, never congratulate, "
    "and never claim more was understood than actually was."
)

RULES = f"""Rules:

1. WRITE NO NUMBERS. Not times, not durations, not percentages, not counts. Every figure in this
   document is inserted from the recorded data after you finish, and a number you write here will
   contradict one of them. Say "error rate rose sharply", never "error rate rose to 75%".
   Any figure you write will be removed before the report is published, leaving a gap in your
   sentence - so a sentence that depends on one will read as though a word is missing.

2. USE ONLY THE RECORD BELOW. Do not invent a deployment, a customer report, a meeting, an oncall
   engineer, or a cause nobody established. If the cause was never confirmed, say so plainly -
   "the investigation did not establish a cause" is a legitimate postmortem.

3. BE HONEST ABOUT WHAT WENT BADLY. An investigation that took several rounds, a reviewer that
   rejected the first answer, an action that did not fix it, a cause that stayed unconfirmed -
   these belong in "what_went_badly". A postmortem with nothing in that list is not credible.

4. BLAMELESS. Describe what the system did, not who should have known better. "The alert rule
   averages over three minutes, which delayed detection" - not "nobody noticed".

5. FOLLOW-UPS MUST BE ACTIONABLE AND MUST COME FROM THIS INCIDENT. Name the specific thing that
   went wrong here and what would change it. A follow-up that could have been written without
   reading this record is not a follow-up. If the record supports none, return an empty list.
   Do not propose work on a part of the system this incident did not touch.

{base.NUMBER_RULES}"""


def _agent():
    return base.make_agent(
        role=INSTRUCTION_ROLE, goal=INSTRUCTION_GOAL, backstory=INSTRUCTION_STORY)


def _check(result: Postmortem) -> None:
    """Reject a report that says nothing. Figures are handled elsewhere, on purpose.

    The first version of this raised on any number in the narrative, and it was wrong - not about
    the problem, which is real, but about the remedy. The model had just been shown a record full
    of percentages and asked to write about it, so it quoted one; the retry quoted it again; and
    the whole report was lost over the string "100%". That is the same mistake as letting a bad
    retrieved hint fail an investigation: a guarantee that destroys the artifact instead of
    repairing it.

    So the figures are now stripped deterministically when the report is rendered, which cannot
    fail and cannot be argued with, and this check is left to do the one thing a retry can
    actually fix - a summary that is empty or a single word.
    """
    if len(result.summary.split()) < 12:
        raise ValueError(
            "The summary is too short to be useful. Write two to four sentences covering what "
            "broke, what caused it, and what was done about it."
        )


def run(record: str) -> tuple[Postmortem, int, float]:
    """Write the narrative sections from the assembled record. Returns (result, attempts, secs)."""
    prompt = (
        "Write a postmortem for this incident. The record below is everything that was observed "
        "and done; it is complete and it is the only source you may use.\n\n"
        + record + "\n\n" + RULES
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, Postmortem, record, check=_check)
