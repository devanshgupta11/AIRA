"""Shared plumbing for every AIRA agent.

One local model serves all agents (the laptop has ~1.4 GB of free VRAM, so a second model will not
fit), and every agent follows the same contract: it reasons only over supplied evidence, returns
JSON validated against a Pydantic schema, and gets exactly one retry carrying the validation error
before the incident is given an honest error state.

The number-discipline rules below were derived from real failures observed on 2026-10-06:
the model blended two measured percentages into a third that existed nowhere, and it reported the
alert's threshold as the service's baseline. Both are addressed here so every agent inherits the fix.
"""

import json
import re
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .. import config  # noqa: F401  (sets CrewAI telemetry env vars before crewai is imported)
from .. import events, guardrails
from ..config import OLLAMA_MODEL, OLLAMA_URL
from crewai import LLM, Agent, Crew, Process, Task  # noqa: E402

T = TypeVar("T", bound=BaseModel)

NUMBER_RULES = """- Use ONLY the facts given below. Do not invent metrics, logs, deployments, error messages,
  root causes or numbers that are not present.
- Copy every number character-for-character from the text above. NEVER average, round, combine or
  recalculate them. The alert and the evidence are measured over different time windows at different
  moments, so they legitimately disagree: that is expected, and you must not reconcile them into a
  single figure. If you write a percentage, the exact same digits must appear in the text above.
- When you state a number, also state which measurement and window it came from, in the form
  "<metric> over <window> is <value>". Prefer quoting one specific measurement over summarising
  several. Any number appearing only in these instructions is a placeholder, never data.
- If a value is "unavailable", say it is unavailable; do not guess it.
- "threshold" and "baseline" are different things and must never be swapped. The threshold is the level
  that made the alert fire; the baseline is what this service actually measured 15-25 minutes ago.
- Reply with a single JSON object only. No markdown, no code fences, no text before or after it."""

ENVIRONMENT = (
    "ENVIRONMENT\n"
    "- The OpenTelemetry Demo (Astronomy Shop) microservices, each running as a Docker container\n"
    "  (Docker Compose, single host). There is no Kubernetes. Container name = service name.\n"
    "- Available data sources: Prometheus metrics, Jaeger traces, container logs in OpenSearch,\n"
    "  feature flags in flagd.\n"
)


class AgentError(Exception):
    """An agent failed to produce valid output even after its retry."""

    def __init__(self, message: str, attempts: int, raw: str | None):
        super().__init__(message)
        self.attempts = attempts
        self.raw = raw


def make_llm() -> LLM:
    """The single shared local model. CrewAI 1.15.23 routes 'ollama/<model>' to its native
    OpenAI-compatible provider against Ollama's /v1 endpoint."""
    return LLM(model=f"ollama/{OLLAMA_MODEL}", base_url=OLLAMA_URL, temperature=0.1, timeout=150)


def make_agent(role: str, goal: str, backstory: str) -> Agent:
    return Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        llm=make_llm(),
        allow_delegation=False,
        max_iter=2,
        verbose=False,
    )


def clarify_threshold(description: str | None) -> str:
    """Spell out what an alert's trigger level is.

    Alert descriptions end with "(threshold 5%)". Sitting next to the measured error percentage,
    that bare word was repeatedly misread as the service's baseline, producing "higher than the
    baseline of 5%" when the measured baseline was 4.3%. Naming it explicitly removes the ambiguity
    at the source instead of relying on the prompt alone.
    """
    if not description:
        return ""
    # Matches the word wherever it appears, not only when it is the whole parenthesis. The CPU rule
    # writes "(100% = one full core; threshold 100%)", which an earlier stricter pattern missed, and
    # the agent then reported the 100% trigger level as grafana's baseline.
    return re.sub(
        r"threshold ([^)\s;,]+)",
        r"the level that makes this alert fire is \1, which is the rule's trigger point and NOT a "
        r"measurement of this service",
        description,
    )


