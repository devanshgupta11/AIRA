# Live demo script: AIRA mid-semester review (8–10 minutes)

## Before the review (start 30–40 minutes early)

The baselines in the evidence ("15–25 minutes ago") need about 25 minutes of history, and the model
must be loaded on the GPU. Start early.

```powershell
cd D:\AIRA\scripts
.\start-all.ps1                 # ~3-4 min from cold. Must end with "AIRA stack is up and healthy."
.\clear-all-faults.ps1          # every flag off, recommendation running
.\reset-incidents.ps1           # type YES -> empty console for the demo
.\status.ps1                    # everything [OK], "no alerts pending or firing"
```

Open these browser tabs, in this order:

1. http://localhost:8000/ (AIRA console: should say "No incidents yet")
2. http://localhost:8080/ (Astronomy Shop)
3. http://localhost:8080/grafana/ → Dashboards → **Spanmetrics Demo Dashboard**, last 15 minutes
4. http://localhost:8080/feature/ (feature flags)
5. http://localhost:9090/alerts (Prometheus alerts)
6. http://localhost:9093/ (Alertmanager)
7. http://localhost:8080/jaeger/ui/

Keep one PowerShell window open in `D:\AIRA\scripts`. Close other heavy apps.

## Measured timings (from `docs/measurements/alert-timings.jsonl`)

| Scenario | Alert | Fires after | Resolves after clear |
|---|---|---|---|
| `high-cpu` | AiraHighCpu / ad | ~72 s | ~74 s |
| `service-down` | AiraServiceDown / recommendation | ~89 s | ~31 s |
| `payment-failure` | AiraOperationFailing / payment (+ error rate on payment and checkout) | ~90–135 s | ~3–4 min |
| `product-catalog-failure` | AiraHighErrorRate / product-catalog (frontend first, ~42 s) | ~117 s | ~3–4 min |
| `cart-failure` | AiraOperationFailing / cart | ~5.5 min (don't use live) | ~2.5 min |

The two error rules carry `keep_firing_for: 2m` (added 2026-10-06 to stop flapping on low-traffic
services), so they deliberately hold an incident open for two minutes after the errors stop. Start the
next scenario while the previous one resolves in the background rather than waiting for it.

Triage agent: about 7–14 s per incident once the model is warm.

## Script

### 0:00–1:30 The problem and the system (talk, tab 1–2)
* "Responding to an incident still means a person stitching together metrics, logs, traces and
  changes. AIRA is the agentic layer on top of standard monitoring that does that work."
* Show the **shop** (tab 2): "This is the official OpenTelemetry Demo: 28 microservices in several
  languages, running locally in Docker, with a load generator producing real traffic."
* Show `docs/architecture-midsem.md` (diagram) or describe it: demo → OTel Collector → Prometheus →
  our alert rules → Alertmanager → our FastAPI backend → Prometheus evidence → CrewAI Triage Agent on a
  local model → console.
* "Nothing is mocked. Every number comes from the running system."

### 1:30–2:30 Normal state (tabs 3, 5, 1)
* Grafana: request rates and latency per service, steady.
* Prometheus alerts: our four rules, all green/inactive. Click one rule to show the PromQL.
* AIRA console: empty, with health pills "prometheus: up" and "ollama: up · qwen2.5:7b-instruct".

### 2:30–6:00 Scenario 1: payment failure (feature flag)
```powershell
.\inject-fault.ps1 -Scenario payment-failure
```
* Point at the output: "the flag was changed through the flag service's API, and **flagd itself
  confirms** it now serves `paymentFailure = 100%`."
* Tab 4 (flags UI): paymentFailure shows 100%.
* While waiting (about 2 minutes), explain the rules: span metrics give request rate and errors per
  service and per operation; `for:` avoids flapping; this is a low-traffic service (~0.05 req/s), so it
  takes a couple of minutes.
* Tab 7 (Jaeger): service `checkout`, Tags `error=true` → a trace whose `PaymentService/Charge` span is
  red with "Invalid token".
* **~1.5 min:** tab 5, `AiraHighErrorRate` for payment/checkout goes **pending** (yellow), then **firing**.
* Tab 6 (Alertmanager): the alert group appears.
* Tab 1 (AIRA console), within seconds: a firing incident with **metrics evidence**: error ratio about
  100%, failing operation `oteldemo.PaymentService/Charge`. After about 10 s, the **Triage agent** block
  appears: summary, symptoms with the exact numbers, category `application_error`, next investigations,
  confidence.
* Say: "The agent sees only this evidence. It is instructed not to invent anything, the output is
  validated against a Pydantic schema, and it takes no actions yet. That is the Remediation agent's
  job later."
```powershell
.\clear-fault.ps1 -Scenario payment-failure
```
* "About two minutes later the incidents switch to resolved, with their duration." (Continue with
  scenario 2 meanwhile.)

### 6:00–8:30 Scenario 2: a service goes down (stopped container)
```powershell
.\inject-fault.ps1 -Scenario service-down      # docker stop recommendation
```
* "Our services push telemetry, so there is no `up` metric. We detect a stopped container because
  the collector reports every running container every 10 seconds and this one has gone silent."
* Shop: product pages load without the "You may also like" recommendations.
* **~90 s:** `AiraServiceDown / recommendation` fires. The console shows evidence "LAST REPORT … AGO - the
  container is not running" and the agent classifies it as `service_unavailable`.
```powershell
.\clear-fault.ps1 -Scenario service-down       # docker start recommendation
```
* About 30 s later it resolves. Point out that the payment incidents are now resolved too.

### 8:30–10:00 Wrap-up
* Run `.\status.ps1` for one view of all components and state.
* Show `docs/midsem-progress.md`: measured detection times and agent runtimes.
* Next steps (architecture doc, section 2): Orchestrator with dynamic planning, Metrics/Log/Change
  investigators in parallel, RCA + Critic with replanning, allow-listed remediation with human approval,
  verification loop, Qdrant memory, React dashboard, evaluation against baselines.

## If something misbehaves

| Problem | What to do (stay calm, the system is real) |
|---|---|
| Alert takes longer than expected | Normal variance (rate windows). Keep explaining the rules; open http://localhost:9090/alerts to show it pending. Payment can take up to about 2.5 min |
| Alert fires but nothing in the AIRA console | `docker logs alertmanager --tail 20` (webhook errors?) and `.\status.ps1`. If the backend is down, run `.\start-all.ps1` again: it only starts what is missing, and resumes unfinished incidents |
| Agent block shows an error | Show it honestly: it is the validated-output safety net. `ollama ps` should list the model; re-run `.\start-all.ps1` to warm it up. The evidence block is still valid |
| Agent slow (~30 s) on the first incident | The model was unloaded. Mention that the first load takes about 30 s and later runs take about 10 s |
| Shop or UI not loading | `.\status.ps1`; if containers are down, run `.\start-all.ps1` (about 3 min) |
| Payment scenario not firing after 3 min | Switch to `high-cpu` (fires in about 72 s, very visible in Grafana and `docker stats`) |
| Everything is broken | Use the screenshots in `docs/screenshots/` and walk through `docs/midsem-progress.md`, which has the measured evidence |

After the demo: `.\clear-all-faults.ps1` (and `.\stop-all.ps1` if you are done).
