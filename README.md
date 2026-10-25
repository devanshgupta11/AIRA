# AIRA: Autonomous Intelligent Response Agent

AIRA is a multi-agent AI system for incident response in a microservices environment. It sits on top
of standard monitoring tools (Prometheus, Alertmanager, Grafana, Jaeger). AIRA does not replace them:
it reasons over their evidence, coordinates investigation and, later in the semester, performs
controlled remediation.

## What works today

```
OpenTelemetry Demo (28 containers, real traffic from Locust)
   │ OTLP
   ▼
OTel Collector ──► Prometheus ──► AIRA alert rules ──► Alertmanager
                                                         │ webhook
                                                         ▼
                                        AIRA backend (FastAPI + SQLite)
                                          │  ▲ guardrail 1: webhook payload validated
                                          │
                                          ├─ Triage Agent        first summary + cause category
                                          ├─ Orchestrator        decides WHICH investigators to run
                                          │    │
                                          │    │  MCP (:8002, separate process)
                                          │    ├─ Metrics Agent  collect_prometheus_evidence
                                          │    ├─ Log Agent      collect_log_evidence
                                          │    └─ Change Agent   collect_change_evidence
                                          │       ▲ guardrail 2: tool returns type-checked
                                          │       ▲ guardrail 3: untrusted text fenced + defanged
                                          ├─ shared incident context (every finding stored)
                                          │        │
                                          │        ├──► RCA Agent (Google ADK, separate process)
                                          │        │      reached over A2A on :8001
                                          │        │      → 1-3 ranked hypotheses
                                          │        │
                                          │        └──► Critic Agent (CrewAI)
                                          │               accept, or insufficient + the gap
                                          │                      │
                                          └──────────────────────┘
                                            insufficient → re-run the named investigator,
                                            asked the Critic's question (max 2 rounds)
                                          │
                                          └─ Remediation Agent  proposes ONE action from a
                                             │                  fixed allowlist, or declines
                                             ▼
                                          ┌──────────────────────────────┐
                                          │  HUMAN APPROVAL REQUIRED     │  ← nothing runs
                                          │  Approve and run / Reject    │     without this
                                          └──────────────────────────────┘
                                             │ approved
                                             ▼
                                          execute → confirm at the source
                                             │        (flagd / docker)
                                             ▼
                                          verify against live metrics
                                             recovered / not yet / not recovered
                                             │ not recovered → back round, max 2 attempts
                                                         │
                                                         ▼
                                          Incident Console: http://localhost:8000
                                          + execution trace per incident
                                          + one Jaeger trace across all 3 processes
                                          + full audit of every action
```

All seven agents run on one local model (qwen2.5:7b-instruct on the GPU), across **two frameworks**:
CrewAI for triage, the investigators and the Critic; **Google ADK** for the RCA agent, which runs as
its own process and is reached over the **A2A protocol** rather than imported. Evidence gathering is
parallel; the reasoning is serial because a single local model serves them all.

Measured over four real fault scenarios: **28–46 s** from alert to accepted root cause, **56–69 s**
when the Critic rejects the first analysis and a second investigation round runs. Putting the
evidence tools behind MCP added about **115 ms** per investigation, and uncovered a ~300 ms
per-collection cost that had been there since Phase 0, so the net result is slightly faster.

1. **Monitored system:** [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) 3.1.0
   (Astronomy Shop) running locally in Docker, with Grafana, Jaeger, Prometheus, flagd and Locust.
2. **Real fault injection:** demo feature flags (high CPU, cart, payment and product-catalog failures,
   memory leak, homepage flood) plus stopping a container.
3. **Detection:** four AIRA alert rules (error rate, failing operation, high CPU, service down) built only
   on metric names discovered in the running stack.
4. **Alerting:** Alertmanager sends a webhook to the AIRA backend.
5. **Backend:** stores each alert as an incident and investigates it in the background.
6. **Triage Agent:** a CrewAI agent on a local Ollama model writes a structured, evidence-grounded
   summary, validated with Pydantic.
