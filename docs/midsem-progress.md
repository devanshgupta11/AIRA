# AIRA: mid-semester progress report

*Group 18 · CSE3101 Agentic AI · status as of 2026-10-06*

## Summary

The complete path from a **real fault** to an **AI-written, evidence-grounded triage** works on a
laptop, and the stack starts with one command. The pipeline: OpenTelemetry Demo 3.1.0 → OTel Collector →
Prometheus → 4 AIRA alert rules → Alertmanager → FastAPI webhook → SQLite + live Prometheus evidence →
CrewAI Triage Agent on a local Ollama model → live console. Everything shown is real data; nothing is
mocked.

## Completed (with evidence)

| Item | Evidence |
|---|---|
| Environment on D: only (Docker disk image moved, models, caches, venv) | Docker disk at `D:\AIRA\docker-data\...\docker_data.vhdx`; `ollama list` model blobs in `D:\AIRA\ollama-models` (4.36 GB); nothing large on C: |
| OpenTelemetry Demo 3.1.0 running (28 containers + Alertmanager) | `status.ps1`: 29/29 up; shop, Grafana, Jaeger, flags and Locust UIs return HTTP 200 |
| Metric and flag discovery from the live system | `docs/environment-discovery.md` (440 metric names inspected; 18 flags read from source) |
| Fault injection verified at the source | `flagctl.py` changes flags via the flagd-ui API and confirms with flagd's OFREP API; all 7 scenarios injected and cleared successfully |
| 4 alert rules, validated by `promtool`, tested with real faults | table below; `docs/measurements/alert-timings.jsonl` |
| Alertmanager → backend webhook | `start-all.ps1` checks the path from inside the container; 23 real webhook payloads logged during testing |
| Backend: incidents, evidence, API, console | `/incidents`, `/incidents/{id}`, `/health`, `/` work; firing → resolved transitions observed |
| CrewAI Triage Agent on local GPU model | 10/10 real incidents triaged, all valid on the first attempt |
| One-command operation | `start-all.ps1` cold start in about 55 s after images are present (plus Docker Desktop start), every step verified |
| Robustness | Backend resumes unfinished incidents after a restart (tested); agent runs serialised and time-limited |

### Measured alert timings (real faults, 2026-10-06)

| Scenario (script name) | Fault | Expected alert | Pending | **Firing** | Resolved after clearing | Side effects observed |
|---|---|---|---|---|---|---|
| `high-cpu` | `adHighCpu` (4 busy threads) | AiraHighCpu / ad | 42 s | **72 s** | 74 s | none |
| `service-down` | `docker stop recommendation` | AiraServiceDown / recommendation | 72 s | **89 s** | 31 s | none |
| `product-catalog-failure` | `productCatalogFailure` | AiraHighErrorRate / product-catalog | 87 s | **117 s** | 90 s | frontend error rate firing at 42 s (cascade) |
| `payment-failure` | `paymentFailure` 100 % | AiraOperationFailing / payment | 103 s | **133 s** | 120 s | payment and checkout error rate firing at 119 s |
| `cart-failure` | `cartFailure` 100 % | AiraOperationFailing / cart | 299 s | **327 s** | 30 s | none |
| `memory-leak` | `recommendationCacheFailure` | none (no memory rule) | – | – | – | no visible memory growth within 4 min |
| `homepage-flood` | `loadGeneratorFloodHomepage` | none | – | – | – | observation stopped early (no rule targets it) |

