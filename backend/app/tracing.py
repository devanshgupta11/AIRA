"""Distributed tracing: how a conclusion is traced back to what produced it.

The `agent_events` table already records the execution flow, which answers "what happened, in
order". A trace answers a different question: how the work *nests*, across process boundaries -
which tool call belongs to which agent, and which agent belongs to which investigation - including
the parts that happen in the MCP tools server and the RCA agent's own process, where AIRA's event
recorder has no incident context at all.

**Where the spans go, and why.** Not LangFuse. Self-hosting it needs Postgres, ClickHouse, Redis,
MinIO and a web tier - roughly 3-4 GB against the 2.7 GB free on this machine - and its cloud
offering would send every prompt, including real log lines from the monitored services, off the
machine, which contradicts the whole point of running a local model. Jaeger is already running in
this stack, it is an open-source tracing backend, and the OpenTelemetry SDK was already installed.
So AIRA exports to the collector the demo services already export to, and its spans appear in the
same Jaeger UI beside theirs - the investigation and the incident it investigated, in one view.

The collector's OTLP port is published on a random host port, so it is discovered the same way
`log_tool` discovers OpenSearch's. `AIRA_OTLP_ENDPOINT` overrides it.

**Tracing must never be able to break an investigation.** Spans are exported on a background
thread by `BatchSpanProcessor`, so a slow or dead collector costs the investigation nothing; if the
endpoint cannot be found at startup, tracing is simply off and says so on `/health`. Every function
here is safe to call whether or not it was initialised.
"""

import logging
import os
import subprocess
import time

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

log = logging.getLogger("aira.tracing")

SERVICE_NAME = os.getenv("AIRA_TRACE_SERVICE_NAME", "aira")

# Spans the a2a-sdk emits for its own internal event queue. One RCA call produced 66 of them -
# enqueue_event, _deliver_to_sink, dequeue_event, task_done - against the 6 spans that say what
# actually happened, which made the trace useless to read. They are dropped at export rather than
# by disabling instrumentation, because the same libraries also emit the spans worth keeping
# (invoke_agent, call_llm, generate_content).
NOISY_SPAN_PREFIXES = (
    "a2a.server.events.event_queue",
    "a2a.client.transports",
    "a2a.server.tasks.task_manager",
    "a2a.server.request_handlers",
)


class DropNoise(SpanProcessor):
    """Forwards spans to the real processor unless their name is known plumbing."""

    def __init__(self, inner: SpanProcessor):
        self.inner = inner

    def on_start(self, span, parent_context=None):
        self.inner.on_start(span, parent_context)

    def on_end(self, span):
        if span.name.startswith(NOISY_SPAN_PREFIXES):
            return
        self.inner.on_end(span)

    def shutdown(self):
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self.inner.force_flush(timeout_millis)


_state: dict = {"on": False, "endpoint": None, "reason": "not initialised"}
_provider: TracerProvider | None = None
_processor = None          # the live export processor, replaced when the collector moves
_last_retarget_check = 0.0
RETARGET_EVERY_S = 60.0