7. **Orchestrator:** decides which investigators this incident needs. A CPU alert skips the Log
   agent; an error alert runs all three. Skipped agents are recorded with their reason.
8. **Investigators:** Metrics (Prometheus), Log (OpenSearch + container stdout, merged and
   de-duplicated), Change (feature flags, container restarts, image versions). Their evidence is
   gathered in parallel; each writes findings into the shared incident context.
9. **RCA Agent:** a **Google ADK** agent in its own process, reached over **A2A** (JSON-RPC over
    HTTP, agent card at `http://127.0.0.1:8001/.well-known/agent-card.json`). It reads every
    finding and returns 1–3 ranked hypotheses, each naming the agents that support it.
10. **Critic Agent:** a CrewAI agent that reviews those hypotheses against the same evidence and
    returns `accept`, or `insufficient` with a specific gap — what is missing and which
    investigator should find it.
11. **Replanning:** on a rejection the Orchestrator re-runs exactly that investigator, handing it
    the Critic's question as a focus, then re-runs the RCA and Critic. **Capped at two rounds.** If
    the Critic is still unsatisfied, AIRA records that honestly: *"investigated twice; the evidence
    still does not establish a cause."*
12. **Remediation Agent:** proposes one action from the allowlist — `reset_feature_flag` or
    `restart_container` — with its reasoning, its risk, and how to reverse it. Or answers
    **"no safe action applies"**, which is a complete answer, not a failure.
13. **Human approval:** the proposal appears on the console and waits. **Nothing executes until a
    person approves it.** The decision is recorded before the action runs.
14. **Execution and confirmation:** the action is re-validated, carried out, and then confirmed at
    the source — flagd is asked what it now serves, Docker is asked for the container's status and
    a changed start time.
15. **Verification:** AIRA waits the window that alert actually takes to clear, then reports
    **recovered**, **not yet**, or **not recovered** — judging the measurement behind the alert as
    well as the alert itself, because `keep_firing_for` makes an alert outlive its cause. A failed
    remedy goes back for one more attempt, capped at two.
16. **Console:** a live page showing incidents, a per-agent timeline with runtimes, each agent's
    findings and the evidence it used, which agents were skipped and why, the ranked hypotheses,
    the Critic's verdict, any second round, the final conclusion, and every action with its
    approval, result and verification.

**AIRA can now act, but only with permission.** It chooses one action from a fixed set of two —
reset a feature flag, or restart a container — names the target, and waits. A human clicks
**Approve** on the console; nothing else in the system can reach the execution path. The action is
re-validated at the moment of acting, carried out without any shell, confirmed by asking flagd or
Docker directly, and then checked against live metrics to see whether the incident is really over.

Twelve services can never be acted on, enforced in code: the six monitoring services, plus flagd
and flagd-ui (the control plane a flag reset runs through), frontend-proxy (the ingress AIRA itself
polls), and kafka, valkey-cart and astronomy-db (where a restart would lose in-flight data).

Nothing on screen is mocked: every value comes from the running system.

## Repository layout

