"""Regression suite for every bug found while building the multi-agent layer.

Each check corresponds to a real failure observed against the running system, so a later change
cannot quietly reintroduce one. Static checks run instantly; the ones marked [llm] call the local
model and take a few seconds each.

Usage:  python regression_check.py            all checks
        python regression_check.py --static   skip the model calls
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import change_tool, db  # noqa: E402
from app.agents import base, change_agent, log_agent, metrics_agent  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n          {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------- static checks

def static_checks() -> None:
    print("\nStatic checks")

    # BUG: triage reported the alert's trigger level as the service's baseline. The rewrite only
    # matched "(threshold X)" as a whole parenthesis and missed the CPU rule's wording.
    formats = {
        "grafana container used 113% CPU over the last minute (100% = one full core; threshold 100%).": True,
        "payment: 42.86% of server requests failed over the last 3 minutes (threshold 5%).": True,
        "payment / Charge: 70% of calls failed over the last 3 minutes (threshold 50%).": True,
        "No container metrics for over 45 seconds.": False,
    }
    bad = [d for d, should in formats.items() if (base.clarify_threshold(d) != d) != should]
    check("threshold wording clarified in every alert format", not bad, f"not handled: {bad}")

    # BUG: the metrics agent copied "4.3%" out of an example in its own prompt instead of the data.
    rules = base.NUMBER_RULES + metrics_agent.RULES + log_agent.RULES + change_agent.RULES
    decimals = re.findall(r"\d+\.\d+", rules)
    check("no realistic example numbers left in prompt rules", not decimals, f"found: {decimals}")

    # BUG: a routine stack start 13 minutes earlier was presented as a recent restart, and the agent
    # blamed it instead of the feature flag that was set.
    check("recent-restart window tightened to <= 10 min",
          change_tool.RECENT_RESTART_MIN <= 10, f"is {change_tool.RECENT_RESTART_MIN}")
    check("restart wording no longer shouts in capitals",
          "RESTARTED SHORTLY BEFORE" not in Path(change_tool.__file__).read_text(encoding="utf-8"))

    # BUG: the change agent blamed a payment flag for a cart alert. The call graph is now read from
    # each container's own environment rather than guessed.
    deps_checkout = change_tool.dependencies("checkout")
    deps_cart = change_tool.dependencies("cart")
    check("dependency graph read from containers: checkout calls payment",
          "payment" in deps_checkout, f"checkout -> {deps_checkout}")
    check("dependency graph read from containers: cart does NOT call payment",
          "payment" not in deps_cart, f"cart -> {deps_cart}")

    # BUG: the quote checker was a no-op risk; it must catch a fabricated line, accept several real
    # lines cited together, and ignore "none".
    # check_agent_numbers parses sys.argv at import, so shield it and restore ours afterwards.
    saved_argv = sys.argv
    sys.argv = ["x"]
    from check_agent_numbers import check_quotes  # noqa: E402
    sys.argv = saved_argv
    from app.agents.log_agent import LogFinding, LogResult  # noqa: E402

    src = "- Container status: running, image ghcr.io/demo:3.1.0-payment\n- Feature flags: every flag is off."

    def mk(q):
        return LogResult(logs_available=True, confidence="high",
                         findings=[LogFinding(statement="observed something", quoted_line=q,
                                              related_to_alert=False)])

    check("quote checker catches a fabricated log line",
          bool(check_quotes(mk("FATAL: database connection refused"), src)))
    check("quote checker accepts two real lines cited together",
          not check_quotes(mk("Container status: running, image ghcr.io/demo:3.1.0-payment\n"
                              "Feature flags: every flag is off."), src))
    check("quote checker ignores 'none'", not check_quotes(mk("none"), src))

    # BUG: incidents stayed "firing" forever when resolved webhooks were lost during downtime.
    from app import main  # noqa: E402
    check("startup reconciliation against Alertmanager exists", hasattr(main, "reconcile_firing"))

    # Storage added for the multi-agent phase, including the round column a replanning phase needs.
    cols = {r[1] for r in db.connect().execute("pragma table_info(agent_findings)")}
    needed = {"incident_id", "agent", "seq", "round", "status", "skip_reason", "output",
              "evidence_text", "runtime_s", "attempts", "error"}
    check("agent_findings schema complete", needed <= cols, f"missing: {needed - cols}")


# ---------------------------------------------------------------- live model checks

def llm_checks() -> None:
    print("\nLive model checks (each calls the local model)")

    inc = next((db.get(i["id"]) for i in db.list_incidents(60)
                if i["service"] == "checkout" and i.get("evidence")), None)
    if inc:
        # BUG: the metrics agent said latency "increased substantially" when the number had fallen.
        ev = inc["evidence"]
        r, _, _ = metrics_agent.run(inc, evidence=ev)
        text = " ".join(f.statement for f in r.findings).lower()
        m = re.search(r"p95 latency \(2m\): ([\d.]+) ms \(baseline ([\d.]+) ms\)", ev.get("text", ""))
        ok = True
        if m and "latency" in text:
            fell = float(m.group(1)) < float(m.group(2))
            said_risen = "risen" in text or "increased" in text
            said_fallen = "fallen" in text or "decreased" in text or "dropped" in text
            if said_risen or said_fallen:
                ok = (fell and said_fallen) or (not fell and said_risen)
        check("metrics agent states latency direction correctly", ok, text[:150])

    # BUG: the change agent answered "no changes detected" while a flag was set, and separately
    # omitted the flag when a container restart was also present.
    flags, _ = change_tool.active_flags()
    if flags:
        svc = flags[0]["affects"]
        test_inc = {"alertname": "AiraHighErrorRate", "service": svc, "severity": "critical",
                    "starts_at": db.now_iso(), "summary": f"test on {svc}", "description": "",
                    "labels": {"service_name": svc}}
        ev = change_agent.collect(svc, test_inc["starts_at"])
        r, attempts, _ = change_agent.run(test_inc, evidence=ev)
        mentioned = flags[0]["flag"].lower() in " ".join(
            f"{f.statement} {f.detail}" for f in r.findings).lower()
        check(f"change agent reports the set flag ({flags[0]['flag']})",
              r.changes_found and mentioned, f"changes_found={r.changes_found} mentioned={mentioned}")

        # BUG: with paymentFailure set, a cart alert was blamed on it even though cart never calls
        # payment. An unrelated service must not inherit another service's flag as its cause.
        unrelated = next((s for s in ("cart", "ad", "currency")
                          if s != svc and svc not in change_tool.dependencies(s)), None)
        if unrelated:
            u_inc = {**test_inc, "service": unrelated, "labels": {"service_name": unrelated},
                     "summary": f"test on {unrelated}"}
            u_ev = change_agent.collect(unrelated, u_inc["starts_at"])
            ur, _, _ = change_agent.run(u_inc, evidence=u_ev)
            blamed = any(f.likely_cause for f in ur.findings)
            check(f"change agent does NOT blame {flags[0]['flag']} for an unrelated {unrelated} alert",
                  not blamed, f"findings: {[f.statement for f in ur.findings]}")
    else:
        print("  SKIP  change agent flag test (no flag is currently set; inject a fault to test it)")

    # BUG: the log agent must say so honestly when a service produced no logs, not invent one.
    #       checkout and product-catalog write nothing to either source.
    no_log_inc = {"alertname": "AiraHighErrorRate", "service": "checkout", "severity": "critical",
                  "starts_at": db.now_iso(), "summary": "test", "description": "",
                  "labels": {"service_name": "checkout"}}
    ev = log_agent.collect("checkout", no_log_inc["starts_at"])
    if ev["counts"]["total"] == 0:
        r, _, _ = log_agent.run(no_log_inc, evidence=ev)
        quotes = [f.quoted_line.lower() for f in r.findings]
        check("log agent reports absence honestly when there are no logs",
              not r.logs_available and all(q in ("none", "n/a", "-") for q in quotes),
              f"logs_available={r.logs_available} quotes={quotes}")
    else:
        print("  SKIP  log agent absence test (checkout produced logs this time)")


def main() -> int:
    static_checks()
    if "--static" not in sys.argv:
        llm_checks()
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for _, name, detail in failed:
        print(f"  FAILED: {name} - {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
