"""Monitoring callbacks: the execution trace of an investigation, as it happens.

`agent_findings` records what each agent *concluded*. This records the *flow* that produced it -
which agent started, what prompt the model was given and what it answered, how long a tool call
took and whether it crossed MCP, which outputs failed validation and were retried. When a
conclusion is wrong, the findings tell you what was decided and the events tell you why.

Both frameworks already have callback mechanisms, so nothing here invents one:

* **CrewAI** publishes typed events on a global bus (`crewai.events.crewai_event_bus`) covering
  agent execution, task execution and every LLM call, each carrying the prompt, the response and
  token usage. We subscribe once at startup.
* **ADK** takes callbacks per agent (`before_model_callback`, `after_model_callback`,
  `on_model_error_callback`, and the agent/tool equivalents), supplied where the agent is built.

What neither framework can report is what happens outside it - our MCP tool calls, and the
validation-and-retry loop in `base.run_agent` - so those are recorded directly.

**How an event knows which incident it belongs to.** CrewAI's bus is global and its events carry no
incident id, so the orchestrator declares the current incident and agent in a `ContextVar` before
running one. `asyncio.to_thread` copies the context into the worker thread, which is how the
investigators and the Critic are run, so the attribution follows them. Outside any incident - the
RCA agent's own process, a script, a test - there is no context, and an event is logged instead of
stored rather than being attributed to the wrong incident or inventing one.
"""

import contextlib
import contextvars
import json
import logging
import time

from . import db, tracing

log = logging.getLogger("aira.events")

# (incident_id, agent, round) for whatever is running in this context, or None outside an incident.
_current: contextvars.ContextVar[tuple[int, str, int] | None] = contextvars.ContextVar(
    "aira_event_context", default=None)

# Prompts and responses are the point of the trace, but they are also the largest thing in it, and
# this table is written on the hot path of every model call.
MAX_TEXT = 2000


@contextlib.contextmanager
def investigating(incident_id: int, agent: str, round_: int = 1, phase: str = "agent"):
    """Declare which incident and agent the events from this block belong to.

    This is also where the agent's span is opened. Every agent run already passes through here, so
    the event context and the trace span share one boundary rather than being wrapped separately
    and drifting apart.
    """
    token = _current.set((incident_id, agent, round_))
    try:
        with tracing.span(f"{phase}:{agent}", **{"aira.incident_id": incident_id,
                                                 "aira.agent": agent,
                                                 "aira.round": round_,
                                                 "aira.phase": phase}):
            yield
    finally:
        _current.reset(token)


@contextlib.contextmanager
def agent_call(agent: str, via: str):
    """Record an agent invocation that neither framework's callbacks can see.

    The RCA agent is the case this exists for. It runs in a separate process behind A2A, so
    CrewAI's bus never hears about it (it is an ADK agent) and ADK's own callbacks fire over there,
    where there is no incident context. Without this the trace showed the investigation jumping
    straight from the investigators to the Critic, which reads as though the RCA never ran -
    observed on incident 115, where it had in fact run for 8.5 s and written a finding.

    The `source` column stays honest about which mechanism reported an event, so an "aira" agent
    call next to "crewai" ones is information rather than inconsistency.
    """
    t0 = time.perf_counter()
    record("aira", "agent_call_started", agent=agent, detail={"via": via})
    try:
        yield
    except Exception as e:  # noqa: BLE001 - recorded, then re-raised untouched
        record("aira", "agent_call_failed", agent=agent, ok=False,
               duration_ms=round((time.perf_counter() - t0) * 1000, 1),
               detail={"via": via, "error": clip(f"{type(e).__name__}: {e}", 600)})
        raise
    record("aira", "agent_call_completed", agent=agent, ok=True,
           duration_ms=round((time.perf_counter() - t0) * 1000, 1), detail={"via": via})


def context() -> tuple[int, str, int] | None:
    return _current.get()