```
D:\AIRA
├── opentelemetry-demo\    upstream clone at tag 3.1.0 (never edited; not committed)
├── infra\                 AIRA's layer on top of the demo
│   ├── aira.env               pins demo images to 3.1.0, points the collector at our extras file
│   ├── compose.aira.yaml      adds Alertmanager, Prometheus rule loading, product-catalog memory fix
│   ├── alert-rules.yml        the four AIRA alert rules
│   ├── alertmanager\alertmanager.yml
│   ├── prometheus\prometheus-config.yaml   upstream config + rule_files + alerting
│   └── otelcol-config-extras.yml           span metrics every 15s
├── backend\               FastAPI app and the agents
│   └── app\
│       ├── prometheus_tool.py · log_tool.py · change_tool.py   deterministic evidence, no LLM
│       ├── triage_agent.py                                     first responder
│       └── agents\        base.py        shared CrewAI plumbing: prompt rules, retry, validation
│                          adk_base.py    the same contract for Google ADK, plus the A2A client
│                          orchestrator.py · metrics_agent.py · log_agent.py · change_agent.py
│                          rca_agent.py   ranked hypotheses (ADK)
│                          rca_server.py  serves the RCA agent over A2A on :8001
│                          critic_agent.py accepts the analysis, or names what is missing
│                          remediation_agent.py  proposes one action, or declines
│       ├── remediation.py      the action allowlist - no agent, model or prompt imported
│       ├── guardrails.py       webhook validation + prompt-injection defence
│       ├── events.py           the execution trace, from both frameworks' callbacks
│       └── tracing.py          one distributed trace per incident, into Jaeger
├── scripts\               start/stop/status, fault injection, measurements
├── docs\                  architecture, progress, demo script, viva notes, discovery
├── logs\                  runtime logs (not committed)
├── docker-data\           Docker Desktop disk image (moved here from C:)
└── ollama-models\         local LLM weights (OLLAMA_MODELS)
```

## Prerequisites (already installed on the demo laptop)

| Tool | Version used |
|---|---|
| Windows 11 + WSL2 | build 26200 |
| Docker Desktop | engine 29.1.2, Compose v2.40.3, **disk image at `D:\AIRA\docker-data`** |
| Git | 2.54 |
| Python | 3.12.3 (backend venv at `backend\.venv`) |
| Ollama | 0.9.6, user env var `OLLAMA_MODELS=D:\AIRA\ollama-models` |
| GPU (optional) | RTX 4060 Laptop 8 GB; the model runs 100% on GPU |

RAM: the demo uses about 6–7 GB and the model about 6 GB of GPU memory. Tested on a 24 GB laptop.

## Setup from scratch

```powershell
# 1. Demo at the pinned release
git clone --branch 3.1.0 --depth 1 https://github.com/open-telemetry/opentelemetry-demo.git D:\AIRA\opentelemetry-demo

# 2. Model (stored under D:\AIRA\ollama-models)
[Environment]::SetEnvironmentVariable('OLLAMA_MODELS','D:\AIRA\ollama-models','User')   # then restart Ollama
ollama pull qwen2.5:7b-instruct

# 3. Backend environment
cd D:\AIRA\backend
$env:UV_CACHE_DIR='D:\AIRA\.cache\uv'
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

## Running

```powershell
cd D:\AIRA\scripts
.\start-all.ps1          # Docker stack, collector fix, Ollama + warm-up, MCP tools (:8002),
                         # RCA agent (A2A :8001), backend; prints URLs. Cold start ~130s
.\status.ps1             # health of everything, active alerts, active faults, incidents
.\inject-fault.ps1 -Scenario payment-failure
.\clear-fault.ps1  -Scenario payment-failure
.\clear-all-faults.ps1   # every flag off, stopped containers started again
.\reset-incidents.ps1    # empty the incident database (asks for confirmation; -Force to skip)
.\stop-all.ps1           # stop backend, RCA agent, containers, Ollama
```

Verification tools (both run against the real system, no mocks):

```powershell
# re-run every agent against stored incidents and check that no number or quoted log line
# was invented. Covers all six reasoning and investigating agents; --agent=rca narrows it.
# The RCA agent is exercised over the real A2A connection when :8001 is up.
D:\AIRA\backend\.venv\Scripts\python.exe check_agent_numbers.py

# inject real faults end to end and record which agents were planned, run, skipped and how long
# each took. Results append to docs\measurements\phase-a-runs.jsonl
D:\AIRA\backend\.venv\Scripts\python.exe verify_phase_a.py

