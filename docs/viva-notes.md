# Viva notes: likely questions and grounded answers

Every answer refers to what is actually built in this repository (mid-semester state, 2026-10-06).
Measured numbers come from `docs/measurements/alert-timings.jsonl` and the backend log.

## Multi-agent investigation (Phase A)

**Q: How many agents are there now, and what does each do?**
Five. **Triage** turns the alert plus metrics into a first summary and a cause category. The
**Orchestrator** then plans which investigators to run. **Metrics** interprets the Prometheus
evidence, **Log** reads the service's real log lines, and **Change** looks for what changed —
feature flags, container restarts, image versions.

**Q: What makes the Orchestrator agentic rather than a fixed pipeline?**
It chooses. A CPU alert skips the Log agent, because logs rarely explain saturation; a stopped
service skips it too, because the service stopped logging before the incident. Error alerts run all
three. Every skipped agent is recorded with its reason and shown on the console, so the decision is
visible rather than implied. Measured: a CPU incident ran 2 investigators, an error incident ran 3.

**Q: Why is the planning logic rules rather than another LLM call?**
Three reasons: the choice has to be explainable to you, it has to be repeatable between runs, and an
extra model call would spend ~7 seconds of a ~25 second budget to reproduce what four rules already
express. It becomes a model decision in the replanning phase, where the next step depends on what the
Critic found and cannot be reduced to a table.

**Q: Your synopsis says the investigators run in parallel. Do they?**
The evidence gathering does, genuinely — Prometheus, OpenSearch, container stdout and Docker
inspection are I/O, and three sources are fetched in 0.6–1.9 seconds concurrently. The reasoning is
serialised, because one local model serves every agent and there is only ~1.4 GB of spare VRAM, not
enough for a second copy. That is an honest hardware constraint, and it is why investigator outputs
are kept terse: generation time scales with tokens.

**Q: How long does a full investigation take?**
17–28 seconds measured end to end, for triage plus two or three investigators. Each agent takes
4–14 seconds.

**Q: Can you show it finding a real root cause?**
Yes, and the best example is a cascade. When the payment service is broken, **checkout** alerts too,
because it calls payment. The Change agent on the *checkout* incident reports
"paymentFailure flag is set to 100% on payment service" and marks it the likely cause — it traced the
failure across a service boundary. It knows checkout calls payment because the dependency graph is
read from the containers' own environment variables, not hard-coded.

**Q: What stops an agent inventing a log line?**
Three things. The prompt requires every quote to be copied exactly. The evidence states plainly when
a service produced no logs at all, so "none" is an available answer. And
`scripts/check_agent_numbers.py` re-runs the agents against stored incidents and verifies every
number and every quoted line against the input — it is tested against a deliberately fabricated quote
to prove it is not a no-op. Latest run: 16/16 clean across 4 agents and 4 incident types.

**Q: Which bugs did testing catch?** (state these before being asked)
* The Metrics agent reported latency "increased substantially" when the number had fallen. Fixed with
  an explicit direction rule; it now says RISEN or FALLEN correctly.
* The Change agent answered "no changes detected" while `paymentFailure` was set to 100%. Stronger
  wording alone was not enough, so a code-level consistency check now rejects that answer and forces
  the retry.
* The Change agent blamed a payment flag for a cart alert. Fixed by reading the real dependency graph
  from container environment variables, so the agent is told the relationship instead of guessing it.
* The Metrics agent invented a baseline of "4.3%". The cause was my own prompt: it contained the
  example `"error ratio (2m): 100.0% (baseline 4.3%)"` and the model copied the example instead of
  the data. All example numbers are now placeholders.
* The Change agent blamed a container restart and dropped the feature flag entirely. A routine stack
  start 13 minutes earlier counted as "recent", so the window was cut to 8 minutes and a code check
  now rejects any answer that omits a flag affecting this service or one it calls.
* On the CPU alert, triage called the 100% trigger level grafana's "baseline". The clarifying rewrite
  only matched `(threshold X)` as a whole parenthesis and missed
  `(100% = one full core; threshold 100%)`. Now matched wherever the word appears.

**Q: Has it ever caught something you did not inject?**
Yes, three times. Grafana spikes its own CPU above one full core occasionally, with nothing injected.
The most recent was investigated end to end by the multi-agent pipeline while unattended: Metrics
confirmed CPU had risen to 107.2%, the Log agent was correctly skipped because logs rarely explain
saturation, and the alert resolved itself when Grafana settled back to 2.2%. That is the system
working on a real, unplanned incident rather than a staged one.

**Q: Why two log sources?**
Because neither covers the demo. Of 870,000 documents in OpenSearch only ~2,100 carry a service name,
and all of them are `payment` — the rest are the collector's own debug records, and other services'
logs are rejected upstream by a field-mapping conflict. Container stdout covers cart, recommendation
and ad as well. Both are queried and the results de-duplicated, with occurrence counts kept because
an error seen 66 times is stronger evidence than one seen once. `checkout` and `product-catalog`
write nothing to either source, and the Log agent reports that honestly rather than guessing.

