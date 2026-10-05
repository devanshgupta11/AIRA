"""Toggle OpenTelemetry Demo feature flags through the flagd-ui API, then verify with flagd.

Discovered from the demo source (tag 3.1.0):
  - flagd-ui (routed by Envoy at /feature) exposes GET /api/read -> {"flags": {...}}
    and POST /api/write with {"data": <whole flag file>}, which replaces demo.flagd.json.
  - When the UI sets a variant it changes targeting.if[1] if the flag has an
    if/then/else targeting rule (productCatalogFailure), otherwise defaultVariant.
  - flagd watches that file; its OFREP API (container port 8016) evaluates flags.

Usage:
  python flagctl.py list
  python flagctl.py get <flag>
  python flagctl.py set <flag> <variant>     e.g. set cartFailure 100%
  python flagctl.py reset-all                 every flag back to "off"
"""

import json
import subprocess
import sys
import time
import urllib.request

FLAGD_UI = "http://localhost:8080/feature/api"
SCHEMA = "https://flagd.dev/schema/v0/flags.json"
# Evaluation context that makes the targeting rule of productCatalogFailure match.
CONTEXT = {"productCatalogFailure": {"product_id": "OLJCESPC7Z"}}


def http_json(url, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def read_flags():
    return http_json(f"{FLAGD_UI}/read")["flags"]


def configured_variant(flag):
    rule = flag.get("targeting", {}).get("if")
    if isinstance(rule, list) and len(rule) == 3:
        return rule[1]
    return flag["defaultVariant"]


def apply_variant(flag, variant):
    rule = flag.get("targeting", {}).get("if")
    if isinstance(rule, list) and len(rule) == 3:
        rule[1] = variant
    else:
        flag["defaultVariant"] = variant


def write_flags(flags):
    http_json(f"{FLAGD_UI}/write", {"data": {"$schema": SCHEMA, "flags": flags}})


def ofrep_port():
    out = subprocess.run(["docker", "port", "flagd", "8016"], capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[0].rsplit(":", 1)[1]


def evaluate(name):
    """Ask flagd itself what the flag evaluates to (proves the change took effect)."""
    port = ofrep_port()
    ctx = CONTEXT.get(name, {})
    return http_json(f"http://localhost:{port}/ofrep/v1/evaluate/flags/{name}", {"context": ctx})


def wait_for(name, variant, timeout=20):
    deadline = time.time() + timeout
    result = {}
    while time.time() < deadline:
        result = evaluate(name)
        if result.get("variant") == variant:
            return True, result
        time.sleep(1)
    return False, result


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]

    if cmd == "list":
        for name, flag in sorted(read_flags().items()):
            print(f"{name:30} {configured_variant(flag):8} variants={list(flag['variants'])}")
        return 0

    if cmd == "get" and len(argv) == 3:
        r = evaluate(argv[2])
        print(f"{argv[2]}: flagd evaluates variant={r.get('variant')} value={r.get('value')}")
        return 0

    if cmd == "set" and len(argv) == 4:
        name, variant = argv[2], argv[3]
        flags = read_flags()
        if name not in flags:
            print(f"ERROR: unknown flag '{name}'. Known: {', '.join(sorted(flags))}")
            return 1
        if variant not in flags[name]["variants"]:
            print(f"ERROR: '{variant}' is not a variant of {name}: {list(flags[name]['variants'])}")
            return 1
        before = configured_variant(flags[name])
        apply_variant(flags[name], variant)
        write_flags(flags)
        print(f"flagd-ui: {name} {before} -> {variant}")
        ok, r = wait_for(name, variant)
        if ok:
            print(f"VERIFIED: flagd now evaluates {name} = {r.get('variant')} (value {r.get('value')})")
            return 0
        print(f"ERROR: flagd still evaluates {name} = {r.get('variant')} after 20s")
        return 1

    if cmd == "reset-all":
        flags = read_flags()
        changed = [n for n, f in flags.items() if configured_variant(f) != "off"]
        for n in changed:
            apply_variant(flags[n], "off")
        if changed:
            write_flags(flags)
        print(f"reset to off: {', '.join(changed) if changed else '(nothing was on)'}")
        failed = []
        for n in changed:
            ok, r = wait_for(n, "off")
            if not ok:
                failed.append(n)
        if failed:
            print(f"ERROR: flagd still has non-off values for: {', '.join(failed)}")
            return 1
        print("VERIFIED: flagd evaluates every changed flag as off")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