# one check per bug found while building, so none can quietly come back.
# --static skips the model calls and runs in a second.
D:\AIRA\backend\.venv\Scripts\python.exe regression_check.py
```

Two of the regression checks only run while a fault flag is set, so for the complete suite inject a
fault first (`.\inject-fault.ps1 -Scenario payment-failure`) and clear it afterwards. Latest run:
**142/142 passed**; the static-only pass runs in seconds.

The RCA agent runs as its own process and must be up for the root-cause step to work. `start-all.ps1`
starts it and `status.ps1` reports it; to run it by hand:

```powershell
cd D:\AIRA\backend
.\.venv\Scripts\python.exe -m uvicorn app.agents.rca_server:app --host 127.0.0.1 --port 8001
```

If PowerShell blocks the scripts, run them as
`powershell -ExecutionPolicy Bypass -File .\start-all.ps1`.

Scenarios: `high-cpu`, `cart-failure`, `payment-failure`, `product-catalog-failure`, `memory-leak`,
`homepage-flood`, `service-down` (stops the `recommendation` container).

## URLs

| What | URL |
|---|---|
| **AIRA Incident Console** | http://localhost:8000/ |
| AIRA API | http://localhost:8000/incidents · http://localhost:8000/health · http://localhost:8000/docs |
| Astronomy Shop | http://localhost:8080/ |
| Grafana | http://localhost:8080/grafana/ |
| Jaeger | http://localhost:8080/jaeger/ui/ |
| Feature flags | http://localhost:8080/feature/ |
| Load generator (Locust) | http://localhost:8080/loadgen/ |
| Prometheus / alerts | http://localhost:9090/ · http://localhost:9090/alerts |
| Alertmanager | http://localhost:9093/ |
| **RCA agent card (A2A)** | http://127.0.0.1:8001/.well-known/agent-card.json |
| **MCP tools server** | http://127.0.0.1:8002/mcp (needs an MCP handshake; `status.ps1` lists its tools) |
| **Jaeger (AIRA's own traces)** | http://localhost:8080/jaeger/ui/ — search for service `aira` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `start-all.ps1` says Docker engine not running | Open Docker Desktop and wait for "Engine running", then re-run |
| Port 8000 already in use | `Get-NetTCPConnection -LocalPort 8000` shows who owns it. Stop that process or the old backend (`stop-all.ps1`) |
| Console shows "backend unreachable" | Check `logs\uvicorn.err.log`. Start again with `start-all.ps1` (it only starts what is missing) |
| Ollama pill red / agent errors | `ollama list` must show `qwen2.5:7b-instruct`. Check `OLLAMA_MODELS` points to `D:\AIRA\ollama-models`, then restart Ollama |
| Alert does not appear in AIRA | Check http://localhost:9090/alerts (rule state) and http://localhost:9093 (Alertmanager). `docker logs alertmanager` shows webhook errors |
| A service has no CPU/memory evidence | Collector start-up race: `docker restart otel-collector` (start-all does this) |
| First agent run slow (~30 s) | The model is loading into GPU memory. start-all warms it up and keeps it loaded for 60 min |
| Shop slow or errors with no fault injected | Run `.\clear-all-faults.ps1`, then `.\status.ps1` |
| Every incident's evidence says `_mcp.used: false` | The MCP tools server is down; investigations fall back to direct tool calls and record that. `status.ps1` says so; `start-all.ps1` starts it, or run `uvicorn app.mcp_servers.tools_server:app --port 8002` from `backend\` |
| The console offers no Jaeger link, or `/health` says spans are being dropped | The otel-collector is not reachable. AIRA re-points itself within a minute of the collector moving port; if it stays down, investigations are unaffected and tracing resumes when it returns |
| Every incident's RCA step errors | The RCA agent process is down. `status.ps1` says so; `start-all.ps1` starts it, or run `uvicorn app.agents.rca_server:app --port 8001` from `backend\`. The investigators still report normally without it |
| First root-cause step after a cold start takes ~25 s longer | LiteLLM initialises once per process. `start-all.ps1` pays this up front; it only shows up if the RCA agent was started by hand |

More detail: [docs/architecture-midsem.md](docs/architecture-midsem.md) ·
[docs/demo-script.md](docs/demo-script.md) · [docs/environment-discovery.md](docs/environment-discovery.md) ·
[docs/viva-notes.md](docs/viva-notes.md).