def incident_block(inc: dict, evidence_text: str | None = None, evidence_label: str = "EVIDENCE (live Prometheus queries)") -> str:
    """The alert and its evidence, formatted for any agent.

    evidence_text defaults to the incident's Prometheus evidence; investigator agents working from
    another source (logs, change history) pass their own.
    """
    labels = dict(inc.get("labels") or {})
    body = evidence_text if evidence_text is not None else inc.get("evidence_text")
    # Everything below this line came from outside AIRA - alert annotations written by Prometheus,
    # and evidence containing log lines written by application code. This is the single funnel
    # where that text becomes a prompt, so it is where it gets fenced and defanged.
    return (
        f"{ENVIRONMENT}\n"
        "ALERT\n"
        f"- alertname: {inc['alertname']}\n"
        f"- service: {inc.get('service')}\n"
        f"- severity: {inc.get('severity')}\n"
        f"- started at: {inc['starts_at']}\n"
        f"- summary: {inc.get('summary')}\n"
        f"- description: {clarify_threshold(inc.get('description'))}\n"
        f"- labels: {json.dumps(labels)}\n\n"
        f"{evidence_label}\n"
        + (guardrails.safe_for_prompt(body, evidence_label)
           if body else "No evidence could be collected.")
    )


def focus_block(focus: str | None) -> str:
    """A specific question for a second investigation round.

    When the Critic rejects an analysis it names what is missing. Re-running an investigator with
    the same prompt would mostly reproduce its first answer, so the gap is handed to it as a
    sharper question - that is what makes a second round worth the time it costs.
    """
    if not focus:
        return ""
    return (
        "\n\nTHIS IS A SECOND LOOK. A reviewer judged the first analysis unsupported and asked "
        f"specifically for:\n  {focus}\n"
        "Answer in the same format, but prioritise that question. If the evidence still does not "
        "address it, say so plainly rather than restating your earlier findings."
    )


def schema_hint(shape: dict) -> str:
    return json.dumps(shape, indent=2)


def _first_json_object(text: str) -> str | None:
    """The first complete, balanced {...} in the text.

    Spanning from the first '{' to the last '}' breaks whenever the model adds a closing remark
    after the JSON, or emits two objects - the slice then contains trailing characters and fails to
    parse. Larger schemas provoke that far more often. Braces inside string literals are ignored,
    so a quoted log line containing one cannot end the object early.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse(raw: str, model_cls: type[T]) -> T:
    """Extract and validate the JSON object from an agent's reply."""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    obj = _first_json_object(text)
    if obj is None:
        raise ValueError("no JSON object found in the agent output")
    return model_cls.model_validate_json(obj)


def kickoff(agent: Agent, description: str, hint: str) -> str:
    task = Task(
        description=description,
        expected_output="One JSON object matching this structure:\n" + hint,
        agent=agent,
    )
    crew = Crew(agents=[task.agent], tasks=[task], process=Process.sequential, verbose=False, tracing=False)
    return str(crew.kickoff().raw)


def run_agent(agent: Agent, prompt: str, hint: str, model_cls: type[T], context: str,
              check=None) -> tuple[T, int, float]:
    """Run one agent with a single corrective retry. Returns (result, attempts, seconds).

    `context` is re-sent on the retry so the model still has the evidence alongside its mistake.

    `check` is an optional callable taking the parsed result and raising ValueError when the output
    is well-formed but contradicts its own evidence. The schema cannot express facts like "a flag is
    listed as not off, so changes_found cannot be false", and prompting alone proved unreliable for
    exactly that class of mistake, so the retry is driven by a real check rather than wording.
    """
    t0 = time.perf_counter()
    raw = kickoff(agent, prompt, hint)
    try:
        result = parse(raw, model_cls)
        if check:
            check(result)
        return result, 1, time.perf_counter() - t0
    except (ValueError, ValidationError) as e:
        error = str(e)
        # Neither framework can report this: the answer came back fine as far as CrewAI is
        # concerned, and it is our schema and our semantic check that rejected it.
        events.record("aira", "output_validation_failed", ok=False,
                      detail={"schema": model_cls.__name__, "error": events.clip(error, 600),
                              "rejected_output": events.clip(raw, 800)})

    events.record("aira", "retry_started", detail={"schema": model_cls.__name__, "attempt": 2})
    retry = (
        "Your previous answer was not valid. Problem:\n" + error[:1500] +
        "\n\nPrevious answer:\n" + raw[:3000] +
        "\n\nThe task again:\n" + context +
        "\n\nReturn a corrected JSON object with exactly these keys:\n" + hint
    )
    raw2 = kickoff(agent, retry, hint)
    try:
        result = parse(raw2, model_cls)
        if check:
            check(result)
        events.record("aira", "retry_succeeded", ok=True,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        return result, 2, time.perf_counter() - t0
    except (ValueError, ValidationError) as e:
        events.record("aira", "retry_failed", ok=False,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      detail={"schema": model_cls.__name__, "error": events.clip(str(e), 600)})
        raise AgentError(f"invalid agent output after retry: {str(e)[:500]}", 2, raw2) from e
