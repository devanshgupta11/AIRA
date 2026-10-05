# Environment discovery: OpenTelemetry Demo (Astronomy Shop)

Everything below was read from the cloned repository or queried from the running stack on
2026-10-06. Nothing was assumed. Re-run the queries at the bottom to re-check after an upgrade.

## Version

| Item | Value |
|---|---|
| Repository | https://github.com/open-telemetry/opentelemetry-demo |
| Tag checked out | **3.1.0** (commit `dedc0178918e260823323b8d95005a8cb924b007`, 2026-09-18) |
| Clone location | `D:\AIRA\opentelemetry-demo` (unmodified upstream) |
| Images | `ghcr.io/open-telemetry/demo:3.1.0-<service>` (pinned by `infra/aira.env`; upstream `.env` uses `DEMO_VERSION=latest`) |
| Collector | `opentelemetry-collector-contrib:0.159.0` |
| Prometheus | `quay.io/prometheus/prometheus:v3.13.1` |
| Grafana / Jaeger / flagd | `grafana/grafana:13.1.0` / `jaegertracing/jaeger:2.19.0` / `open-feature/flagd:v0.16.0` |

Known cosmetic quirk: the `service_version` label on telemetry shows `3.0.0`. Upstream `.env` builds
`OTEL_RESOURCE_ATTRIBUTES` from its own `IMAGE_VERSION=3.0.0` while the file is parsed, before our env
file is applied. The running images really are the 3.1.0 tags (`docker inspect` shows the image names below).

## How the demo is started

From the upstream `Makefile` (`make start`), the default stack is four compose files plus two env files:

```
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.full.yaml -f compose.observability.yaml -f compose.extras.yaml up -d
```

AIRA adds `--env-file ../infra/aira.env` and `-f ../infra/compose.aira.yaml` (see `infra/`).
The images are pulled pre-built and never built from source.

## URLs (all through the Envoy frontend-proxy on port 8080, except Prometheus)

| UI | URL |
|---|---|
| Astronomy Shop | http://localhost:8080/ |
| Grafana | http://localhost:8080/grafana/ |
| Jaeger | http://localhost:8080/jaeger/ui/ |
| Feature flags (flagd-ui) | http://localhost:8080/feature/ |
| Load generator (Locust) | http://localhost:8080/loadgen/ |
| Prometheus | http://localhost:9090/ (published directly by `compose.observability.yaml`) |
| Alertmanager (AIRA) | http://localhost:9093/ |

Other services publish container ports to **random** host ports (e.g. `telemetry-docs` 8000 maps to a
random host port), so nothing conflicts with the AIRA backend on host port 8000.

## Services (28 demo containers; AIRA adds `alertmanager` = 29)

| Container | Image | Published ports (host side is random unless shown) |
|---|---|---|
| accounting | `demo:3.1.0-accounting` | – |
| ad | `demo:3.1.0-ad` | 9555, 9465 |
| astronomy-db | `postgres:18.4` | 5432 |
| cart | `demo:3.1.0-cart` | 7070 |
| checkout | `demo:3.1.0-checkout` | 5050 |
| currency | `demo:3.1.0-currency` | 7001 |
| email | `demo:3.1.0-email` | 6060 |
| flagd | `open-feature/flagd:v0.16.0` | 8013 (gRPC), 8016 (OFREP HTTP) |
| flagd-ui | `demo:3.1.0-flagd-ui` | 4000 |
| fraud-detection | `demo:3.1.0-fraud-detection` | – |
| frontend | `demo:3.1.0-frontend` | 8080 |
| frontend-proxy | `demo:3.1.0-frontend-proxy` | **8080:8080**, **10000:10000** |
| grafana | `grafana/grafana:13.1.0` | 3000 |
| image-provider | `demo:3.1.0-image-provider` | 8081 |
| jaeger | `jaegertracing/jaeger:2.19.0` | 4317, 16686 |
| kafka | `demo:3.1.0-kafka` | – |
| load-generator | `demo:3.1.0-load-generator` | 8089 |
| opamp-server | `demo:3.1.0-opamp-server` | – |
| opensearch | `demo:3.1.0-opensearch` | 9200 |
| otel-collector | `opentelemetry-collector-contrib:0.159.0` | 4317, 4318 |
| payment | `demo:3.1.0-payment` | 50051 |
| product-catalog | `demo:3.1.0-product-catalog` | 3550 |
| prometheus | `prometheus:v3.13.1` | **9090:9090** |
| quote | `demo:3.1.0-quote` | 8090 |
| recommendation | `demo:3.1.0-recommendation` | 9001 |
| shipping | `demo:3.1.0-shipping` | 50050 |
| telemetry-docs | `demo:3.1.0-telemetry-docs` | 8000 |
| valkey-cart | `valkey:9.0.4-alpine3.23` | 6379 |

