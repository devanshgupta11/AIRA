"""Metrics Agent: interprets the live Prometheus evidence for the alerting service.

The numbers themselves come from prometheus_tool.py, which runs fixed PromQL - deterministic and
repeatable. This agent only decides what those numbers mean: which are abnormal relative to the
service's own baseline, and what that pattern suggests.

Output is deliberately terse. Generation time scales with tokens and every agent shares one local
model, so investigators return a few short findings rather than prose.
"""

from typing import Literal

from pydantic import BaseModel, Field

from . import base
from .. import prometheus_tool

AGENT_NAME = "metrics"


class MetricFinding(BaseModel):
    statement: str = Field(min_length=5, description="one short factual observation")
    measurement: str = Field(min_length=1, description="the exact measured value this is based on")
    abnormal: bool = Field(description="true if this differs meaningfully from the baseline")


class MetricsResult(BaseModel):
    findings: list[MetricFinding] = Field(min_length=1, max_length=4)
    assessment: Literal["normal", "degraded", "critical"]
    confidence: Literal["low", "medium", "high"]


SCHEMA_HINT = base.schema_hint({
    "findings": [{
        "statement": "short factual observation",
        "measurement": "the exact value from the evidence, copied character-for-character",
        "abnormal": "true | false",
    }],
    "assessment": "normal | degraded | critical",
    "confidence": "low | medium | high",
})

RULES = f"""Rules:
{base.NUMBER_RULES}
- Report at most 4 findings. Prefer the measurements that differ most from their baseline.
- Each finding's "measurement" must be copied from the evidence, including its unit and window.
  Use the form "<metric> (<window>): <value> (baseline <value>)", taking every value from the
  evidence above. The placeholders here are not data: never write a number that appears only in
  these instructions.
- Set "abnormal" true only when the current value differs meaningfully from its own baseline. A
  value that matches its baseline is normal even if it looks high.
- State the direction and get it right: compare the two numbers before writing. If the current value
  is smaller than the baseline it has FALLEN, if larger it has RISEN. Never say a value increased
  when its number is lower than the baseline.
- If the evidence lists failing operations, include one finding naming the worst one, because it
  identifies exactly which call is breaking.
- If a measurement is "unavailable", you may report that fact, but mark it abnormal only if its
  absence is itself meaningful (for example a container that has stopped reporting).
- Keep every statement under 20 words. Do not suggest fixes or next steps; another agent does that."""


def collect(service: str | None) -> dict:
    """Gather this agent's evidence. Deterministic, no LLM, safe to run in parallel."""
    return prometheus_tool.collect(service)


def _agent():
    return base.make_agent(
        role="Metrics Investigator",
        goal="Identify which of a service's measured metrics are genuinely abnormal, and how severe that is.",
        backstory=(
            "You are AIRA's metrics specialist. You compare each live measurement against that same "
            "service's own baseline and report only what the numbers actually show."
        ),
    )


def run(inc: dict, evidence: dict | None = None, focus: str | None = None) -> tuple[MetricsResult, int, float]:
    """Interpret Prometheus evidence for this incident. Returns (result, attempts, seconds)."""
    ev = evidence if evidence is not None else collect(inc.get("service"))
    context = base.incident_block(inc, evidence_text=ev.get("text"))
    prompt = (
        "Analyse the metrics for this incident and report which measurements are abnormal.\n\n"
        + context + "\n\n" + RULES
        + base.focus_block(focus)
        + "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, MetricsResult, context)
