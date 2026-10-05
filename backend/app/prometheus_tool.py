"""Deterministic evidence collection from the real Prometheus HTTP API.

Every value comes from a live PromQL query. If a query returns nothing the value is reported
as unavailable, never estimated. Metric names were discovered from the running stack
(docs/environment-discovery.md).
"""

from datetime import datetime, timezone

import httpx

from .config import PROMETHEUS_URL

SERVER = 'span_kind="SPAN_KIND_SERVER"'
ERROR = 'status_code="STATUS_CODE_ERROR"'


def _queries(svc: str) -> dict[str, str]:
    s = f'service_name="{svc}"'
    c = f'container_name="{svc}"'
    calls = f"traces_span_metrics_calls_total{{{s},{SERVER}}}"
    errors = f"traces_span_metrics_calls_total{{{s},{SERVER},{ERROR}}}"
    return {
        "request_rate_rps": f"sum(rate({calls}[2m]))",
        "request_rate_rps_baseline": f"sum(rate({calls}[10m] offset 15m))",
        "error_rate_rps": f"sum(rate({errors}[2m]))",
        "error_ratio": f"sum(rate({errors}[2m])) / sum(rate({calls}[2m]))",
        "error_ratio_baseline": f"sum(rate({errors}[10m] offset 15m)) / sum(rate({calls}[10m] offset 15m))",
        "latency_p95_ms": (
            "histogram_quantile(0.95, sum by (le) (rate("
            f"traces_span_metrics_duration_milliseconds_bucket{{{s},{SERVER}}}[2m])))"
        ),
        "latency_p95_ms_baseline": (
            "histogram_quantile(0.95, sum by (le) (rate("
            f"traces_span_metrics_duration_milliseconds_bucket{{{s},{SERVER}}}[10m] offset 15m)))"
        ),
        "cpu_percent_of_core": f"sum(rate(container_cpu_usage_nanoseconds_total{{{c}}}[1m])) / 1e9 * 100",
        "cpu_percent_of_core_baseline": f"sum(rate(container_cpu_usage_nanoseconds_total{{{c}}}[10m] offset 15m)) / 1e9 * 100",
        "memory_bytes": f"sum(container_memory_usage_total_bytes{{{c}}})",
        "memory_limit_bytes": f"sum(container_memory_usage_limit_bytes{{{c}}})",
        "memory_bytes_baseline": f"sum(avg_over_time(container_memory_usage_total_bytes{{{c}}}[10m] offset 15m))",
        "container_seconds_since_last_report": (
            f"time() - max(max_over_time(timestamp(container_memory_usage_total_bytes{{{c}}})[30m:10s]))"
        ),
    }


def _failing_ops_query(svc: str) -> str:
    s = f'service_name="{svc}"'
    return (
        f"topk(5, sum by (span_name) (rate(traces_span_metrics_calls_total{{{s},{SERVER},{ERROR}}}[3m]))"
        f" / sum by (span_name) (rate(traces_span_metrics_calls_total{{{s},{SERVER}}}[3m])) > 0)"
    )


def _scalar(client: httpx.Client, q: str) -> float | None:
    r = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": q})
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if not result:
        return None
    v = float(result[0]["value"][1])
    return None if v != v else v  # NaN (e.g. 0/0) -> unavailable