## Scope and novelty

**Q: What exactly have you built so far?**
A working pipeline from a real fault to an AI-written triage summary. The pipeline is: the
OpenTelemetry Demo (28 containers with real Locust traffic), a fault injected through feature flags or
by stopping a container, our four Prometheus alert rules, Alertmanager, a webhook to our FastAPI
backend, an incident stored in SQLite, live metrics evidence pulled from Prometheus, a CrewAI Triage
Agent on a local Ollama model, and a live console page. Start/stop/status/fault scripts make it
repeatable.

**Q: Prometheus and Alertmanager already detect the problem. What does AIRA add?**
Detection is deliberately left to the standard tools. AIRA's contribution is the agentic layer after
the alert: deciding what evidence to gather, interpreting it, and (later) planning investigation,
checking its own diagnosis, remediating and verifying. At mid-semester the first part exists: the
backend gathers evidence automatically and the agent turns it into a structured triage
(`likely_cause_category`, `suggested_next_investigation`) that the Orchestrator will consume.

**Q: Why the OpenTelemetry Demo instead of your own app?**
It is an official, maintained microservices application in many languages, it is fully instrumented,
and it ships built-in, documented fault flags. That gives us realistic and repeatable incidents
without writing a fake app. We use release 3.1.0, pinned.

**Q: Is anything mocked?**
No. Alerts come from Prometheus rules over live metrics, faults are real (flags change service
behaviour; `docker stop` really stops a container), evidence values are live PromQL results, and the
agent's text is generated by the local model at run time. When a value is missing, the tool writes
"unavailable" instead of a number.

## Monitoring and alerting

**Q: Which metrics do you use, and how did you choose them?**
We listed the 440 metric names actually present in Prometheus and chose ones every service emits:
* `traces_span_metrics_calls_total` and `traces_span_metrics_duration_milliseconds_bucket` (from the
  collector's span-metrics connector) for request rate, error ratio and latency per service and operation;
* `container_cpu_usage_nanoseconds_total` and `container_memory_usage_total_bytes` (docker_stats receiver)
  for CPU and memory.
The discovery is documented in `docs/environment-discovery.md`.

**Q: How do you detect a stopped service, when there is no `up` metric?**
Services push telemetry over OTLP, so Prometheus scrapes nothing for them and `up` does not exist.
Instead we use container metrics: the collector reports every running container every 10 s. Our rule
fires for a container that reported in the last 30 minutes but not in the last 45 s. Measured: fired
89 s after `docker stop recommendation` and resolved 31 s after it was started again. Limitation: after
30 minutes down, the alert resolves on its own.

**Q: Why not use `container_cpu_utilization_ratio` for CPU?**
We tried it. With `adHighCpu` on, `docker stats` showed ad at 404 % and the CPU-time counter showed 3.99
cores, but this gauge crept up only about 2.5 points per sample. So we use the rate of the CPU-time
counter (percent of one core). Threshold 100 %: the normal peak is 15–50 %; the fault gives ~400 %.

**Q: How fast are faults detected?** (`docs/measurements/alert-timings.jsonl`)
| Fault | Alert | Time to fire | Resolve after clearing |
|---|---|---|---|
| adHighCpu | AiraHighCpu / ad | 72 s | 74 s |
| docker stop recommendation | AiraServiceDown / recommendation | 89 s | 31 s |
| productCatalogFailure | AiraHighErrorRate / product-catalog | 117 s (frontend already at 42 s) | 90 s |
| paymentFailure 100 % | AiraOperationFailing / payment | 133 s (payment error rate ~119 s) | 120 s |
| cartFailure 100 % | AiraOperationFailing / cart | 327 s | 30 s |

The time is mostly data, not tooling: the rules need a 1–3 minute rate window plus a 15–30 s `for:`
to avoid flapping. Cart is slow because only `EmptyCart` fails, and it runs only once per checkout
(about 0.02/s).

**Q: Did you have false positives?**
Yes, one, and we fixed it. `AiraOperationFailing/flagd` fired because long-lived
`flagd.evaluation.v2.Service/EventStream` connections end with an error status when a client
reconnects. That isn't a failed request, so streaming `*/EventStream` spans are now excluded from the
error rules.

**Q: Did you change the demo?**
We never edit upstream files. Everything is in `infra/`: an env file pinning images to 3.1.0, a compose
override (Alertmanager, Prometheus rule loading, a named volume for Prometheus data), a collector extras
file (span metrics every 15 s instead of 60 s), and one resource fix: `product-catalog` ran at 130–190 %
CPU with an 800 ms median latency because upstream limits it to 16 MiB. Raising that to 48 MiB brought it
to 0.3 % CPU and 3.8 ms. We document each change with the evidence.

## Backend and agent

**Q: How does an alert become an incident?**
Alertmanager posts its standard webhook (version 4) to `POST /alerts`. We log the raw payload, then
upsert an incident keyed by the alert's fingerprint plus `startsAt`, so a later re-fire is a new
incident and a `resolved` message closes the existing one. New firing incidents start a background
task, so the webhook returns immediately.

**Q: Why is the Prometheus tool not an LLM tool call?**
Numbers must be exact and reproducible. The tool runs fixed PromQL queries (current value plus a
15–25-minute-ago baseline) and produces both raw values and a text summary. The LLM only interprets
that text and never computes or fetches numbers itself, which removes a big source of hallucination.

**Q: How do you stop the LLM from inventing things?**
1. The prompt gives it only the alert and the evidence, with explicit rules: use only these facts,
   quote values exactly, say "unavailable" rather than guess, propose investigations not fixes.
2. The output must be JSON matching a Pydantic schema (fixed category list, confidence low/medium/high).
3. Invalid output gets exactly one retry with the validation error. After that the incident shows an
   error state and nothing is fabricated.
4. Low temperature (0.1).
In our runs, all triages validated on the first attempt and quoted the evidence numbers.

**Q: Which model, and why?**
`qwen2.5:7b-instruct` (Q4, 4.7 GB) on Ollama 0.9.6. The laptop has an RTX 4060 with 8 GB, so the whole
model runs on the GPU (about 45 tokens/s). One triage takes about 7–14 s once the model is loaded; the
first load takes about 30 s, so `start-all.ps1` warms it up. It is local, free and private, as the
synopsis requires.

**Q: How is CrewAI configured with Ollama?**
CrewAI 1.15.23 has a native OpenAI-compatible provider: `LLM(model="ollama/qwen2.5:7b-instruct",
base_url="http://localhost:11434")` talks to Ollama's `/v1` API (we read this from the installed CrewAI
source). One Agent ("Incident Triage Agent", no delegation, `max_iter=2`), one Task, one sequential
Crew. Telemetry and tracing are disabled with env vars so nothing leaves the machine.

