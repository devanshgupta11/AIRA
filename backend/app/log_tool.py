"""Deterministic log collection for the Log Agent.

Two sources are queried, because neither covers the demo on its own (verified 2026-10-06):

* **OpenSearch** (`otel-logs-*`) holds structured OTLP logs, but of 870k documents only ~2.1k carry
  a service name and all of them are `payment`. The rest are the collector's own debug-exporter
  records (`body: "Traces"`). Other services' log records are rejected upstream with
  `mapper_parsing_exception`.
* **Container stdout** (`docker logs`) covers `payment`, `cart`, `recommendation`, `ad` and others,
  with `--since/--until/--timestamps` giving a precise incident window.

`checkout` and `product-catalog` write nothing to either source. When that happens this tool says so
explicitly rather than returning an empty result that could be mistaken for "no errors".

No LLM is involved here. The agent interprets what this returns.
"""

import json
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone

# Words that mark a log line as interesting. Severity fields are unusable here: a failing service
# logs at "info" level, and severity>=17 matches only the collector's own export failures.
ERROR_TERMS = (
    "error", "fail", "failed", "failure", "exception", "invalid", "timeout", "timed out",
    "refused", "unable", "denied", "panic", "fatal", "unavailable", "rejected", "cannot",
)
ERROR_RE = re.compile("|".join(re.escape(t) for t in ERROR_TERMS), re.I)

MAX_LINES = 8          # kept small: this text goes into a prompt
SNIPPET = 220