def collect(service: str | None) -> dict:
    """Query Prometheus for one service. Returns {"values", "queries", "failing_operations", ...}."""
    out = {
        "service": service,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prometheus": PROMETHEUS_URL,
        "values": {},
        "queries": {},
        "failing_operations": [],
        "errors": [],
    }
    if not service:
        out["errors"].append("alert has no service_name label")
        return out

    with httpx.Client(timeout=10) as client:
        for name, q in _queries(service).items():
            try:
                out["values"][name] = _scalar(client, q)
            except Exception as e:  # noqa: BLE001 - report every failure honestly
                out["values"][name] = None
                out["errors"].append(f"{name}: {e}")
            out["queries"][name] = q
        try:
            r = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": _failing_ops_query(service)})
            r.raise_for_status()
            out["failing_operations"] = [
                {"operation": x["metric"].get("span_name"), "error_ratio": float(x["value"][1])}
                for x in r.json()["data"]["result"]
            ]
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"failing_operations: {e}")

    v = out["values"]
    # A STATUS_CODE_ERROR series only exists once a service has failed at least once. If there was
    # traffic in a window but no error series, the measured error count is zero, not missing data.
    if v.get("error_rate_rps") is None and v.get("request_rate_rps") is not None:
        v["error_rate_rps"] = 0.0
        v["error_ratio"] = 0.0
    if v.get("error_ratio_baseline") is None and v.get("request_rate_rps_baseline") is not None:
        v["error_ratio_baseline"] = 0.0
    out["text"] = to_text(out)
    return out


def _fmt(v, unit="", digits=2, scale=1.0):
    return "unavailable" if v is None else f"{v * scale:.{digits}f}{unit}"


def _mb(v):
    return "unavailable" if v is None else f"{v / 1048576:.1f} MiB"


def to_text(ev: dict) -> str:
    v = ev["values"]
    svc = ev["service"]
    lines = [f"Live Prometheus evidence for service '{svc}' (collected {ev['collected_at']} UTC).",
             "Format: current value (baseline = average 15-25 minutes ago)."]
    lines.append(f"- Request rate (server spans, 2m): {_fmt(v.get('request_rate_rps'), ' req/s', 3)} "
                 f"(baseline {_fmt(v.get('request_rate_rps_baseline'), ' req/s', 3)})")
    lines.append(f"- Error ratio (2m): {_fmt(v.get('error_ratio'), '%', 1, 100)} "
                 f"(baseline {_fmt(v.get('error_ratio_baseline'), '%', 1, 100)}); "
                 f"errors/s now {_fmt(v.get('error_rate_rps'), '', 3)}")
    lines.append(f"- p95 latency (2m): {_fmt(v.get('latency_p95_ms'), ' ms', 1)} "
                 f"(baseline {_fmt(v.get('latency_p95_ms_baseline'), ' ms', 1)})")
    age = v.get("container_seconds_since_last_report")
    reporting = age is not None and age <= 45
    # Current container values are only meaningful while the container is reporting; otherwise
    # Prometheus returns the last (stale) sample, e.g. 0 bytes written as the container stopped.
    cpu_now = _fmt(v.get("cpu_percent_of_core"), "%", 1) if reporting else "not reporting"
    mem_now = (f"{_mb(v.get('memory_bytes'))} of limit {_mb(v.get('memory_limit_bytes'))}"
               if reporting else "not reporting")
    lines.append(f"- Container CPU (1m, 100% = one core): {cpu_now} "
                 f"(baseline {_fmt(v.get('cpu_percent_of_core_baseline'), '%', 1)})")
    lines.append(f"- Container memory: {mem_now} (baseline {_mb(v.get('memory_bytes_baseline'))})")
    if age is None:
        lines.append("- Container metrics: none in the last 30 minutes (unavailable)")
    elif age > 45:
        lines.append(f"- Container metrics: LAST REPORT {age:.0f}s AGO - the container is not running")
    else:
        lines.append(f"- Container metrics: reporting normally (last report {age:.0f}s ago)")
    if ev["failing_operations"]:
        ops = "; ".join(f"{o['operation']} {o['error_ratio'] * 100:.0f}% errors" for o in ev["failing_operations"])
        lines.append(f"- Failing operations (3m): {ops}")
    else:
        lines.append("- Failing operations (3m): none")
    if ev["errors"]:
        lines.append(f"- Query problems: {'; '.join(ev['errors'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    print(collect(sys.argv[1] if len(sys.argv) > 1 else "frontend")["text"])
