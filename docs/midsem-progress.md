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

**Rule revision after these measurements (2026-10-06).** During a later rehearsal, `AiraHighErrorRate`
was seen to flap on low-traffic services: one continuous `paymentFailure` fault produced incidents at
12:46:54, 12:51:24 and 12:52:39, each resolving while the fault was still active. The cause was the
`>= 3 errors` guard, which at ~0.06 req/s sits close to the roughly 7 requests a 2-minute window holds.
The rule now uses a 3-minute window and both error rules carry `keep_firing_for: 2m`. Verified after the
change: the same continuous fault produced exactly 2 incidents, firing without interruption for over
13 minutes. Resolution after clearing a fault is correspondingly ~2 minutes slower, which is the
intended trade-off.

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

## Phase A — multi-agent investigation (added after the mid-semester milestone)

The single Triage agent became a planned, five-agent investigation. Everything in the mid-semester
pipeline above still works unchanged; this layer sits on top of it.

| Component | What it does | Evidence source |
|---|---|---|
| **Orchestrator** | Decides which investigators this incident needs; records skips with reasons | alert `aira_signal` label + triage category |
| **Metrics Agent** | Says which measurements are genuinely abnormal vs their own baseline | `prometheus_tool.py` |
| **Log Agent** | Reports what the service's own log lines show, or that there are none | OpenSearch + `docker logs`, merged and de-duplicated with occurrence counts |
| **Change Agent** | Finds the configuration change behind the incident | flagd OFREP, container restarts, image versions |
| **Shared context** | One row per agent per incident: plan, status, findings, evidence, timing | `agent_findings` table |

### Dynamic planning, measured

| Fault | Alert | Investigators run | Skipped |
|---|---|---|---|
| `high-cpu` | AiraHighCpu / ad | metrics, change | **log** — CPU saturation is a resource problem; logs rarely explain it |
| `payment-failure` | AiraOperationFailing / payment | metrics, log, change | – |
| `service-down` | AiraServiceDown / recommendation | metrics, change | **log** — the service stopped logging before the incident |

### Timing (measured, same laptop)

| Stage | Time |
|---|---|
| Evidence for 3 investigators, gathered **in parallel** | 0.6–1.9 s |
| Triage agent | 7–14 s |
| Metrics / Log / Change agents | 6–10 / 4–7 / 5–9 s |
| **Full investigation, end to end** | **17–28 s** (budget was 45 s) |

Reasoning is serial by necessity: one local model serves every agent, and ~1.4 GB of spare VRAM
cannot hold a second. The parallelism is in the evidence gathering, which is pure I/O.

### Root causes actually identified

* `high-cpu` → *"Feature flag adHighCpu is on"* — the injected flag, correctly named.
* `payment-failure` on **checkout** → *"paymentFailure flag is set to 100% on payment service"* —
  traced across a service boundary, because checkout calls payment. The dependency graph is read
  from the containers' own `<NAME>_ADDR` environment variables rather than hard-coded.

### Bugs found by testing, and how they were fixed

| Bug | Fix |
|---|---|
| Metrics agent said latency "increased" when the number had fallen | Explicit direction rule; it now states RISEN / FALLEN and was re-verified on four incidents |
| Change agent answered "no changes detected" while `paymentFailure` was at 100% | Stronger wording was not enough, so a code-level consistency check rejects the answer and forces the retry |
| Change agent blamed a payment flag for a `cart` alert | Real dependency graph read from container environment; the agent is now told the relationship instead of guessing |
| Metrics agent invented a baseline of "4.3%" | The number came from an example inside our own prompt. All example values are now placeholders |
| Quote checker rejected agents citing two real evidence lines together | Each line is now verified separately; re-tested against a deliberately fabricated quote to confirm it still catches one |
| After a stop/start cycle, 5 incidents stayed "firing" forever while Prometheus showed none | The resolved webhooks were lost while the backend was down. On startup the backend now reconciles against Alertmanager and closes any firing incident it no longer lists as active |
| Change agent blamed a container restart and omitted the feature flag entirely | A routine stack start 13 minutes earlier was labelled a recent restart. The window was cut from 30 to 8 minutes, the wording de-emphasised, and a code check now rejects any answer that fails to mention a flag affecting this service or one it calls |
| On the CPU alert, triage reported the 100% trigger level as grafana's "baseline" | The threshold-clarifying rewrite only matched `(threshold X)` as a whole parenthesis, so it missed `(100% = one full core; threshold 100%)`. It now matches the word wherever it appears; verified against all four alert description formats |

### Verification

`scripts/regression_check.py` holds **one check per bug listed above**, so none can quietly return.
Eleven are static (prompt wording, thresholds, dependency graph, schema, the quote checker's own
behaviour) and four call the local model to confirm agent behaviour on real data. Latest run:
**15/15 passed**, with a fault injected so the flag-related checks could run.

`scripts/check_agent_numbers.py` re-runs every agent against stored incidents and checks that no
number and no quoted log line appears that was not in its input. It is itself tested against a
fabricated quote, so it is not a no-op. Latest run: **16/16 clean across 4 agents and 4 incident
types.** `scripts/verify_phase_a.py` injects real faults end to end and records the plan, runtimes
and skips into `docs/measurements/phase-a-runs.jsonl`.

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