def _opensearch_url() -> str | None:
    """OpenSearch publishes 9200 on a random host port, so it must be discovered each run."""
    try:
        out = subprocess.run(["docker", "port", "opensearch", "9200"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return "http://localhost:" + out.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
    except Exception:  # noqa: BLE001
        return None


def _window(starts_at: str, before_min: int = 5, after_min: int = 10) -> tuple[datetime, datetime]:
    t = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    end = min(t + timedelta(minutes=after_min), now)
    return t - timedelta(minutes=before_min), end


def _from_opensearch(service: str, start: datetime, end: datetime) -> dict:
    base = _opensearch_url()
    if not base:
        return {"available": False, "reason": "OpenSearch port could not be discovered", "lines": [], "total": 0}
    body = {
        "size": 200,
        "query": {"bool": {"filter": [
            {"term": {"attributes.service.name": service}},
            {"range": {"@timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}},
        ]}},
        "sort": [{"@timestamp": "desc"}],
    }
    req = urllib.request.Request(f"{base}/otel-logs-*/_search",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"query failed: {e}", "lines": [], "total": 0}
    hits = data.get("hits", {})
    lines = [
        {"ts": h["_source"].get("@timestamp"), "text": str(h["_source"].get("body", "")), "source": "opensearch"}
        for h in hits.get("hits", [])
    ]
    return {"available": True, "total": hits.get("total", {}).get("value", 0), "lines": lines}


def _from_container(service: str, start: datetime, end: datetime) -> dict:
    try:
        out = subprocess.run(
            ["docker", "logs", service, "--timestamps",
             "--since", start.isoformat().replace("+00:00", "Z"),
             "--until", end.isoformat().replace("+00:00", "Z")],
            capture_output=True, text=True, timeout=25, errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"docker logs failed: {e}", "lines": [], "total": 0}
    if out.returncode != 0:
        return {"available": False, "reason": (out.stderr or "").strip()[:160], "lines": [], "total": 0}

    raw = [ln for ln in (out.stdout + out.stderr).splitlines() if ln.strip()]
    lines = []
    for ln in raw:
        ts, _, rest = ln.partition(" ")
        lines.append({"ts": ts, "text": _clean(rest), "source": "container"})
    return {"available": True, "total": len(lines), "lines": lines}


def _clean(text: str) -> str:
    """Pull the human message out of a JSON log line (payment logs JSON; others log plain text)."""
    t = text.strip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
            msg = obj.get("message") or obj.get("msg") or obj.get("body")
            if msg:
                level = obj.get("level") or obj.get("severity")
                return f"[{level}] {msg}" if level else str(msg)
        except Exception:  # noqa: BLE001
            pass
    return t


LEVEL_PREFIX = re.compile(r"^\[(?:trace|debug|info|warn|warning|error|fatal)\]\s*", re.I)


def _dedupe_key(text: str) -> str:
    """The same message arrives from both sources in slightly different shapes - OpenSearch stores
    the bare body while the container line carries a "[warn]" prefix - so strip the level before
    comparing, or the identical error is reported twice."""
    return LEVEL_PREFIX.sub("", text).strip()[:160]


def collect(service: str | None, starts_at: str) -> dict:
    """Gather logs around an incident from both sources. Deterministic; safe to run in parallel."""
    out = {
        "service": service,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {},
        "error_lines": [],
        "sample_lines": [],
        "counts": {"total": 0, "raw_lines": 0, "matching_error_terms": 0},
    }
    if not service:
        out["text"] = "No service name on the alert, so no logs could be looked up."
        return out

    start, end = _window(starts_at)
    out["window"] = {"from": start.isoformat(), "to": end.isoformat()}

    merged: list[dict] = []
    for name, fetch in (("opensearch", _from_opensearch), ("container", _from_container)):
        res = fetch(service, start, end)
        out["sources"][name] = {k: res[k] for k in ("available", "total") if k in res}
        if not res.get("available"):
            out["sources"][name]["reason"] = res.get("reason", "unavailable")
        merged.extend(res.get("lines", []))

    # Services repeat the same few messages, so collapse by text and keep the count: an error
    # seen 40 times is far stronger evidence than one seen once, and the count is what a human
    # would look at first.
    by_text: dict[str, dict] = {}
    for ln in merged:
        key = _dedupe_key(ln["text"])
        if not key:
            continue
        if key in by_text:
            entry = by_text[key]
            entry["count"] += 1
            if ln["ts"] and (not entry["ts"] or ln["ts"] > entry["ts"]):
                entry["ts"] = ln["ts"]
            if ln["source"] not in entry["sources"]:
                entry["sources"].append(ln["source"])
        else:
            by_text[key] = {"ts": ln["ts"], "text": ln["text"], "count": 1, "sources": [ln["source"]]}
    unique = sorted(by_text.values(), key=lambda x: (x["count"], x["ts"] or ""), reverse=True)

    errors = [ln for ln in unique if ERROR_RE.search(ln["text"])]
    out["counts"]["total"] = len(unique)
    out["counts"]["raw_lines"] = len(merged)
    out["counts"]["matching_error_terms"] = len(errors)
    out["error_lines"] = errors[:MAX_LINES]
    out["sample_lines"] = unique[:3]
    out["text"] = to_text(out)
    return out


def to_text(ev: dict) -> str:
    svc = ev["service"]
    c = ev["counts"]
    src = ev["sources"]
    lines = [f"Log evidence for service '{svc}' (collected {ev['collected_at']} UTC)."]
    if "window" in ev:
        lines.append(f"Window searched: {ev['window']['from']} to {ev['window']['to']}.")
    avail = [f"{n}: {'{} lines'.format(s.get('total', 0)) if s.get('available') else s.get('reason', 'unavailable')}"
             for n, s in src.items()]
    lines.append("Sources - " + "; ".join(avail))

    if c["total"] == 0:
        lines.append("RESULT: this service produced NO log lines in either source for this window. "
                     "That is an absence of data, not evidence that nothing went wrong.")
        return "\n".join(lines)

    lines.append(f"{c['total']} distinct messages ({c['raw_lines']} lines in total), "
                 f"{c['matching_error_terms']} of them containing error wording.")
    shown = ev["error_lines"] or ev["sample_lines"]
    lines.append("Messages containing error wording, most frequent first:" if ev["error_lines"]
                 else "No message contained error wording. Most frequent messages, for context:")
    for ln in shown:
        times = "once" if ln["count"] == 1 else f"{ln['count']} times"
        lines.append(f"  (seen {times}, last at {ln['ts']}) {ln['text'][:SNIPPET]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    svc = sys.argv[1] if len(sys.argv) > 1 else "payment"
    when = sys.argv[2] if len(sys.argv) > 2 else datetime.now(timezone.utc).isoformat()
    print(collect(svc, when)["text"])
