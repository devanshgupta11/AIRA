"""CrewAI Triage Agent running on a local Ollama model.

The agent receives one alert plus the live Prometheus evidence and returns a structured triage
summary. It reasons only over that evidence and takes no actions. Output is validated with
Pydantic; on invalid output the agent is asked once to correct it, after which the incident gets
an honest error state.
"""

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from . import config  # noqa: F401  (sets CrewAI telemetry env vars before crewai is imported)
from .config import OLLAMA_MODEL, OLLAMA_URL
from crewai import LLM, Agent, Crew, Process, Task  # noqa: E402

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


SCHEMA_HINT = json.dumps({
    "summary": "string, 2-3 sentences",
    "affected_service": "string",
    "observed_symptoms": ["string with the measured value", "..."],
    "likely_cause_category": " | ".join(CauseCategory.__args__),
    "suggested_next_investigation": ["string", "..."],
    "confidence": "low | medium | high",
}, indent=2)

RULES = """Rules:
- Use ONLY the facts in the alert and evidence below. Do not invent metrics, logs, deployments,
  error messages, root causes or numbers that are not present.
- Copy every number character-for-character from the text above. NEVER average, round, combine or
  recalculate them. The alert and the evidence are measured over different time windows at different
  moments, so they legitimately disagree: that is expected, and you must not reconcile them into a
  single figure. If you write a percentage, the exact same digits must appear in the text above.
- When you state a number, also state where it came from, e.g. "error ratio over 2 minutes is 100.0%"
  or "the alert reports 70% of calls failed over 3 minutes". Prefer quoting one specific measurement
  over summarising several.
- If a value is "unavailable", say it is unavailable; do not guess it.
- "threshold" and "baseline" are different things and must never be swapped. The threshold is the level
  that made the alert fire; the baseline is what this service actually measured 15-25 minutes ago.
  Saying "higher than the baseline of <threshold>" is wrong.
- likely_cause_category is a category suggested by the evidence, not a proven root cause.
- Choose confidence "high" only when several independent measurements agree; "low" when the
  evidence is thin or contradictory.
- suggested_next_investigation lists what a human or a later investigator agent should check next
  (e.g. traces, logs, recent changes, dependencies). Do not propose fixes or take actions.
- Reply with a single JSON object only. No markdown, no code fences, no text before or after it."""


def _llm() -> LLM:
    return LLM(model=f"ollama/{OLLAMA_MODEL}", base_url=OLLAMA_URL, temperature=0.1, timeout=150)


def _agent() -> Agent:
    return Agent(
        role="Incident Triage Agent",
        goal="Turn a monitoring alert and its live metrics evidence into an accurate, evidence-grounded triage summary.",
        backstory=(
            "You are the first responder in AIRA, an incident response system for a microservices shop. "
            "You read alerts and Prometheus evidence carefully and never claim more than the data shows."
        ),
        llm=_llm(),
        allow_delegation=False,
        max_iter=2,
        verbose=False,
    )


def _clarify_threshold(description: str | None) -> str:
    """Spell out what the alert's trigger level is.

    Alert descriptions end with "(threshold 5%)". Sitting next to the measured error percentage,
    that bare word was repeatedly misread by the model as the service's baseline, producing
    "higher than the baseline of 5%" when the measured baseline was 4.3%. Naming it explicitly
    removes the ambiguity at the source instead of relying on the prompt alone.
    """
    if not description:
        return ""
    return re.sub(
        r"\(threshold ([^)]+)\)",
        r"(this alert fires when the value goes above \1; that is the rule's trigger level, "
        r"not a measurement of this service)",
        description,
    )


def _incident_block(inc: dict) -> str:
    labels = {k: v for k, v in (inc.get("labels") or {}).items()}
    return (
        "ENVIRONMENT\n"
        "- The OpenTelemetry Demo (Astronomy Shop) microservices, each running as a Docker container\n"
        "  (Docker Compose, single host). There is no Kubernetes. Container name = service name.\n"
        "- Available data sources: Prometheus metrics (below), Jaeger traces, container logs, feature flags.\n\n"
        "ALERT\n"
        f"- alertname: {inc['alertname']}\n"
        f"- service: {inc.get('service')}\n"
        f"- severity: {inc.get('severity')}\n"
        f"- started at: {inc['starts_at']}\n"
        f"- summary: {inc.get('summary')}\n"
        f"- description: {_clarify_threshold(inc.get('description'))}\n"
        f"- labels: {json.dumps(labels)}\n\n"
        "EVIDENCE (live Prometheus queries)\n"
        f"{inc.get('evidence_text') or 'No evidence could be collected.'}"
    )


def _kickoff(description: str) -> str:
    task = Task(
        description=description,
        expected_output="One JSON object matching this structure:\n" + SCHEMA_HINT,
        agent=_agent(),
    )
    crew = Crew(agents=[task.agent], tasks=[task], process=Process.sequential, verbose=False, tracing=False)
    return str(crew.kickoff().raw)


def _parse(raw: str) -> TriageResult:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in the agent output")
    return TriageResult.model_validate_json(text[start:end + 1])


class TriageError(Exception):
    def __init__(self, message: str, attempts: int, raw: str | None):
        super().__init__(message)
        self.attempts = attempts
        self.raw = raw


def run(inc: dict) -> tuple[TriageResult, int, float]:
    """Run the triage agent for an incident. Returns (result, attempts, seconds)."""
    t0 = time.perf_counter()
    first = (
        "Triage this incident.\n\n" + _incident_block(inc) + "\n\n" + RULES +
        "\n\nReturn JSON with exactly these keys:\n" + SCHEMA_HINT
    )
    raw = _kickoff(first)
    try:
        return _parse(raw), 1, time.perf_counter() - t0
    except (ValueError, ValidationError) as e:
        error = str(e)

    retry = (
        "Your previous answer for this incident was not valid. Problem:\n" + error[:1500] +
        "\n\nPrevious answer:\n" + raw[:3000] +
        "\n\nThe incident again:\n" + _incident_block(inc) + "\n\n" + RULES +
        "\n\nReturn a corrected JSON object with exactly these keys:\n" + SCHEMA_HINT
    )
    raw2 = _kickoff(retry)
    try:
        return _parse(raw2), 2, time.perf_counter() - t0
    except (ValueError, ValidationError) as e:
        raise TriageError(f"invalid agent output after retry: {str(e)[:500]}", 2, raw2) from e
