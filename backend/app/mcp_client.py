"""MCP client: how the investigation reaches its evidence tools.

The three `*_tool.py` modules used to be imported and called directly. They are now reached over
the Model Context Protocol, so a tool is a service with a declared interface rather than a function
in this process - which is the point of the requirement, and also what makes the tools reusable by
anything else that speaks MCP.

Two deliberate choices, both about not corrupting evidence:

* **The server is an exact passthrough and validation happens here.** The server returns each
  tool's dict unchanged, and this module validates it against the shared models in `mcp_schema`
  before it reaches a prompt. Having the server return a Pydantic model instead would publish an
  `outputSchema` over the protocol, which is tidier, but it would also re-serialise the payload
  (observed: optional keys the tool omits come back as explicit nulls). Swapping a function call
  for a protocol call must not change what an agent sees, so the payload is passed through
  untouched and the contract is enforced on arrival.
* **A failure is recorded, never hidden.** If the server is unreachable or returns the wrong shape,
  `McpUnavailable` is raised with the reason. The caller falls back to the direct call so the
  investigation still completes, and records that MCP was bypassed - losing evidence silently would
  be worse than either.
"""

import json
import os
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from . import events, http_util, mcp_schema, tracing

T = TypeVar("T", bound=BaseModel)

MCP_TOOLS_URL = os.getenv("AIRA_MCP_TOOLS_URL", "http://127.0.0.1:8002/mcp")

PROMETHEUS_TOOL = "collect_prometheus_evidence"
LOG_TOOL = "collect_log_evidence"
CHANGE_TOOL = "collect_change_evidence"


class McpUnavailable(Exception):
    """A tool could not be reached, or returned something that is not valid evidence."""


async def _call(tool: str, args: dict, model: type[T]) -> dict:
    """Call one MCP tool and return its payload, validated but otherwise untouched.

    A session per call. The transport is local HTTP and the measured setup cost is small next to a
    Prometheus query, while a long-lived session would have to be owned, health-checked and
    reconnected - state this module deliberately does not keep.
    """
    t0 = time.perf_counter()
    events.record("aira", "tool_call_started", detail={"tool": tool, "args": args,
                                                       "transport": "mcp/streamable-http"})
    span_cm = tracing.span(f"mcp:{tool}", **{"aira.tool": tool, "aira.transport": "streamable-http",
                                             "aira.mcp_url": MCP_TOOLS_URL})
    span_cm.__enter__()
    try:
        return await _call_traced(tool, args, model, t0)
    except Exception as e:  # noqa: BLE001 - marked on the span, then re-raised untouched
        tracing.record_exception(e)
        raise
    finally:
        span_cm.__exit__(None, None, None)


async def _call_traced(tool: str, args: dict, model: type[T], t0: float) -> dict:
    """The call itself. Split out so the span and the event wrap it without nesting try blocks.

    Imported here rather than at module scope: the backend imports this module during startup and
    the MCP client stack pulls in a fair amount of machinery only needed on first use.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(
            MCP_TOOLS_URL, httpx_client_factory=http_util.mcp_http_client_factory
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
    except Exception as e:  # noqa: BLE001 - every transport failure reads the same to the caller
        events.record("aira", "tool_call_failed", ok=False,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      detail={"tool": tool, "error": _why(e)})
        raise McpUnavailable(f"{tool} unreachable at {MCP_TOOLS_URL}: {_why(e)}") from e

    if result.isError:
        detail = getattr(result.content[0], "text", "") if result.content else ""
        events.record("aira", "tool_call_failed", ok=False,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      detail={"tool": tool, "error": f"tool reported an error: {detail[:300]}"})
        raise McpUnavailable(f"{tool} returned an error: {detail[:300]}")

    payload = _payload(result, tool)
    try:
        model.model_validate(payload)
    except ValidationError as e:
        # The shape is the contract. An unexpected shape reaching a prompt is how a compromised or
        # simply broken tool would corrupt an investigation, so it is refused here.
        events.record("aira", "tool_return_rejected", ok=False,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      detail={"tool": tool, "error": str(e)[:400],
                              "guardrail": "tool-return shape validation"})
        raise McpUnavailable(
            f"{tool} returned something that is not valid evidence: {str(e)[:300]}"
        ) from e

    latency = round((time.perf_counter() - t0) * 1000, 1)
    payload["_mcp"] = {
        "tool": tool,
        "transport": "streamable-http",
        "url": MCP_TOOLS_URL,
        "latency_ms": latency,
    }
    events.record("aira", "tool_call_completed", ok=True, duration_ms=latency,
                  detail={"tool": tool, "validated_as": model.__name__,
                          "bytes": len(json.dumps(payload, default=str))})
    return payload


def _why(e: BaseException, depth: int = 0) -> str:
    """The real reason behind an MCP transport failure.

    The client runs its transport in an anyio task group, so a refused connection surfaces as
    "ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)" - which is what gets
    recorded on the incident, and says nothing. Unwrapping to the innermost cause turns that into
    "ConnectError: All connection attempts failed", which is the thing someone reading the console
    actually needs.
    """
    inner = getattr(e, "exceptions", None)
    if inner and depth < 5:
        return " / ".join(_why(x, depth + 1) for x in inner[:3])
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _payload(result, tool: str) -> dict:
    """The tool's dict, from structured content when the server publishes a schema, else the text.

    FastMCP only emits `structuredContent` for a tool with a typed return. Ours return plain dicts
    on purpose (see the module docstring), so in practice this reads the JSON text part - but both
    are handled, because that is a server-side detail the client should not depend on.
    """
    if getattr(result, "structuredContent", None):
        return dict(result.structuredContent)
    if not result.content:
        raise McpUnavailable(f"{tool} returned no content at all")
    text = getattr(result.content[0], "text", None)
    if not text:
        raise McpUnavailable(f"{tool} returned a {type(result.content[0]).__name__} with no text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise McpUnavailable(f"{tool} returned text that is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise McpUnavailable(f"{tool} returned {type(data).__name__}, expected an object")
    return data


async def collect_prometheus(service: str) -> dict:
    """Live Prometheus evidence for one service, over MCP. Raises McpUnavailable on any failure."""
    return await _call(PROMETHEUS_TOOL, {"service": service}, mcp_schema.PrometheusEvidence)


async def collect_log(service: str, starts_at: str) -> dict:
    """Log evidence for one service, over MCP. Raises McpUnavailable on any failure.

    This is the payload carrying text written by application code, so it is the one that most needs
    the shape enforced on arrival: a log line is free text, but the structure around it is not.
    """
    return await _call(LOG_TOOL, {"service": service, "starts_at": starts_at},
                       mcp_schema.LogEvidence)


async def collect_change(service: str, starts_at: str) -> dict:
    """Configuration and deployment evidence for one service, over MCP."""
    return await _call(CHANGE_TOOL, {"service": service, "starts_at": starts_at},
                       mcp_schema.ChangeEvidence)


async def available() -> bool:
    """Is the tools server reachable and serving its tool list? Used by /health and status.ps1."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(
            MCP_TOOLS_URL, httpx_client_factory=http_util.mcp_http_client_factory
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return bool((await session.list_tools()).tools)
    except Exception:  # noqa: BLE001
        return False


async def tool_names() -> list[str]:
    """The tools the server actually advertises, for an honest health report."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        MCP_TOOLS_URL, httpx_client_factory=http_util.mcp_http_client_factory
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return [t.name for t in (await session.list_tools()).tools]