**Q: Why only one agent? The synopsis has eight.**
The mid-semester milestone proves the hardest integration path end to end: real faults, real alerts,
real evidence, a local LLM agent and structured output. The Orchestrator, investigators, RCA, Critic,
Remediation and Memory agents are built on this foundation in weeks 3–8 (see
`docs/architecture-midsem.md`). The triage output is designed to be the Orchestrator's planning input.

**Q: What if several alerts arrive at once?**
They all become incidents immediately, and agent runs are serialised (one at a time) because the local
model serves requests sequentially. During the payment fault, four incidents (payment, checkout x2,
flagd) were triaged one after another in about 7 s each.

**Q: What happens if the backend restarts during an incident?**
Two things, both tested. It resumes evidence collection and investigation for every firing incident
that had not finished (incidents #2 and #4 were picked up after a restart). And it reconciles against
Alertmanager: if an alert resolved while the backend was down, that webhook was lost, so the incident
would otherwise stay "firing" forever. After one stop/start cycle five incidents were stale this way;
the backend now closes them on startup and records why on the incident.

## Limitations (say these before being asked)

* **No RCA or Critic yet.** The investigators each report their own findings into a shared context,
  but nothing yet combines them into ranked hypotheses or challenges the diagnosis. That is the next
  phase, and the `round` column on `agent_findings` already exists so an agent can be re-run.
* **The agents still cannot act.** No remediation: you clear the fault, and AIRA confirms recovery
  from the metrics.
* **Two services have no logs at all.** `checkout` and `product-catalog` write nothing to OpenSearch
  or stdout, so the Log agent can only report their absence.
* **Investigator reasoning is serial**, limited by one local model and ~1.4 GB of spare VRAM.
* The Triage agent still sees one service's metrics only, so on its own it cannot tell a dependency
  failure from a local error. The Change agent now closes much of that gap by tracing a flag across
  the real dependency graph.
* Baselines need about 25 minutes of Prometheus history; right after a fresh start they show
  "unavailable".
* The cart scenario takes about 5.5 minutes to alert because of low traffic on the failing operation.
* There are no memory-based alerts yet: many demo containers normally sit at 80–88 % of their memory
  limit, so a simple threshold would be noisy. The memory-leak and homepage-flood scenarios can be
  injected but have no dedicated AIRA rule.
* The demo's logs go to OpenSearch (not Loki as the synopsis says). Some log records are rejected
  because of an upstream field-mapping conflict. Logs are not yet used by AIRA.
* CrewAI writes one small task-output cache file (~20 KB) under `%LOCALAPPDATA%\CrewAI\backend` on C:.
  That path comes from `appdirs` inside the library and cannot be redirected. WSL2 keeps its swap
  file in `%TEMP%` on C: (~1.2 GB) by default. All project data, images, models and caches are on D:.
