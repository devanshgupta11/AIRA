"""Deterministic change detection for the Change Agent.

In this environment there are no rolling deployments, so "what changed" means three things, checked
in order of how often they actually explain an incident:

1. **Feature flags.** Every injected fault is a flag toggle, so a non-default flag is usually the
   real cause. flagd is the source of truth: it is asked what it is currently serving, which is the
   same check `scripts/flagctl.py` performs after changing a flag.
2. **Container restarts.** A container that started shortly before the incident is suspicious; one
   with a non-zero restart count has been crashing.
3. **Image versions.** Recorded so a genuine version difference would be visible.

No LLM is involved here. The agent interprets what this returns.
"""

import json
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Which service each flag affects, read from src/flagd/demo.flagd.json descriptions and the service
# source. Used to say whether a flag that is on is relevant to *this* incident.
FLAG_SERVICE = {
    "adFailure": "ad", "adHighCpu": "ad", "adManualGc": "ad",
    "cartFailure": "cart", "failedReadinessProbe": "cart",
    "paymentFailure": "payment", "paymentUnreachable": "checkout",
    "productCatalogFailure": "product-catalog", "productCatalogLockContention": "product-catalog",
    "recommendationCacheFailure": "recommendation",
    "emailMemoryLeak": "email",
    "imageSlowLoad": "frontend", "loadGeneratorFloodHomepage": "load-generator",
    "intlShippingSlowdown": "shipping", "kafkaQueueProblems": "kafka",
    "emitRawPii": "payment",
}
# productCatalogFailure only triggers for this product, so flagd must be asked with that context or
# it reports "off" even when the fault is active.
FLAG_CONTEXT = {"productCatalogFailure": {"product_id": "OLJCESPC7Z"}}

FLAG_FILE = Path(__file__).resolve().parents[2] / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"
# A restart counts as "recent" only if it was shortly before the incident. 30 minutes proved far too
# generous: a routine stack start 13 minutes earlier was presented as a recent restart and the agent
# blamed it instead of the feature flag that was actually set.
RECENT_RESTART_MIN = 8


def _docker(args: list[str], timeout: int = 15) -> str | None:
    try:
        out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _flagd_port() -> str | None:
    out = _docker(["port", "flagd", "8016"])
    return out.splitlines()[0].rsplit(":", 1)[1] if out else None


def _flag_names() -> list[str]:
    try:
        return sorted(json.loads(FLAG_FILE.read_text(encoding="utf-8"))["flags"])
    except Exception:  # noqa: BLE001
        return sorted(FLAG_SERVICE)