## How telemetry reaches Prometheus

* Services push OTLP to `otel-collector`. The collector **pushes** metrics to Prometheus's OTLP receiver
  (`--web.enable-otlp-receiver`, exporter `otlp_http/prometheus`). Prometheus scrapes nothing, so the
  classic `up` metric does not exist for demo services.
* Traces go to Jaeger. The collector's `span_metrics` connector turns spans into RED metrics.
* Logs go to OpenSearch (not Loki; the demo does not ship Loki).
* The collector's `docker_stats` receiver reads `/var/run/docker.sock` every 10s and produces per-container
  CPU and memory metrics labelled `container_name`.
* Resource attributes promoted to labels (from `src/prometheus/prometheus-config.yaml`) include
  `service_name`, `service_namespace`, `service_version`, `container_name` and `host_name`.

## Metric names used by AIRA (verified via `/api/v1/label/__name__/values`, 440 names)

### Request rate, error rate, latency: span metrics (all services, uniform)

| Metric | Labels used |
|---|---|
| `traces_span_metrics_calls_total` (counter) | `service_name`, `span_name`, `span_kind`, `status_code` |
| `traces_span_metrics_duration_milliseconds_bucket` / `_sum` / `_count` (histogram, ms) | same + `le` |

Observed label values:
* `span_kind`: `SPAN_KIND_SERVER`, `SPAN_KIND_CLIENT`, `SPAN_KIND_INTERNAL`, `SPAN_KIND_CONSUMER`
* `status_code`: `STATUS_CODE_OK`, `STATUS_CODE_UNSET`, `STATUS_CODE_ERROR`
* `service_name`: payment, accounting, ad, shipping, quote, flagd-ui, recommendation, image-provider,
  currency, product-catalog, cart, telemetry-docs, frontend, flagd, checkout, frontend-proxy,
  load-generator, frontend-web

PromQL used (server spans = the requests a service handles):

```promql
# request rate (req/s)
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[2m]))
# error ratio (0..1)
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR"}[2m]))
  / sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[2m]))
# p95 latency (ms)
histogram_quantile(0.95, sum by (service_name, le) (rate(traces_span_metrics_duration_milliseconds_bucket{span_kind="SPAN_KIND_SERVER"}[2m])))
```

Note: a service with zero errors has **no** `STATUS_CODE_ERROR` series, so the error ratio query returns
nothing for it. The AIRA Prometheus tool treats "no error series but traffic present" as 0 errors.

Other per-service RED metrics also exist but are not uniform across languages:
`http_server_request_duration_seconds_*`, `rpc_server_call_duration_seconds_*`.

### CPU and memory: container metrics (docker_stats receiver)

| Metric | Meaning | Label |
|---|---|---|
| `container_cpu_usage_nanoseconds_total` | cumulative CPU time (ns). **AIRA uses** `rate(...[1m]) / 1e9 * 100` = percent of one core | `container_name` |
| `container_cpu_utilization_ratio` | gauge, **not used**: with `adHighCpu` on, the counter showed ad at 3.99 cores (`docker stats` showed 404 %), while this gauge crept up about 2.5 points per 10 s sample (2 → 15), so it lags far behind reality | `container_name` |
| `container_memory_usage_total_bytes` | memory used, bytes | `container_name` |
| `container_memory_usage_limit_bytes` | memory limit, bytes | `container_name` |
| `container_memory_percent_ratio` | memory used / limit, percent | `container_name` |

`container_name` equals the compose service name (`ad`, `cart`, `payment`, ...).

Process-level alternatives exist (`process_cpu_time_seconds_total`, `process_memory_usage_bytes`,
`jvm_cpu_recent_utilization_ratio`) but are not emitted uniformly by every language SDK.

### Sample frequency

* Container metrics: every **10 s**.
* Span metrics: every **60 s** upstream. AIRA sets `metrics_flush_interval: 15s` through
  `infra/otelcol-config-extras.yml` (loaded via `OTEL_COLLECTOR_CONFIG_EXTRAS`), so 1–2 minute windows have
  enough samples for fast alerts.
* App SDK metrics: every 60 s (OTel default).

## Feature flags (from `src/flagd/demo.flagd.json`)