The time-to-fire is dominated by the data: rate windows of 1–3 minutes plus a 15–30 s `for:` clause,
on services with low traffic (payment about 0.05 req/s, cart's `EmptyCart` about 0.02 req/s).

### Triage agent (qwen2.5:7b-instruct on Ollama 0.9.6, RTX 4060 Laptop 8 GB)

| Measure | Value |
|---|---|
| Model size / placement | 4.7 GB on disk; 5.6 GB loaded, 100 % in GPU memory |
| Generation speed | ~45 tokens/s (warm) |
| Cold model load | 31 s (first request after a restart; `start-all.ps1` pre-warms) |
| Runtime per incident (10 real incidents) | min 6.7 s · **median 7.4 s** · mean 8.3 s · max 14.1 s |
| Valid structured output | 10/10 on the first attempt (no retries needed) |

### Resource usage (whole stack running)

| Component | Usage |
|---|---|
| 29 containers (docker stats) | 4.24 GiB |
| WSL2 VM (incl. Linux file cache) | 7.5 GB after the 12 GB cap (11.2 GB before) |
| Ollama | ~1 GB system RAM + 5.6 GB GPU memory |
| AIRA backend | ~210 MB |
| System RAM in use | 18.6 of 23.7 GB with VS Code and a browser open |
| Disk on D: | 28.4 GB in `D:\AIRA`: Docker disk image 22.7 GB, model 4.4 GB, caches 0.7 GB, venv 0.6 GB |
| Disk on C: | AIRA itself: one ~20 KB CrewAI cache file. WSL2 swap file (~1.2 GB, Windows default location) |

### End-to-end rehearsal (2026-10-06, after a cold `stop-all` → WSL restart → `start-all`)

| Step | Result |
|---|---|
| `start-all.ps1` from cold (Docker Desktop not running) | healthy in 55 s after the engine came up; all checks `[OK]` |
| `start-all.ps1` re-run on a running stack | idempotent, 40 s, only restarted the missing backend |
| `payment-failure` | 4 incidents (payment + checkout) at ~2 min 15 s; each triaged in 7.2–7.5 s; error ratio 100 % vs 0.0 % baseline |
| `service-down` | AiraServiceDown at ~75 s; triaged in 7.3 s as `service_unavailable`, confidence high |
| Clear both | all alerts and incidents resolved without intervention within ~4 min |

## In progress / known limitations

* **Single-service view:** the agent only sees the alerting service's metrics, so cascades are labelled
  `application_error` even when the cause is a dependency (frontend errors caused by product-catalog).
  The Orchestrator and investigator agents address this.
* **Baselines need history:** "15–25 minutes ago" comparisons show "unavailable" for about 25 minutes
  after Prometheus first starts.
* **No memory-based rule yet:** many demo containers normally run at 80–88 % of their memory limit, so a
  plain threshold would be noisy. `memory-leak` grows too slowly to see in 4 minutes. A trend-based rule
  (`deriv`/`predict_linear`) is planned.
* **Cart scenario is slow (about 5.5 min):** only one rarely called operation fails.
* **Logs:** the demo ships OpenSearch rather than Loki. Some log records are rejected by an upstream
  mapping conflict. Logs are not yet consumed by AIRA.

## Next steps (mapped to the synopsis timeline)

| Week(s) | Deliverable | Starting point in the current code |
|---|---|---|
| 3–4 | Orchestrator; Metrics, Log and Change agents in parallel; RCA and Critic | `prometheus_tool.py` becomes the Metrics agent's tool; triage output feeds the Orchestrator |
| 4–5 | Controlled remediation (allow-list: restart container, reset flag, bounded scale), human approval, verification loop, Qdrant memory | `inject/clear-fault` primitives; alert rules double as recovery checks |
| 5–6 | Repeatable end-to-end incident runs with replanning | `measure_alerts.py` scenario runner |
| 6–7 | Evaluation: rule baseline vs single LLM vs AIRA without Critic vs full AIRA | timings and incidents already recorded per run |
| 7–8 | React + Vite dashboard, incident timeline, agent activity | replaces the preview console on the same `/incidents` API |

## Challenges and how they were solved

| Challenge | Solution |
|---|---|
| C: drive had 2.4 GB free; Docker's disk image was on C: | Moved the Docker Desktop disk image to `D:\AIRA\docker-data`; `OLLAMA_MODELS`, pip/uv caches and venv on D: |
| Demo images default to `latest` | Pinned to 3.1.0 through our own env file, without editing upstream |
| No `up` metric (services push OTLP) | Service-down rule based on container metrics going silent |
| `container_cpu_utilization_ratio` lagged badly (2→15 while the real value was 400 %) | Used the rate of the CPU-time counter instead |
| Span metrics only once per minute | Collector extras file: flush every 15 s |
| Collector missed containers started at the same moment | `start-all.ps1` restarts the collector, then verifies fresh metrics for key services |
| product-catalog degraded at baseline (130–190 % CPU, 800 ms median) | More memory headroom in our override: 0.3 % CPU, 3.8 ms median |
| False positive from flagd stream disconnects | Excluded `*/EventStream` spans from the error rules |
| Backend restart orphaned in-flight triage | Resume unfinished firing incidents on startup |
| PowerShell 5.1 swallowed `docker compose -d` | Compose wrapper forwards raw `$args` |
| WSL2 VM held 11 GB of file cache (system RAM 23.1/23.7 GB) | `.wslconfig` cap of 12 GB with automatic cache reclaim |