def discover_endpoint() -> str | None:
    """The collector's OTLP/HTTP endpoint, from the environment or from Docker."""
    explicit = os.getenv("AIRA_OTLP_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    try:
        out = subprocess.run(["docker", "port", "otel-collector", "4318"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        port = out.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        return f"http://localhost:{port}"
    except Exception:  # noqa: BLE001
        return None


def init(service_name: str = SERVICE_NAME) -> dict:
    """Start exporting spans. Idempotent; returns the state for /health to report."""
    global _provider
    if _state["on"]:
        return dict(_state)

    endpoint = discover_endpoint()
    if not endpoint:
        _state.update(on=False, endpoint=None,
                      reason="OTLP endpoint not found (is the otel-collector container running?)")
        log.warning("tracing is OFF: %s", _state["reason"])
        return dict(_state)

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider = TracerProvider(resource=Resource.create({
            "service.name": service_name,
            "service.namespace": "aira",
            # Marks these spans as the investigator rather than the investigated, so they are easy
            # to separate from the demo services' own spans in the same Jaeger instance.
            "aira.role": "incident-response",
        }))
        # Background export: the investigation never waits for the collector.
        global _processor
        _processor = DropNoise(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", timeout=5)))
        provider.add_span_processor(_processor)
        trace.set_tracer_provider(provider)
        _provider = provider
        # Prove a span actually records before claiming tracing is on. Setting up an exporter
        # succeeds even when the SDK is disabled for the process (OTEL_SDK_DISABLED), in which case
        # every span is silently a no-op - which is exactly how this layer came to report itself
        # healthy while recording nothing.
        with provider.get_tracer("aira.selftest").start_as_current_span("selftest") as probe:
            recording = bool(probe.get_span_context().trace_id)
        if not recording:
            _state.update(on=False, endpoint=endpoint,
                          reason="spans are not being recorded; is OTEL_SDK_DISABLED set?")
            log.warning("tracing is OFF: %s", _state["reason"])
            return dict(_state)
        _state.update(on=True, endpoint=endpoint, reason="exporting to the OpenTelemetry collector")
        log.info("tracing ON: exporting spans to %s/v1/traces (visible in Jaeger)", endpoint)
    except Exception as e:  # noqa: BLE001 - never let telemetry setup stop the backend
        _state.update(on=False, endpoint=endpoint, reason=f"{type(e).__name__}: {e}")
        log.warning("tracing is OFF: %s", _state["reason"])
    return dict(_state)


def state() -> dict:
    return dict(_state)


def tracer():
    return trace.get_tracer("aira")


def current_trace_id() -> str | None:
    """The active trace as a hex id, which is what Jaeger's UI is addressed by."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or not ctx.trace_id:
        return None
    return format(ctx.trace_id, "032x")


def trace_url(trace_id: str | None) -> str | None:
    """Where a human can go and look at it.

    Jaeger's UI is served behind the demo's frontend-proxy on the fixed port 8080, unlike its OTLP
    port, so this link does not depend on a randomly published one.
    """
    if not trace_id:
        return None
    return f"http://localhost:8080/jaeger/ui/trace/{trace_id}"


def span(name: str, **attributes):
    """A span context manager that is safe whether or not tracing was initialised.

    When tracing is off, the SDK's no-op tracer is used and this costs essentially nothing - so
    call sites never need to ask whether tracing is on.
    """
    return tracer().start_as_current_span(
        name, kind=SpanKind.INTERNAL,
        attributes={k: v for k, v in attributes.items() if v is not None})


def record_exception(exc: BaseException) -> None:
    """Mark the current span as failed. Does nothing when tracing is off."""
    try:
        s = trace.get_current_span()
        s.record_exception(exc)
        s.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
    except Exception:  # noqa: BLE001
        pass


def set_attributes(**attributes) -> None:
    try:
        s = trace.get_current_span()
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v)
    except Exception:  # noqa: BLE001
        pass


def headers_for_outgoing() -> dict:
    """W3C trace-context headers for an outgoing HTTP call.

    This is what joins the other processes to the same trace. AIRA's tools run behind MCP and its
    RCA agent behind A2A, both over HTTP, so injecting `traceparent` means their spans - and in the
    RCA agent's case, its own model call - become children of the investigation that caused them.
    It is also the clean answer to a gap left open in the callback layer: the RCA agent's process
    has no incident id, but it does have the trace.
    """
    carrier: dict = {}
    try:
        propagate.inject(carrier)
    except Exception:  # noqa: BLE001
        return {}
    return carrier


def context_from_headers(headers) -> object | None:
    """The parent context carried by an incoming request's headers, if any."""
    try:
        return propagate.extract(dict(headers))
    except Exception:  # noqa: BLE001
        return None


def attach(ctx) -> object | None:
    try:
        return otel_context.attach(ctx)
    except Exception:  # noqa: BLE001
        return None


def detach(token) -> None:
    try:
        if token is not None:
            otel_context.detach(token)
    except Exception:  # noqa: BLE001
        pass


def shutdown() -> None:
    """Flush whatever is buffered. Called when the backend stops."""
    try:
        if _provider is not None:
            _provider.shutdown()
    except Exception:  # noqa: BLE001
        pass


class TraceMiddleware:
    """Minimal ASGI middleware: continue an incoming trace instead of starting a new one.

    AIRA's tools server and RCA agent are separate processes reached over HTTP, so without this
    their spans would form unrelated traces and the investigation's trace would stop at the process
    boundary - exactly where the interesting part happens.

    Hand-written rather than installed: `opentelemetry-instrumentation-asgi` would do this and
    more, but the last dependency added to this project silently downgraded six packages, and what
    is needed here is small enough to read in one sitting. It extracts the W3C context from the
    request headers, opens one server span, and gets out of the way.
    """

    def __init__(self, app, name: str = "request"):
        self.app = app
        self.name = name

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        maybe_retarget()
        headers = {k.decode("latin-1"): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        token = attach(context_from_headers(headers))
        try:
            route = scope.get("path") or "/"
            with tracer().start_as_current_span(
                f"{self.name} {scope.get('method', 'GET')} {route}",
                kind=SpanKind.SERVER,
                attributes={"http.request.method": scope.get("method"),
                            "url.path": route},
            ):
                await self.app(scope, receive, send)
        finally:
            detach(token)


async def endpoint_reachable(timeout: float = 2.0) -> bool:
    """Is the collector actually accepting spans right now?

    `state()` is captured when tracing initialises and does not change afterwards, so on its own it
    would keep reporting "exporting to the collector" long after the collector had gone - observed
    while proving graceful degradation: the investigation carried on correctly, but health still
    claimed tracing was fine while every batch was being dropped. Health has to probe.

    A GET to an OTLP ingest path is not a valid OTLP request, so any HTTP answer at all means the
    collector is there; only a connection failure means it is not.
    """
    if not _state.get("on") or not _state.get("endpoint"):
        return False
    try:
        from . import http_util

        async with http_util.async_client(timeout=timeout) as c:
            await c.get(f"{_state['endpoint']}/v1/traces")
        return True
    except Exception:  # noqa: BLE001
        return False


def retarget_if_moved() -> bool:
    """Re-point the exporter when the collector comes back on a different port.

    The collector's OTLP port is published to a random host port, and `docker start` assigns a new
    one - so after the collector is restarted, the endpoint discovered at startup is dead for good
    and every span is dropped silently. Observed while testing graceful degradation: tracing
    recovered on the collector's side but AIRA kept posting to the old port.

    A provider's processors can be added after the fact, so the fix is to attach a processor
    pointing at the new endpoint and shut the stale one down. Returns whether it re-pointed.
    """
    global _processor
    if not _state.get("on") or _provider is None:
        return False
    found = discover_endpoint()
    if not found or found == _state.get("endpoint"):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        stale, _processor = _processor, DropNoise(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{found}/v1/traces", timeout=5)))
        _provider.add_span_processor(_processor)
        old = _state.get("endpoint")
        _state.update(endpoint=found)
        try:
            if stale is not None:
                stale.shutdown()
        except Exception:  # noqa: BLE001
            pass
        log.info("tracing re-pointed: the collector moved from %s to %s", old, found)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("could not re-point tracing at %s: %s", found, e)
        return False


def maybe_retarget() -> None:
    """Re-point at the collector if it has moved, at most once a minute.

    `retarget_if_moved` was first called only from the backend's /health handler, which fixed the
    backend and nothing else: the MCP tools server and the RCA agent never serve that endpoint, so
    after a collector restart they kept posting to the dead port and their spans vanished from the
    trace. Observed directly - a trace that had spanned three processes came back with one, and
    rca.err.log was full of "Failed to export spans batch".

    Cheap enough to call per request because of the throttle, and the `docker port` lookup only
    happens when the throttle lets it through.
    """
    global _last_retarget_check
    if not _state.get("on"):
        return
    now = time.monotonic()
    if now - _last_retarget_check < RETARGET_EVERY_S:
        return
    _last_retarget_check = now
    retarget_if_moved()
