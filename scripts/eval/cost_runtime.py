"""Measured cost and running time for AIRA, per stage and per investigation.

Everything here is read from events the callbacks recorded during real investigations - token
counts and durations reported by the model itself, not estimates from character counts. No new
model runs are needed, which is the point: the system has been instrumented since Phase C, so the
evidence for this already exists.

Energy uses GPU power sampled with nvidia-smi while the evaluation was driving the model, rather
than the card's rated TDP. The two differ by a lot on a laptop part.

The commercial-API comparison is the honest version of "this project is free". It is not free; it
costs electricity and it cost a GPU. What it does not cost is per-token fees, and that is worth
quantifying rather than asserting.

    backend\\.venv\\Scripts\\python.exe scripts\\eval\\cost_runtime.py
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402

# Sampled with nvidia-smi at 1 Hz over 70 s while the replay evaluation was running, on the
# RTX 4060 Laptop GPU this project runs on. Re-measure with --gpu-watts if the hardware changes.
GPU_WATTS_INFERRING = 55.2
GPU_WATTS_IDLE = 13.3

# Indicative list prices per million tokens, for the comparison only. These move, and they are not
# measured by this project - check them before quoting the figure anywhere that matters.
API_PRICES = {
    "a small hosted model":  {"in": 0.15, "out": 0.60},
    "a mid-tier hosted model": {"in": 3.00, "out": 15.00},
    "a frontier hosted model": {"in": 15.00, "out": 75.00},
}

ELECTRICITY_PER_KWH = 8.0   # INR, typical Indian domestic slab rate
CURRENCY = "INR"
USD_TO_INR = 88.0


def gather(limit: int) -> tuple[dict, dict]:
    """Per-agent model calls, and per-incident totals, from recorded events."""
    per_agent = defaultdict(lambda: {"calls": 0, "ms": [], "pin": [], "pout": []})
    per_incident = {}

    for row in db.list_incidents(limit):
        iid = row["id"]
        tot_ms = tot_in = tot_out = calls = 0
        tools = 0
        for e in db.get_events(iid):
            d = e.get("detail") or {}
            if e["kind"] == "tool_call_completed":
                tools += 1
            if e["kind"] != "model_call_completed":
                continue
            tok = d.get("tokens") or {}
            if not tok:
                continue
            agent = e.get("agent") or "unknown"
            a = per_agent[agent]
            a["calls"] += 1
            if e.get("duration_ms"):
                a["ms"].append(e["duration_ms"])
                tot_ms += e["duration_ms"]
            a["pin"].append(tok.get("prompt_tokens", 0))
            a["pout"].append(tok.get("completion_tokens", 0))
            tot_in += tok.get("prompt_tokens", 0)
            tot_out += tok.get("completion_tokens", 0)
            calls += 1
        if calls:
            per_incident[iid] = {"model_ms": tot_ms, "in": tot_in, "out": tot_out,
                                 "calls": calls, "tools": tools,
                                 "alertname": row["alertname"], "service": row["service"]}
    return per_agent, per_incident


def table_per_agent(per_agent: dict) -> None:
    print("\n" + "=" * 104)
    print("PER-AGENT MODEL COST, measured from recorded callbacks")
    print("=" * 104)
    print(f"{'agent':<14}{'calls':>7}{'median s':>11}{'p95 s':>9}"
          f"{'prompt tok':>13}{'output tok':>12}{'total tok':>12}{'share':>9}")
    print("-" * 104)
    grand = sum(sum(a["pin"]) + sum(a["pout"]) for a in per_agent.values())
    order = sorted(per_agent.items(), key=lambda kv: -(sum(kv[1]["pin"]) + sum(kv[1]["pout"])))
    for name, a in order:
        tot = sum(a["pin"]) + sum(a["pout"])
        ms = sorted(a["ms"]) or [0]
        p95 = ms[min(len(ms) - 1, int(0.95 * len(ms)))]
        print(f"{name:<14}{a['calls']:>7}{st.median(ms) / 1000:>11.1f}{p95 / 1000:>9.1f}"
              f"{st.mean(a['pin']):>13.0f}{st.mean(a['pout']):>12.0f}"
              f"{tot:>12,}{tot / grand:>8.0%}")
    print("-" * 104)
    print(f"{'TOTAL':<14}{sum(a['calls'] for a in per_agent.values()):>7}"
          f"{'':>11}{'':>9}{'':>13}{'':>12}{grand:>12,}")


def table_per_incident(per_incident: dict) -> None:
    if not per_incident:
        return
    ins = [v["in"] for v in per_incident.values()]
    outs = [v["out"] for v in per_incident.values()]
    mss = [v["model_ms"] / 1000 for v in per_incident.values()]
    calls = [v["calls"] for v in per_incident.values()]
    tools = [v["tools"] for v in per_incident.values()]

    def band(xs):
        xs = sorted(xs)
        return (f"{st.median(xs):,.0f}", f"{xs[0]:,.0f}", f"{xs[-1]:,.0f}",
                f"{st.mean(xs):,.0f}")

    print("\n" + "=" * 104)
    print(f"PER-INVESTIGATION, across {len(per_incident)} real investigations")
    print("=" * 104)
    print(f"{'measure':<28}{'median':>14}{'mean':>14}{'min':>14}{'max':>14}")
    print("-" * 104)
    for label, xs in (("model calls", calls), ("MCP tool calls", tools),
                      ("prompt tokens", ins), ("output tokens", outs),
                      ("total tokens", [i + o for i, o in zip(ins, outs)]),
                      ("model time (s)", mss)):
        xs_s = sorted(xs)
        print(f"{label:<28}{st.median(xs_s):>14,.1f}{st.mean(xs_s):>14,.1f}"
              f"{xs_s[0]:>14,.1f}{xs_s[-1]:>14,.1f}")
    return


def table_cost(per_incident: dict, watts: float) -> None:
    ins = [v["in"] for v in per_incident.values()]
    outs = [v["out"] for v in per_incident.values()]
    mss = [v["model_ms"] / 1000 for v in per_incident.values()]
    m_in, m_out, m_s = st.mean(ins), st.mean(outs), st.mean(mss)

    kwh = (watts / 1000) * (m_s / 3600)
    energy_cost = kwh * ELECTRICITY_PER_KWH

    print("\n" + "=" * 104)
    print("COST OF ONE INVESTIGATION")
    print("=" * 104)
    print(f"  mean tokens        {m_in:,.0f} in + {m_out:,.0f} out = {m_in + m_out:,.0f}")
    print(f"  mean GPU time      {m_s:.1f} s at {watts:.1f} W measured  ->  {kwh * 1000:.2f} Wh")
    print(f"  electricity        {CURRENCY} {energy_cost:.4f}  (at {CURRENCY} {ELECTRICITY_PER_KWH}/kWh)")
    print()
    print("  the same tokens, had they gone to a hosted API instead:")
    print(f"    {'model class':<28}{'per incident':>18}{'per 1,000 incidents':>22}")
    print("    " + "-" * 66)
    for name, p in API_PRICES.items():
        usd = (m_in / 1e6) * p["in"] + (m_out / 1e6) * p["out"]
        inr = usd * USD_TO_INR
        print(f"    {name:<28}{'$' + format(usd, '.4f'):>18}"
              f"{'$' + format(usd * 1000, '.2f') + f'  ({CURRENCY} {inr * 1000:,.0f})':>22}")
    print()
    print("  Read this carefully rather than as a win: the local model is cheaper per token and")
    print("  costs nothing per call, but it needed a GPU to exist, it is slower than a hosted")
    print("  frontier model, and the Critic rejects more of its own answers than a stronger model")
    print("  would. The saving is real; so is what was given up for it.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--gpu-watts", type=float, default=GPU_WATTS_INFERRING)
    ap.add_argument("--json", action="store_true", help="also write a machine-readable copy")
    args = ap.parse_args()

    per_agent, per_incident = gather(args.limit)
    if not per_incident:
        print("No investigations with recorded token counts. Has the backend run since Phase C?")
        return 1

    table_per_agent(per_agent)
    table_per_incident(per_incident)
    table_cost(per_incident, args.gpu_watts)

    if args.json:
        out = ROOT / "logs" / "eval" / "cost_runtime.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "investigations": len(per_incident),
            "gpu_watts_inferring": args.gpu_watts,
            "gpu_watts_idle": GPU_WATTS_IDLE,
            "per_agent": {k: {"calls": v["calls"],
                              "median_s": round(st.median(v["ms"]) / 1000, 2) if v["ms"] else None,
                              "mean_prompt_tokens": round(st.mean(v["pin"]), 1),
                              "mean_output_tokens": round(st.mean(v["pout"]), 1)}
                          for k, v in per_agent.items()},
            "per_incident": per_incident,
        }, indent=1), encoding="utf-8")
        print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
