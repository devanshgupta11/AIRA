# AIRA: Autonomous Intelligent Response Agent

AIRA is a multi-agent AI system for incident response in a microservices environment. It sits on top
of standard monitoring tools (Prometheus, Alertmanager, Grafana, Jaeger). AIRA does not replace them:
it reasons over their evidence, coordinates investigation and, later in the semester, performs
controlled remediation.

*CSE3101 Agentic AI · BML Munjal University · Batch 2023, 7th semester · Group 18:
Devansh Gupta (230514), Deepanshu Goyal (230500), Dev Garg (230487).*

> **Status: mid-semester milestone.** This milestone runs the full path from a real fault to an
> AI-written triage summary. The Orchestrator, investigator agents, RCA, Critic, remediation,
> verification, Qdrant memory and React dashboard come later.
> See [docs/midsem-progress.md](docs/midsem-progress.md).

## What works today

```
OpenTelemetry Demo (28 containers, real traffic from Locust)
   │ OTLP
   ▼
OTel Collector ──► Prometheus ──► AIRA alert rules ──► Alertmanager
                                                         │ webhook
                                                         ▼
                                        AIRA backend (FastAPI + SQLite)
                                          ├─ Prometheus tool: live metrics for the service
                                          └─ CrewAI Triage Agent on Ollama (qwen2.5:7b-instruct, local GPU)
                                                         │
                                                         ▼
                                          Incident Console: http://localhost:8000
```

1. **Monitored system:** [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) 3.1.0
   (Astronomy Shop) running locally in Docker, with Grafana, Jaeger, Prometheus, flagd and Locust.
2. **Real fault injection:** demo feature flags (high CPU, cart, payment and product-catalog failures,
   memory leak, homepage flood) plus stopping a container.
3. **Detection:** four AIRA alert rules (error rate, failing operation, high CPU, service down) built only
   on metric names discovered in the running stack.
4. **Alerting:** Alertmanager sends a webhook to the AIRA backend.
5. **Backend:** stores each alert as an incident, queries Prometheus for the service's current
   request rate, error ratio, p95 latency, CPU and memory, and runs the Triage Agent in the background.
6. **Triage Agent:** a CrewAI agent on a local Ollama model writes a structured, evidence-grounded
   summary, validated with Pydantic. It takes no actions.
7. **Console:** a live page showing incidents, metrics evidence and the agent's summary.

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
├── backend\               FastAPI app, Prometheus tool, Triage agent, console page
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
.\start-all.ps1          # Docker stack, collector fix, Ollama + warm-up, backend; prints URLs (~2-3 min cold)
.\status.ps1             # health of everything, active alerts, active faults, incidents
.\inject-fault.ps1 -Scenario payment-failure
.\clear-fault.ps1  -Scenario payment-failure
.\clear-all-faults.ps1   # every flag off, stopped containers started again
.\reset-incidents.ps1    # empty the incident database (asks for confirmation; -Force to skip)
.\stop-all.ps1           # stop backend, containers, Ollama
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

More detail: [docs/architecture-midsem.md](docs/architecture-midsem.md) ·
[docs/demo-script.md](docs/demo-script.md) · [docs/environment-discovery.md](docs/environment-discovery.md) ·
[docs/viva-notes.md](docs/viva-notes.md).