def _evaluate(port: str, name: str) -> dict | None:
    body = json.dumps({"context": FLAG_CONTEXT.get(name, {})}).encode()
    req = urllib.request.Request(f"http://localhost:{port}/ofrep/v1/evaluate/flags/{name}",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001
        return None


def active_flags() -> tuple[list[dict], str | None]:
    """Flags flagd is currently serving as anything other than 'off'."""
    port = _flagd_port()
    if not port:
        return [], "flagd port could not be discovered"
    active = []
    for name in _flag_names():
        r = _evaluate(port, name)
        if r and r.get("variant") and r["variant"] != "off":
            active.append({"flag": name, "variant": r["variant"], "value": r.get("value"),
                           "affects": FLAG_SERVICE.get(name, "unknown")})
    return active, None


_ADDR_RE = re.compile(r"^([A-Z0-9_]+)_ADDR=(?:https?://)?([a-z0-9-]+)[:/]", re.I)
_deps_cache: dict[str, list[str]] = {}


def dependencies(service: str) -> list[str]:
    """Services this one calls, read from its own container environment.

    The demo wires every dependency as a <NAME>_ADDR variable (checkout has PAYMENT_ADDR,
    CART_ADDR and so on), so the real call graph can be read from the running containers rather
    than hard-coded. Without this the agent guessed, and blamed a payment flag for a cart alert
    even though cart only talks to valkey-cart.
    """
    if service in _deps_cache:
        return _deps_cache[service]
    env_json = _docker(["inspect", service, "--format", "{{json .Config.Env}}"])
    running = (_docker(["ps", "--format", "{{.Names}}"]) or "").split()
    deps: list[str] = []
    if env_json:
        try:
            env = json.loads(env_json)
            deps = sorted({m.group(2) for e in env
                           if (m := _ADDR_RE.match(e)) and m.group(2) in running and m.group(2) != service})
        except Exception:  # noqa: BLE001
            deps = []
    _deps_cache[service] = deps
    return deps


def container_state(service: str) -> dict:
    fmt = "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.Config.Image}}"
    out = _docker(["inspect", service, "--format", fmt])
    if not out:
        return {"found": False}
    status, started, restarts, image = (out.split("|") + ["", "", "", ""])[:4]
    return {"found": True, "status": status, "started_at": started,
            "restart_count": int(restarts or 0), "image": image}


def collect(service: str | None, starts_at: str) -> dict:
    """Gather change evidence around an incident. Deterministic; safe to run in parallel."""
    out = {
        "service": service,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active_flags": [], "flag_error": None, "container": {}, "notes": [],
    }
    out["active_flags"], out["flag_error"] = active_flags()
    if service:
        out["container"] = container_state(service)
        out["dependencies"] = dependencies(service)
        # State each flag's relationship to the alerting service explicitly, so the agent reads a
        # fact instead of guessing which services call which.
        for f in out["active_flags"]:
            target = f["affects"]
            if target == service:
                f["relation"] = "this service"
            elif target in out["dependencies"]:
                f["relation"] = "a service this one calls"
            else:
                f["relation"] = "an unrelated service"

    incident_time = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    c = out["container"]
    if c.get("found") and c.get("started_at"):
        try:
            started = datetime.fromisoformat(c["started_at"].replace("Z", "+00:00"))
            age = incident_time - started
            c["started_before_incident"] = str(age).split(".")[0]
            c["recently_restarted"] = timedelta(0) <= age <= timedelta(minutes=RECENT_RESTART_MIN)
        except ValueError:
            pass
    out["text"] = to_text(out)
    return out


def to_text(ev: dict) -> str:
    svc = ev["service"]
    lines = [f"Change evidence for service '{svc}' (collected {ev['collected_at']} UTC)."]

    if ev["flag_error"]:
        lines.append(f"- Feature flags: could not be read ({ev['flag_error']}).")
    elif not ev["active_flags"]:
        lines.append("- Feature flags: every flag is currently off (no fault flag is set).")
    else:
        lines.append("- Feature flags currently NOT off:")
        for f in ev["active_flags"]:
            rel = f.get("relation", "unknown relationship")
            lines.append(f"    {f['flag']} = {f['variant']} (value {f['value']}; "
                         f"affects '{f['affects']}', which is {rel})")
    if ev.get("dependencies") is not None:
        deps = ", ".join(ev["dependencies"]) or "none"
        lines.append(f"- Services that '{svc}' calls: {deps}")

    c = ev["container"]
    if not c.get("found"):
        lines.append(f"- Container: no container named '{svc}' was found.")
    else:
        lines.append(f"- Container status: {c['status']}, image {c['image']}")
        age = c.get("started_before_incident")
        if age is not None:
            when = ("restarted shortly before the incident" if c.get("recently_restarted")
                    else "running well before the incident")
            lines.append(f"- Container start: {c['started_at']} ({age} before the incident) - {when}")
        lines.append(f"- Restart count since creation: {c['restart_count']}"
                     + (" (container has crashed and been restarted)" if c["restart_count"] else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    svc = sys.argv[1] if len(sys.argv) > 1 else "payment"
    when = sys.argv[2] if len(sys.argv) > 2 else datetime.now(timezone.utc).isoformat()
    print(collect(svc, when)["text"])