def record(source: str, kind: str, *, agent: str | None = None, duration_ms: float | None = None,
           ok: bool | None = None, detail: dict | None = None) -> None:
    """Record one event against the incident currently being investigated.

    Never raises. A failure to record telemetry must not fail an investigation, which is the whole
    reason this is monitoring rather than logic.
    """
    ctx = _current.get()
    try:
        if ctx is None:
            log.debug("event outside any incident: %s/%s %s", source, kind, agent or "")
            return
        incident_id, ctx_agent, round_ = ctx
        db.add_event(incident_id, source, kind, agent=agent or ctx_agent, round_=round_,
                     duration_ms=duration_ms, ok=ok, detail=detail)
    except Exception:  # noqa: BLE001
        log.exception("could not record event %s/%s", source, kind)


def clip(value, limit: int = MAX_TEXT) -> str:
    """A string short enough to store, with the truncation made visible rather than silent."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text)} chars total]"


# A model call's span has to live between two separate callbacks - CrewAI reports the start and
# the completion as different events - so the span and its context token are held by call_id until
# the matching completion arrives. A call that never completes leaves an entry behind, so the map
# is bounded: a stuck span is a leak, not a crash, and dropping the oldest is better than growing.
_model_spans: dict = {}
MAX_OPEN_SPANS = 64


def _open_model_span(call_id: str, model, prompt) -> None:
    try:
        if len(_model_spans) >= MAX_OPEN_SPANS:
            oldest = next(iter(_model_spans))
            stale_span, stale_token = _model_spans.pop(oldest)
            tracing.detach(stale_token)
            stale_span.end()
        cm = tracing.span("model_call", **{"aira.model": model,
                                           "aira.prompt_chars": len(clip(prompt, 10 ** 7))})
        span = cm.__enter__()
        _model_spans[call_id] = (span, cm)
    except Exception:  # noqa: BLE001
        log.debug("could not open a model-call span", exc_info=True)


def _close_model_span(call_id: str, response=None, usage=None, error=None) -> None:
    entry = _model_spans.pop(call_id, None)
    if entry is None:
        return
    span, cm = entry
    try:
        if error:
            span.set_attribute("aira.error", clip(error, 300))
            tracing.record_exception(RuntimeError(clip(error, 300)))
        else:
            if usage:
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if usage.get(key) is not None:
                        span.set_attribute(f"aira.{key}", usage[key])
            span.set_attribute("aira.response_chars", len(clip(response, 10 ** 7)))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- CrewAI's event bus

_crewai_wired = False
_llm_started_at: dict[str, float] = {}


def wire_crewai() -> bool:
    """Subscribe to CrewAI's event bus. Idempotent; returns whether it is now wired.

    Registered once per process, at startup, because the bus is global - subscribing per agent run
    would add a duplicate handler for every incident.
    """
    global _crewai_wired
    if _crewai_wired:
        return True
    try:
        from crewai.events import crewai_event_bus
        from crewai.events.types.agent_events import (AgentExecutionCompletedEvent,
                                                      AgentExecutionErrorEvent,
                                                      AgentExecutionStartedEvent)
        from crewai.events.types.llm_events import (LLMCallCompletedEvent, LLMCallFailedEvent,
                                                    LLMCallStartedEvent)
    except Exception:  # noqa: BLE001 - a CrewAI version without the bus must not break startup
        log.warning("CrewAI event bus unavailable; agent-level callbacks will not be recorded")
        return False

    @crewai_event_bus.on(AgentExecutionStartedEvent)
    def _agent_started(_source, event):
        record("crewai", "agent_started",
               detail={"role": getattr(getattr(event, "agent", None), "role", None),
                       "prompt": clip(getattr(event, "task_prompt", None))})

    @crewai_event_bus.on(AgentExecutionCompletedEvent)
    def _agent_completed(_source, event):
        record("crewai", "agent_completed", ok=True,
               detail={"output": clip(getattr(event, "output", None))})

    @crewai_event_bus.on(AgentExecutionErrorEvent)
    def _agent_error(_source, event):
        record("crewai", "agent_error", ok=False,
               detail={"error": clip(getattr(event, "error", None), 600)})

    @crewai_event_bus.on(LLMCallStartedEvent)
    def _llm_started(_source, event):
        call_id = getattr(event, "call_id", None)
        if call_id:
            _llm_started_at[call_id] = time.perf_counter()
            _open_model_span(call_id, getattr(event, "model", None),
                             getattr(event, "messages", None))
        record("crewai", "model_call_started",
               detail={"model": getattr(event, "model", None),
                       "temperature": getattr(event, "temperature", None),
                       "prompt": clip(getattr(event, "messages", None))})

    @crewai_event_bus.on(LLMCallCompletedEvent)
    def _llm_completed(_source, event):
        call_id = getattr(event, "call_id", None)
        started = _llm_started_at.pop(call_id, None) if call_id else None
        usage = getattr(event, "usage", None) or {}
        _close_model_span(call_id, response=getattr(event, "response", None), usage=usage)
        record("crewai", "model_call_completed", ok=True,
               duration_ms=None if started is None else round((time.perf_counter() - started) * 1000, 1),
               detail={"model": getattr(event, "model", None),
                       "response": clip(getattr(event, "response", None)),
                       "finish_reason": getattr(event, "finish_reason", None),
                       "tokens": {k: usage.get(k) for k in
                                  ("prompt_tokens", "completion_tokens", "total_tokens")
                                  if k in usage}})

    @crewai_event_bus.on(LLMCallFailedEvent)
    def _llm_failed(_source, event):
        call_id = getattr(event, "call_id", None)
        if call_id:
            _llm_started_at.pop(call_id, None)
            _close_model_span(call_id, error=getattr(event, "error", None))
        record("crewai", "model_call_failed", ok=False,
               detail={"model": getattr(event, "model", None),
                       "error": clip(getattr(event, "error", None), 600)})

    _crewai_wired = True
    log.info("CrewAI event bus wired: agent execution and LLM calls are recorded per incident")
    return True


# ---------------------------------------------------------------- ADK's per-agent callbacks

def adk_callbacks() -> dict:
    """ADK callback keyword arguments, to pass to `Agent(...)`.

    Each returns None, which tells ADK to carry on normally - these observe, they never substitute
    a response. The tool callbacks are deliberately absent: the RCA agent has no tools, so
    registering them would advertise a hook that can never fire.
    """
    started: dict[int, float] = {}

    def before_model(ctx, request):
        try:
            parts = []
            for content in (getattr(request, "contents", None) or []):
                for part in (getattr(content, "parts", None) or []):
                    if getattr(part, "text", None):
                        parts.append(part.text)
            started[id(ctx)] = time.perf_counter()
            record("adk", "model_call_started",
                   detail={"model": str(getattr(request, "model", None)),
                           "prompt": clip("\n".join(parts))})
        except Exception:  # noqa: BLE001
            log.debug("adk before_model callback failed", exc_info=True)
        return None

    def after_model(ctx, response):
        try:
            t0 = started.pop(id(ctx), None)
            text = ""
            content = getattr(response, "content", None)
            for part in (getattr(content, "parts", None) or []):
                if getattr(part, "text", None):
                    text += part.text
            record("adk", "model_call_completed", ok=True,
                   duration_ms=None if t0 is None else round((time.perf_counter() - t0) * 1000, 1),
                   detail={"response": clip(text)})
        except Exception:  # noqa: BLE001
            log.debug("adk after_model callback failed", exc_info=True)
        return None

    def on_model_error(ctx, request, error):
        try:
            started.pop(id(ctx), None)
            record("adk", "model_call_failed", ok=False,
                   detail={"error": clip(f"{type(error).__name__}: {error}", 600)})
        except Exception:  # noqa: BLE001
            log.debug("adk on_model_error callback failed", exc_info=True)
        return None

    return {"before_model_callback": before_model,
            "after_model_callback": after_model,
            "on_model_error_callback": on_model_error}