| Flag | Variants | What it does (from source) |
|---|---|---|
| `adHighCpu` | off / on | High CPU load in the ad service (Java) |
| `adFailure` | off / on | Ad service fails |
| `adManualGc` | off / on | Full manual GCs in ad service |
| `cartFailure` | off, 10%…100% | `EmptyCart` uses a bad store, n% of the time (`src/cart/src/services/CartService.cs`) |
| `paymentFailure` | off, 10%…100% | `charge` throws "Invalid token" n% of the time (`src/payment/charge.js`) |
| `paymentUnreachable` | off / on | Checkout calls an unreachable payment address |
| `productCatalogFailure` | off / on | `GetProduct` fails for product `OLJCESPC7Z` (targeting rule) |
| `productCatalogLockContention` | off / on | Lock contention on the product DB |
| `recommendationCacheFailure` | off / on | Recommendation cache grows without bound, a memory leak (`src/recommendation/recommendation_server.py`) |
| `emailMemoryLeak` | off, 1x…10000x | Memory leak in email service |
| `loadGeneratorFloodHomepage` | off / on | Locust floods the homepage |
| `imageSlowLoad` | off / 5sec / 10sec | Slow image loading |
| `intlShippingSlowdown` | off / 5sec / 10sec | Slow international shipping |
| `kafkaQueueProblems` | off / on | Kafka queue overload + consumer lag |
| `failedReadinessProbe` | off / on | Cart readiness probe fails |
| `emitRawPii` | off / on | Not a fault: PII emission example |
| `aiRunawayAgent`, `aiSlowResponse` | – | Only for the optional agent stack (`compose.agent.yaml`), not running |

### Toggling flags programmatically

Read from `src/flagd-ui/lib/flagd_ui_web/router.ex`, `controllers/feature_controller.ex` and
`lib/flagd_ui/storage.ex`. The same method is used by upstream's Cypress tests
(`src/frontend/cypress/e2e/CheckoutPaymentFailure.cy.ts`) and documented in `src/flagd-ui/README.md`.

* `GET  http://localhost:8080/feature/api/read`: returns `{"flags": {...}}`
* `POST http://localhost:8080/feature/api/write` with body `{"data": {"$schema": "...", "flags": {...}}}`:
  replaces the whole flag file. flagd watches the file (bind-mounted `src/flagd/`) and reloads.
* To switch a variant: set `defaultVariant`. For flags with an `if/then/else` targeting rule
  (only `productCatalogFailure`), set `targeting.if[1]` instead, exactly as the UI does.
* **Verification:** flagd's OFREP API (container port 8016, published to a random host port, found with
  `docker port flagd 8016`): `POST /ofrep/v1/evaluate/flags/<flag>` with `{"context":{}}` returns the
  variant flagd is actually serving.

AIRA implements this in `scripts/flagctl.py`. It was tested on 2026-10-06: `adHighCpu` was switched on and
then off, and flagd confirmed each change within seconds.

Note: flagd-ui rewrites `src/flagd/demo.flagd.json` in compact form, so `git status` in the upstream
clone shows it as modified even when every flag is off. The content is semantically identical
(verified by comparing the parsed JSON).

## Issues found during discovery and how they were handled

| Issue | Evidence | Handling |
|---|---|---|
| `product-catalog` degraded at baseline | 130–190 % CPU at 15.4/20 MiB with `GOMEMLIMIT=16MiB`; Jaeger GetProduct median 800 ms | `infra/compose.aira.yaml` raises it to `GOMEMLIMIT=48MiB` / 64M. Result: 0.3 % CPU, median 3.8 ms, p95 21 ms |
| `container_cpu_utilization_ratio` lags far behind real CPU | see the CPU table above | Use the rate of `container_cpu_usage_nanoseconds_total` instead |
| Collector misses containers that start at the same moment it does | `cart`, `payment`, `flagd-ui` had no `container_*` series. After `docker restart otel-collector` they appeared within 10 s | `scripts/start-all.ps1` restarts the collector once all containers are up |
| Span metrics only every 60 s | 1 sample per series per minute | `metrics_flush_interval: 15s` via the extras config seam |
| OpenSearch rejects some log records | collector logs `mapper_parsing_exception … [attributes.http] … found [text]` | Upstream mapping conflict. Other logs are still indexed. Not used by the mid-semester pipeline |
| `/prompt` requests fail in Locust | `gaierror … No address associated with hostname` | Upstream Locust task for the optional AI chatbot (`compose.agent.yaml`), which is not started. Expected |
