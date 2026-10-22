"""Log Agent: reads the alerting service's real log lines around the incident.

Evidence comes from log_tool.py, which merges OpenSearch and container stdout. The agent only
interprets those lines: which look related to the alert, and what they say is happening.

It must never invent a log line. When a service produced no logs at all - `checkout` and
`product-catalog` write nothing to either source - saying so plainly is the correct answer, because
"no logs" is useful information: it tells the investigation to rely on metrics and traces instead.
"""

from typing import Literal

from pydantic import BaseModel, Field

from . import base
from .. import log_tool

AGENT_NAME = "log"


class LogFinding(BaseModel):
    statement: str = Field(min_length=5, description="what this log evidence shows, in one short sentence")
    quoted_line: str = Field(min_length=1, description="the log message this is based on, copied exactly, or 'none'")
    related_to_alert: bool


class LogResult(BaseModel):
    logs_available: bool = Field(description="false when the service produced no log lines at all")
    findings: list[LogFinding] = Field(min_length=1, max_length=3)
    confidence: Literal["low", "medium", "high"]


SCHEMA_HINT = base.schema_hint({
    "logs_available": "true | false",
    "findings": [{
        "statement": "short sentence describing what the logs show",
        "quoted_line": "the log message copied exactly from the evidence, or 'none'",
        "related_to_alert": "true | false",
    }],
    "confidence": "low | medium | high",
})

RULES = f"""Rules:
{base.NUMBER_RULES}
- Every "quoted_line" must be copied exactly from the log evidence above. Never write a log line that
  does not appear there, and never reword one.
- If the evidence says the service produced NO log lines, set logs_available false and return exactly
  one finding saying that no logs were available, with quoted_line "none" and related_to_alert false.
  Do not speculate about what the logs might have contained.
- Set related_to_alert true only when the message plausibly concerns the alerted failure. Routine
  startup, health-check and exporter-retry messages are usually unrelated.
- Occurrence counts matter: a message seen many times is stronger evidence than one seen once. If you
  mention a count, copy it exactly.
- Keep every statement under 20 words. Do not suggest fixes."""


def collect(service: str | None, starts_at: str) -> dict:
    """Gather this agent's evidence. Deterministic, no LLM, safe to run in parallel."""
    return log_tool.collect(service, starts_at)


def _agent():
    return base.make_agent(
        role="Log Investigator",
        goal="Determine what a service's own log lines reveal about an incident, or state clearly that none exist.",
        backstory=(
            "You are AIRA's log specialist. You read raw application logs and report only what they "
            "actually say. You never invent or paraphrase a log line, and you are comfortable "
            "reporting that a service logged nothing at all."
        ),
    )


def run(inc: dict, evidence: dict | None = None) -> tuple[LogResult, int, float]:
    """Interpret log evidence for this incident. Returns (result, attempts, seconds)."""
    ev = evidence if evidence is not None else collect(inc.get("service"), inc["starts_at"])
    context = base.incident_block(inc, evidence_text=ev.get("text"), evidence_label="EVIDENCE (log lines)")
    prompt = (
        "Examine the logs for this incident and report what they show.\n\n"
        + context + "\n\n" + RULES
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, LogResult, context)
