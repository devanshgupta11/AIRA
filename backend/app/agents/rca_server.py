"""Serves the RCA agent over the A2A protocol.

The project runs two agent frameworks on purpose: CrewAI for the investigators, Google ADK for the
reasoning agents. They are kept genuinely separate - the CrewAI side reaches the ADK RCA agent by
speaking A2A over HTTP, not by importing it. Crossing a real protocol boundary is the point: it is
what makes the two frameworks interoperable rather than merely co-resident in one process.

ADK provides both halves, so neither is hand-rolled:
  server  google.adk.a2a.utils.agent_to_a2a.to_a2a()  -> a Starlette app
  client  google.adk.a2a.agent.RemoteA2aAgent          -> see adk_base.ask_a2a()

Run it:
    python -m uvicorn app.agents.rca_server:app --host 127.0.0.1 --port 8001
(`scripts\\start-all.ps1` does this; `status.ps1` reports its health.)

Localhost only. The agent card is published at /.well-known/agent-card.json.
"""

from google.adk.a2a.utils.agent_to_a2a import to_a2a

from . import adk_base, rca_agent
from .. import tracing

HOST = "127.0.0.1"
PORT = 8001

# The same cached agent the in-process path would use, so behaviour is identical on both sides of
# the boundary - only the transport differs.
agent = adk_base.make_adk_agent("aira_rca", rca_agent.INSTRUCTION)

# Its own exporter, and the caller's trace continued rather than restarted:
# this process is where the RCA model call actually happens.
tracing.init("aira-rca-agent")
app = tracing.TraceMiddleware(to_a2a(agent, host=HOST, port=PORT), "a2a")
