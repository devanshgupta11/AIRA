"""AIRA's evidence tools, served over the Model Context Protocol.

The three `*_tool.py` modules already gather evidence deterministically with no LLM involved. This
server does not reimplement any of that - it exposes those exact functions as MCP tools, so the
investigation reaches them through a standard protocol instead of a Python import. Swapping the
transport must not change a single number, which is what the Step 1 verification checks.

**One server exposing several tools, not one server per tool.** They are all "read evidence about
one service", none needs to scale or fail independently, and they are only ever called together -
the orchestrator gathers all three at once. Sharing one process means one port in start-all.ps1,
one health check that proves all three are advertised, and one worker-thread pool through which
the three concurrent collections genuinely overlap (measured: 407 ms for all three, against 292 ms
for the same three called directly in parallel threads - the protocol costs about 115 ms per
investigation, roughly 0.4% of a 30-second run). Three servers would triple the process overhead
and the start-up surface for isolation that nothing here needs.

Run it the same way as the RCA A2A server, from the backend directory:

    python -m uvicorn app.mcp_servers.tools_server:app --host 127.0.0.1 --port 8002

`app` is the Starlette application FastMCP builds for the streamable-HTTP transport, so uvicorn
serves it exactly as it serves the backend itself.
"""

import functools

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from .. import change_tool, log_tool, prometheus_tool, tracing

HOST = "127.0.0.1"
PORT = 8002

# json_response: these tools are request/response, nothing streams, so a plain JSON body is both
#   simpler and inspectable with curl. It made no measurable difference to latency (tested), so the
#   choice is about clarity rather than speed.
# stateless_http: the client opens a fresh session per call, so there is no long-lived session state
#   for the server to keep. Reusing one session across calls was measured and is marginally faster
#   (337 ms against 390 ms for the three gathered together), but it would have to be owned,
#   health-checked and reconnected, and its context cannot be closed from a different task than
#   opened it - anyio raises "cancel scope in a different task". Fifty milliseconds on a
#   thirty-second investigation does not pay for that, and the handshake it avoids now costs 10 ms.
mcp = FastMCP("aira-tools", host=HOST, port=PORT, json_response=True, stateless_http=True)


async def _offload(fn, *args):
    """Run a blocking evidence tool in a worker thread.

    FastMCP calls a synchronous tool function directly on the server's event loop - read from
    `FuncMetadata.call_fn_with_arg_validation`: `if fn_is_async: await fn(...) else: fn(...)`. These
    tools block for 40-250 ms each on HTTP, OpenSearch and `docker` calls, so a sync tool would
    hold the whole server for that time and every tool call would serialise.

    Measured with sync tools: the three tools gathered concurrently took ~715 ms, the same as
    calling them one after another, because they were never actually concurrent. The orchestrator
    gathers all three at once, so that serialisation defeated the point of gathering in parallel.
    """
    return await anyio.to_thread.run_sync(functools.partial(fn, *args))


@mcp.tool(
    name="collect_prometheus_evidence",
    description=(
        "Collect live Prometheus metrics evidence for one service: request rate, error ratio, p95 "
        "latency, container CPU and memory, how long since the container last reported, and the "
        "worst failing operations - each with the same service's own baseline from 15-25 minutes "
        "ago. Returns the measured values, the exact PromQL used for each, and a text rendering "
        "written for an agent prompt. Values that cannot be measured are reported as null, never "
        "estimated."
    ),
)
async def collect_prometheus_evidence(service: str) -> dict:
    """Thin passthrough to prometheus_tool.collect, which owns the 13 queries and their baselines."""
    return await _offload(prometheus_tool.collect, service)


@mcp.tool(
    name="collect_log_evidence",
    description=(
        "Collect log evidence for one service around an incident, merged from OpenSearch and the "
        "container's stdout because neither covers the demo alone. Repeated messages are collapsed "
        "with an occurrence count, and lines containing error wording are separated from ordinary "
        "ones. When a service produced no logs at all this says so explicitly - an absence of "
        "data, not evidence that nothing went wrong. starts_at is the incident's start time as an "
        "ISO 8601 timestamp; the window searched is 5 minutes before to 10 minutes after it."
    ),
)
async def collect_log_evidence(service: str, starts_at: str) -> dict:
    """Thin passthrough to log_tool.collect.

    This is the tool whose output contains text written by application code, so it is where
    untrusted content enters the system. The guardrail layer validates and neutralises that content
    on arrival in the client; nothing is filtered here, because a tool that quietly altered the
    logs it reports would make the evidence untrustworthy in a different way.
    """
    return await _offload(log_tool.collect, service, starts_at)


@mcp.tool(
    name="collect_change_evidence",
    description=(
        "Collect configuration and deployment evidence for one service: which feature flags flagd "
        "is currently serving and whether each affects this service or one it calls, the "
        "container's restart history and image version. In this environment a flag toggle is what "
        "causes every injected fault, so an active flag affecting the service is usually the real "
        "cause. starts_at is the incident's start time as an ISO 8601 timestamp, used to decide "
        "whether a restart happened shortly before the incident."
    ),
)
async def collect_change_evidence(service: str, starts_at: str) -> dict:
    """Thin passthrough to change_tool.collect, which owns the flag, restart and dependency logic."""
    return await _offload(change_tool.collect, service, starts_at)


# Built once at import: uvicorn needs a module-level ASGI app, and FastMCP's session manager is
# started by the app's own lifespan.
# Its own exporter, and the caller's trace continued rather than restarted, so a
# tool call appears inside the investigation that made it.
tracing.init("aira-mcp-tools")
app = tracing.TraceMiddleware(mcp.streamable_http_app(), "mcp")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
