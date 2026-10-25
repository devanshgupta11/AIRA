"""Shared plumbing for AIRA's Google ADK agents.

The project deliberately runs two agent frameworks: CrewAI for the investigators, ADK for the
reasoning agents. This module is the ADK counterpart of base.py and keeps the two honest about the
same things - the number-discipline rules, JSON extraction, the single corrective retry, and the
semantic `check` hook that rejects an answer contradicting its own evidence.

Both frameworks share ONE local model. The laptop has ~1.4 GB of spare VRAM against a 5.6 GB
resident model, so a second model is not an option; ADK reaches the same Ollama instance through
LiteLLM (google.adk.models.lite_llm.LiteLlm -> 'ollama_chat/<model>').

Constructing a LiteLlm agent costs ~19s the first time while LiteLLM initialises, and ~1s
afterwards, so agents are built once and cached rather than per call.
"""

import os
import time
import warnings
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .. import config  # noqa: F401  (sets telemetry env vars before frameworks import)
from ..config import OLLAMA_MODEL, OLLAMA_URL
from . import base
from .. import events, guardrails, http_util

# Keep ADK fully local: no Vertex lookups.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "0")
# OTEL_SDK_DISABLED is deliberately not set here either - see the note in config.py. It disabled
# AIRA's own tracing along with ADK's.

from google.adk.agents import Agent                     # noqa: E402
from google.adk.models.lite_llm import LiteLlm          # noqa: E402
from google.adk.runners import Runner                   # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types                          # noqa: E402

T = TypeVar("T", bound=BaseModel)

APP_NAME = "aira"
USER_ID = "aira"

_agents: dict[str, Agent] = {}
_runners: dict[str, Runner] = {}


def _loop_key() -> int:
    """Identifies the running event loop, or 0 outside one.

    A Runner's session service and an A2A client both hold state bound to the loop that created
    them, so an object cached under one loop fails with "Event loop is closed" under the next. The
    backend has a single long-lived loop and never sees this; a script calling asyncio.run() more
    than once does. Caching per loop keeps the expensive part - LiteLLM's initialisation inside the
    Agent, which is cached by name alone - shared regardless.
    """
    import asyncio

    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def make_adk_agent(name: str, instruction: str) -> Agent:
    """An ADK agent on the shared local model. Cached: rebuilding one re-initialises LiteLLM."""
    if name not in _agents:
        _agents[name] = Agent(
            name=name,
            model=LiteLlm(model=f"ollama_chat/{OLLAMA_MODEL}", api_base=OLLAMA_URL, temperature=0.1),
            instruction=instruction,
            # ADK's own observation hooks rather than a wrapper of our own. They record and return
            # None, so they never substitute a response.
            **events.adk_callbacks(),
        )
    return _agents[name]


def _runner(agent: Agent) -> Runner:
    key = f"{agent.name}:{_loop_key()}"
    if key not in _runners:
        _runners[key] = Runner(
            agent=agent, app_name=APP_NAME, session_service=InMemorySessionService()
        )
    return _runners[key]


def pick_answer(events: list[tuple[str, bool]]) -> str:
    """The agent's answer, from a list of (text, is_final_response) pairs in arrival order.

    Over A2A the same text arrives twice - once as a message event and again in the final one - so
    concatenating every event returned the whole answer doubled (measured 2026-10-23:
    '{"ok": true}{"ok": true}'). base.parse() takes the first balanced JSON object and therefore
    still worked, which is exactly why this went unnoticed; the retry prompt, which quotes the
    previous answer back to the model, did not.

    The last final-response event wins. Concatenation is kept only as the fallback for genuinely
    streamed output, where each event carries an increment rather than the whole answer.
    """
    texts = [t for t, _ in events if t]
    finals = [t for t, is_final in events if t and is_final]
    return (finals[-1] if finals else "".join(texts)).strip()


async def _ask(agent: Agent, prompt: str) -> str:
    """One turn. A fresh session each time: incidents are independent and must not leak context.

    The events are collected in arrival order and reduced by pick_answer(), which explains why the
    final event wins rather than every event being concatenated.
    """
    runner = _runner(agent)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    def _text(event) -> str:
        parts = getattr(getattr(event, "content", None), "parts", None) or []
        return "".join(p.text for p in parts if getattr(p, "text", None))

    def _is_final(event) -> bool:
        fn = getattr(event, "is_final_response", None)
        return bool(callable(fn) and fn())

    events: list[tuple[str, bool]] = []
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session.id, new_message=message
    ):
        text = _text(event)
        if text:
            events.append((text, _is_final(event)))
    return pick_answer(events)


RCA_A2A_CARD = os.getenv(
    "AIRA_RCA_A2A_CARD", "http://127.0.0.1:8001/.well-known/agent-card.json")


def a2a_proxy(name: str, card_url: str = RCA_A2A_CARD):
    """A local stand-in for an agent that actually lives behind an A2A endpoint.

    RemoteA2aAgent is ADK's own A2A client: it resolves the remote agent's card, speaks the
    protocol and converts messages both ways. Calling through it means the CrewAI side genuinely
    crosses a protocol boundary to reach the ADK agent rather than importing it - which is the
    point of running two frameworks.
    """
    from google.adk.a2a.agent import RemoteA2aAgent  # imported lazily: only needed when A2A is used

    # ADK labels its own A2A classes experimental and warns on every call. The warning is real and
    # noted in the docs - ADK's implementation may change, though the A2A protocol itself is
    # stable - but repeating it per message buries the logs.
    warnings.filterwarnings("ignore", message=r".*\[EXPERIMENTAL\].*", category=UserWarning)

    key = f"remote:{name}:{_loop_key()}"
    if key not in _agents:
        # Our own httpx client, so every A2A request carries W3C trace context. Without it the RCA
        # agent's work in its own process is invisible to the investigation's trace - the one hop
        # the event recorder cannot correlate, because that process has no incident id.
        _agents[key] = RemoteA2aAgent(
            name=name, agent_card=card_url, timeout=180.0,
            httpx_client=http_util.async_client(timeout=180.0))
    return _agents[key]


