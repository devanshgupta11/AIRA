"""One shared TLS context for every HTTP client AIRA builds.

Measured on this machine (2026-10-24, while putting the evidence tools behind MCP):

    ssl.create_default_context()            106 ms
    httpx.Client()                          344 ms
    httpx.Client(verify=<reused context>)     0 ms
    an actual GET to localhost              1-20 ms

`httpx.Client()` builds its own TLS context, and on Windows that means loading the system root
certificate store - hundreds of times more expensive than the localhost request that follows. Every
`*_tool.collect()` constructed a fresh client per call, so each evidence collection paid roughly
300 ms before issuing a single query, and the MCP client paid it a second time on the way in.

Building the context once and handing the same object to every client removes that entirely. The
context is read-only in use and `httpx` does not mutate it, so sharing it across threads and
clients is safe. Nothing about verification is weakened: this is the same default context, created
once instead of thousands of times.
"""

import ssl

import httpx

# Built once, at import. Everything that follows reuses it.
SSL_CONTEXT = ssl.create_default_context()


async def _inject_trace_context(request) -> None:
    """Add W3C trace-context headers to an outgoing request, at the moment it is sent.

    Injected per request rather than per client: a client is built once and reused, but the trace
    context changes with every investigation. This is what joins the MCP tools server and the RCA
    agent's process to the trace of the investigation that called them - the hop AIRA could not
    otherwise correlate, because neither of those processes knows the incident id.
    """
    try:
        from . import tracing

        for key, value in tracing.headers_for_outgoing().items():
            request.headers[key] = value
    except Exception:  # noqa: BLE001 - telemetry must never break a request
        pass


def _with_trace_hook(kwargs: dict) -> dict:
    hooks = dict(kwargs.get("event_hooks") or {})
    hooks["request"] = [*hooks.get("request", []), _inject_trace_context]
    kwargs["event_hooks"] = hooks
    return kwargs


def client(**kwargs) -> httpx.Client:
    """A synchronous client. Use for the deterministic evidence tools, which run in threads."""
    kwargs.setdefault("verify", SSL_CONTEXT)
    return httpx.Client(**kwargs)


def async_client(**kwargs) -> httpx.AsyncClient:
    """An async client that carries the current trace context on every request."""
    kwargs.setdefault("verify", SSL_CONTEXT)
    return httpx.AsyncClient(**_with_trace_hook(kwargs))


def mcp_http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """The factory MCP's transports call to build their HTTP client.

    Signature must match `mcp.shared._httpx_utils.create_mcp_http_client`, which MCP uses by
    default; this replaces it only to pass the shared TLS context, keeping MCP's own defaults
    (follow_redirects, and its timeout handling when none is given).
    """
    kwargs: dict = {"follow_redirects": True, "verify": SSL_CONTEXT}
    if timeout is None:
        from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

        kwargs["timeout"] = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    else:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**_with_trace_hook(kwargs))
