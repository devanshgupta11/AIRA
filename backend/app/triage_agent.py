"""CrewAI Triage Agent running on a local Ollama model.

The agent receives one alert plus the live Prometheus evidence and returns a structured triage
summary. It reasons only over that evidence and takes no actions. Output is validated with
Pydantic; on invalid output the agent is asked once to correct it, after which the incident gets
an honest error state.

Shared plumbing (LLM, prompt rules, JSON parsing, retry) lives in agents/base.py so every agent
behaves identically. This module keeps its original public API - run(), TriageError,
_incident_block - so main.py and scripts/check_agent_numbers.py need no changes.
"""

from typing import Literal

from pydantic import BaseModel, Field

from .agents import base
from .agents.base import AgentError

CauseCategory = Literal[
    "resource_saturation",      # CPU / memory pressure
    "application_error",        # the service itself returns errors
    "service_unavailable",      # the service / container is not running
    "dependency_failure",       # errors caused by a downstream service
    "latency_degradation",      # slow responses without errors
    "traffic_change",           # abnormal request volume
    "unknown",                  # evidence does not support a category
]


class TriageResult(BaseModel):
    summary: str = Field(min_length=10, description="2-3 sentence factual incident summary")
    affected_service: str = Field(min_length=1)
    observed_symptoms: list[str] = Field(min_length=1, description="facts taken from the evidence, with numbers")
    likely_cause_category: CauseCategory
    suggested_next_investigation: list[str] = Field(min_length=1, max_length=5)
    confidence: Literal["low", "medium", "high"]


SCHEMA_HINT = base.schema_hint({
    "summary": "string, 2-3 sentences",
    "affected_service": "string",
    "observed_symptoms": ["string with the measured value", "..."],
    "likely_cause_category": " | ".join(CauseCategory.__args__),
    "suggested_next_investigation": ["string", "..."],
    "confidence": "low | medium | high",
})

RULES = f"""Rules:
{base.NUMBER_RULES}
- likely_cause_category is a category suggested by the evidence, not a proven root cause.
- Choose confidence "high" only when several independent measurements agree; "low" when the
  evidence is thin or contradictory.
- suggested_next_investigation lists what a human or a later investigator agent should check next
  (e.g. traces, logs, recent changes, dependencies). Do not propose fixes or take actions."""

# Kept for backwards compatibility: main.py catches TriageError, and check_agent_numbers.py
# calls _incident_block to work out which numbers the agent was allowed to use.
TriageError = AgentError
_incident_block = base.incident_block


def _agent():
    return base.make_agent(
        role="Incident Triage Agent",
        goal="Turn a monitoring alert and its live metrics evidence into an accurate, evidence-grounded triage summary.",
        backstory=(
            "You are the first responder in AIRA, an incident response system for a microservices shop. "
            "You read alerts and Prometheus evidence carefully and never claim more than the data shows."
        ),
    )


def run(inc: dict) -> tuple[TriageResult, int, float]:
    """Run the triage agent for an incident. Returns (result, attempts, seconds)."""
    context = base.incident_block(inc)
    prompt = (
        "Triage this incident.\n\n" + context + "\n\n" + RULES +
        "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    return base.run_agent(_agent(), prompt, SCHEMA_HINT, TriageResult, context)