async def ask_a2a(prompt: str, name: str = "aira_rca_remote",
                  card_url: str = RCA_A2A_CARD) -> str:
    """Send one prompt to a remote agent over A2A and return its text."""
    return await _ask(a2a_proxy(name, card_url), prompt)


async def a2a_available(card_url: str = RCA_A2A_CARD, timeout: float = 4.0) -> bool:
    """Is the A2A peer reachable? Used by /health so a dead peer is reported, not hidden."""
    from .. import http_util

    try:
        async with http_util.async_client(timeout=timeout) as c:
            return (await c.get(card_url)).status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def run_adk_agent(agent: Agent, prompt: str, hint: str, model_cls: type[T], context: str,
                        check=None) -> tuple[T, int, float]:
    """Run an ADK agent with one corrective retry. Returns (result, attempts, seconds).

    Mirrors base.run_agent so both frameworks behave identically from the caller's side:
    `context` is re-sent on the retry so the model still has the evidence next to its mistake, and
    `check` raises ValueError when the output is well-formed but contradicts that evidence - the
    class of mistake prompting alone has repeatedly failed to prevent.
    """
    t0 = time.perf_counter()
    raw = await _ask(agent, prompt)
    try:
        result = base.parse(raw, model_cls)
        if check:
            check(result)
        return result, 1, time.perf_counter() - t0
    except (ValueError, ValidationError) as e:
        error = str(e)
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
    raw2 = await _ask(agent, retry)
    try:
        result = base.parse(raw2, model_cls)
        if check:
            check(result)
        events.record("aira", "retry_succeeded", ok=True,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        return result, 2, time.perf_counter() - t0
    except (ValueError, ValidationError) as e:
        events.record("aira", "retry_failed", ok=False,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      detail={"schema": model_cls.__name__, "error": events.clip(str(e), 600)})
        raise base.AgentError(f"invalid agent output after retry: {str(e)[:500]}", 2, raw2) from e


async def warm() -> float:
    """Pay LiteLLM's one-off initialisation cost up front, so the first real incident does not.

    Returns the seconds taken. Safe to call repeatedly.
    """
    t0 = time.perf_counter()
    agent = make_adk_agent("warmup", "Reply with one word.")
    await _ask(agent, "Reply with exactly: ready")
    return time.perf_counter() - t0


def findings_block(inc: dict, findings: list[dict]) -> str:
    """Every investigator's conclusions, formatted as the reasoning agents' shared input.

    This is the shared incident context made readable: who looked at what, and what they concluded.
    Skipped agents are included deliberately - knowing the Log agent was never run is relevant to
    judging whether the evidence is sufficient.
    """
    # An investigator's finding can quote a log line verbatim - that is the point of the quote -
    # so hostile text can reach the RCA agent second-hand, through the findings rather than through
    # the evidence. The assembled block is fenced for the same reason the investigators' is.
    lines = [base.ENVIRONMENT, "ALERT",
             f"- alertname: {inc['alertname']}",
             f"- service: {inc.get('service')}",
             f"- severity: {inc.get('severity')}",
             f"- started at: {inc['starts_at']}",
             f"- description: {base.clarify_threshold(inc.get('description'))}",
             "", "WHAT EACH AGENT FOUND"]

    for f in sorted(findings, key=lambda x: (x.get("round") or 1, x.get("seq") or 0)):
        agent = f["agent"]
        if agent in ("rca", "critic", "conclusion"):
            continue                       # the reasoning agents and their own verdict are not evidence
        rnd = f.get("round") or 1
        tag = f"{agent} agent" + (f" (round {rnd})" if rnd > 1 else "")

        if f["status"] == "skipped":
            lines.append(f"- {tag}: NOT RUN - {f.get('skip_reason') or 'no reason recorded'}")
            continue
        if f["status"] == "error":
            lines.append(f"- {tag}: FAILED - {f.get('error') or 'unknown error'}")
            continue
        out = f.get("output") or {}
        if not out:
            continue

        lines.append(f"- {tag}:")
        if out.get("summary"):
            lines.append(f"    {out['summary']}")
            if out.get("likely_cause_category"):
                lines.append(f"    category: {out['likely_cause_category']}"
                             f" (confidence {out.get('confidence')})")
        for item in out.get("findings", []):
            mark = ""
            if item.get("likely_cause"):
                mark = "  [THIS AGENT FLAGGED THIS AS THE LIKELY CAUSE]"
            elif item.get("related_to_alert"):
                mark = "  [related to the alert]"
            elif item.get("abnormal"):
                mark = "  [abnormal]"
            # A finding's statement, and especially the evidence it quotes, can carry text an
            # application wrote. Defanged in place rather than fenced: fencing every quote would
            # bury the findings in markers.
            statement = guardrails.defang_inline(
                item.get("statement", ""), f"{agent} agent finding", agent=agent)
            lines.append(f"    * {statement}{mark}")
            src = item.get("measurement") or item.get("quoted_line") or item.get("detail")
            if src and str(src).lower() != "none":
                quoted = guardrails.defang_inline(
                    str(src)[:200], f"{agent} agent quoted evidence", agent=agent)
                lines.append(f"      evidence: {quoted}")

    return "\n".join(lines)
