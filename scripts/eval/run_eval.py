"""Run the evaluation arms against frozen ground truth and score them.

Design, and its limits, stated up front because an evaluation that hides its method is worthless.

REPLAY. Every arm is given the *same stored incident* - the same alert, the same evidence, the
same investigator findings - and asked for a cause and an action. Nothing is re-collected, so the
only thing that differs between arms is the reasoning. That is a stronger comparison than running
each arm against its own live fault, where arms would silently receive different evidence, and it
is the only way to repeat a run on a stochastic model without the system under it moving.

WHAT REPLAY CANNOT MEASURE. Detection time belongs to Prometheus and Alertmanager, not to any arm,
and is identical for all of them. Recovery time requires actually executing an action against the
running system, which only Full AIRA does and which needs human approval. Those two are measured
by separate live runs and are reported as such - never mixed into the replay table.

REPEATS. Each scenario is replayed against several *different* stored incidents of that fault
type, not the same one several times. That samples two sources of variance at once: the model's
own randomness, and the fact that two instances of one fault never produce identical evidence.
Within a repeat every arm sees the same incident, so the comparison stays paired.

    backend\\.venv\\Scripts\\python.exe scripts\\eval\\run_eval.py --repeats 3
    backend\\.venv\\Scripts\\python.exe scripts\\eval\\run_eval.py --arms baseline-rules --repeats 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import BaseModel, Field  # noqa: E402

from app import db  # noqa: E402
from app.agents import base, critic_agent, rca_agent, remediation_agent  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arms import ArmResult, baseline_rules, _evidence_bundle, SINGLE_LLM_PROMPT  # noqa: E402

BACKEND = "http://localhost:8000"

GT_PATH = Path(__file__).resolve().parent / "ground_truth.json"
OUT_DIR = ROOT / "logs" / "eval"

ARMS = ["baseline-rules", "baseline-single-llm", "aira-no-critic", "aira-no-memory", "aira-full"]

ARM_LABEL = {
    "baseline-rules":      "Baseline 1: rule-based, no LLM",
    "baseline-single-llm": "Baseline 2: one LLM, all evidence",
    "aira-no-critic":      "AIRA without the Critic",
    "aira-no-memory":      "AIRA without memory",
    "aira-full":           "Full AIRA",
}


def recall_via_api(incident_id: int) -> list[dict]:
    """Past cases for this incident, from the process that owns the store."""
    import urllib.request
    with urllib.request.urlopen(f"{BACKEND}/memory/recall/{incident_id}", timeout=30) as fh:
        return json.load(fh).get("cases", [])


# ---------------------------------------------------------------- choosing what to replay

def complete(fs: list[dict]) -> bool:
    """An incident worth replaying: triage plus at least one investigator actually ran."""
    done = {f["agent"] for f in fs if f["status"] == "done"}
    return "triage" in done and bool(done & {"metrics", "log", "change"})


def _freshness_s(inc: dict) -> float:
    """Seconds between the incident starting and its evidence being collected.

    The one quality measure of a replay subject that can be computed without looking at the
    answer, and it matters. Incident #210 was investigated after its fault flag had already been
    switched off, so its Change evidence reads "every flag is currently off". The cause is simply
    not recoverable from that input by any arm, and scoring five arms against it measures the
    collection delay rather than the reasoning. Ranking by freshness is not cherry-picking:
    nothing here consults the ground truth, the stored conclusion, or any arm's output.
    """
    from datetime import datetime

    a, b = inc.get("starts_at"), inc.get("evidence_collected_at")
    if not a or not b:
        return float("inf")
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except Exception:  # noqa: BLE001
        return float("inf")


def subjects(scenarios: dict, repeats: int) -> tuple[dict[str, list[int]], list[dict]]:
    """Pick distinct stored incidents per scenario: freshest evidence first, newest as tiebreak.

    Returns the picks and a note per pick, so the report can state which incidents were replayed
    and how stale each one's evidence was.
    """
    out: dict[str, list[int]] = {}
    notes: list[dict] = []
    rows = db.list_incidents(1000)
    for name, sc in scenarios.items():
        want = {(a, s) for a, s in sc["expected_alerts"]}
        cands = []
        for r in rows:
            if (r["alertname"], r["service"]) not in want:
                continue
            if not complete(db.get_findings(r["id"])):
                continue
            cands.append((_freshness_s(db.get(r["id"])), -r["id"], r["id"]))
        cands.sort()
        out[name] = [c[2] for c in cands[:repeats]]
        for gap, _, iid in cands[:repeats]:
            notes.append({"scenario": name, "incident_id": iid,
                          "evidence_lag_s": None if gap == float("inf") else round(gap, 1)})
    return out, notes


# ---------------------------------------------------------------- the arms

class SingleLLMAnswer(BaseModel):
    root_cause: str = Field(description="one sentence naming what caused this")
    service: str = Field(default="", description="the service the cause is in")
    action: str = Field(default="none")
    target_service: str = Field(default="")
    flag: str | None = None
    confidence: str = Field(default="low")


SINGLE_HINT = base.schema_hint({
    "root_cause": "string", "service": "string",
    "action": "reset_feature_flag | restart_container | none",
    "target_service": "string", "flag": "string or null",
    "confidence": "low | medium | high",
})


def run_single_llm(inc: dict, findings: list[dict]) -> ArmResult:
    """One model call, all the evidence at once, no orchestration and no reviewer.

    Uses the same model, temperature and framework as every AIRA agent, so what is being compared
    is the architecture rather than the inference stack.
    """
    t0 = time.perf_counter()
    r = ArmResult(arm="baseline-single-llm", incident_id=inc["id"], llm_calls=1)
    agent = base.make_agent(
        role="Site reliability engineer",
        goal="Identify the root cause of an incident and the single action that would fix it",
        backstory="You are handed every piece of evidence at once and must answer in one pass.",
    )
    prompt = SINGLE_LLM_PROMPT.format(evidence=_evidence_bundle(inc, findings))
    try:
        result, attempts, secs = base.run_agent(
            agent, prompt, SINGLE_HINT, SingleLLMAnswer, prompt)
        r.cause = result.root_cause
        r.action = None if result.action == "none" else result.action
        r.target = result.target_service or result.service
        r.params = {"flag": result.flag} if result.flag else {}
        r.confidence = result.confidence
        r.hypotheses = [result.root_cause]
        r.llm_calls = attempts
        r.runtime_s = secs
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"[:300]
        r.runtime_s = time.perf_counter() - t0
    return r


async def run_aira(inc: dict, findings: list[dict], *, use_critic: bool,
                   use_memory: bool, arm: str) -> ArmResult:
    """The AIRA reasoning path, with the two features under test switchable.

    The investigators are not re-run: their stored findings are the input, exactly as for the
    baselines. What varies is whether the RCA gets retrieved cases, and whether a Critic reviews
    the answer and can send it back for another round.
    """
    t0 = time.perf_counter()
    r = ArmResult(arm=arm, incident_id=inc["id"])
    calls = 0
    try:
        prior = []
        if use_memory:
            # Over HTTP, not by importing memory: Qdrant runs embedded and the backend holds its
            # lock. This is the same route the MCP server and the RCA agent must take.
            try:
                prior = await asyncio.to_thread(recall_via_api, inc["id"])
            except Exception as e:  # noqa: BLE001 - memory is never a dependency
                r.detail["memory_error"] = f"{type(e).__name__}: {e}"[:200]
        r.detail["prior_cases"] = [{"incident_id": c["incident_id"], "score": c["score"]}
                                   for c in prior]

        rca, attempts, _ = await rca_agent.run(inc, findings, prior)
        calls += attempts
        rca_d = rca.model_dump()
        r.detail["rounds"] = 1

        critic_d = None
        if use_critic:
            critic, cattempts, _ = await asyncio.to_thread(
                critic_agent.run, inc, findings, rca_d)
            calls += cattempts
            critic_d = critic.model_dump()
            r.detail["critic_verdict"] = critic.verdict
            # One replan, matching the live pipeline's bound. The investigators cannot actually be
            # re-run in replay - there is no live system to re-query - so this re-asks the RCA
            # with the Critic's objection, which is the part of replanning that replay can honour.
            if critic.verdict == "insufficient":
                rca2, a2, _ = await rca_agent.run(inc, findings, prior)
                calls += a2
                rca_d = rca2.model_dump()
                r.detail["rounds"] = 2
                critic2, a3, _ = await asyncio.to_thread(
                    critic_agent.run, inc, findings, rca_d)
                calls += a3
                critic_d = critic2.model_dump()
                r.detail["critic_verdict"] = critic2.verdict

        hyps = rca_d.get("hypotheses") or []
        r.cause = hyps[0]["statement"] if hyps else (rca_d.get("summary") or "")
        r.hypotheses = [h["statement"] for h in hyps]
        r.confidence = rca_d.get("confidence")

        # The remediation agent validates its proposal against the safety layer, which reads the
        # system as it is NOW - and right now every fault is cleared and every flag is off. So a
        # perfectly correct "reset paymentFailure" is refused as unnecessary, the retry produces
        # the same thing, and the arm dies. That is an artifact of replaying against a healed
        # system, not a reasoning failure, and scoring it as "proposed nothing" would be false.
        # Remediation is therefore marked unmeasurable here and reported from live runs instead.
        try:
            prop, pattempts, _ = await asyncio.to_thread(
                remediation_agent.run, inc, findings, rca_d, critic_d, None)
            calls += pattempts
        except Exception as e:  # noqa: BLE001
            r.detail["remediation_unmeasurable"] = f"{type(e).__name__}: {e}"[:200]
            r.llm_calls = calls
            r.runtime_s = time.perf_counter() - t0
            return r
        r.action = None if prop.action == "none" else prop.action
        r.target = prop.target_service or None
        # ActionProposal carries the flag as its own field; there is no `params` dict on it. The
        # orchestrator builds that dict when it hands the proposal to the safety layer, so reading
        # `prop.params` here raised AttributeError and aborted the arm after the RCA had already
        # answered - which is why the first run recorded causes but no actions for every AIRA arm.
        r.params = {"flag": prop.flag} if prop.flag else {}
    except Exception as e:  # noqa: BLE001
        r.error = f"{type(e).__name__}: {e}"[:300]
    r.llm_calls = calls
    r.runtime_s = time.perf_counter() - t0
    return r


# ---------------------------------------------------------------- scoring

def _norm(text: str) -> str:
    """Lowercase, and strip the separators that distinguish nothing.

    "product-catalog", "product catalog" and the camelCase "productCatalog" inside a flag name are
    the same identifier. Comparing them literally scored a substantively correct answer - "the
    feature flag productCatalogFailure being on is causing errors" - as wrong, because the string
    "product-catalog" did not appear in it. That was a defect in the scoring, not a judgement
    about the arm, and it was corrected before any result was reported. It is applied identically
    to every arm.
    """
    return text.lower().replace("-", "").replace("_", "").replace(" ", "")


def score(res: ArmResult, gt: dict) -> dict:
    """Apply the rules frozen in ground_truth.json. No judgement happens here."""
    cause = (res.cause or "").lower()
    norm = _norm(res.cause or "")
    mentions = all(_norm(tok) in norm for tok in gt["must_mention"])
    # A fault flag belongs to exactly one service, so naming the flag names the service. The
    # separate service test still does the work it was added for: it catches an answer that blames
    # the downstream service and never mentions the flag at all, because `mentions` fails there.
    names = _norm(gt["must_name_service"]) in norm
    phrases = gt.get("requires_phrase_any")
    phrased = (any(p in cause for p in phrases) if phrases else True)
    cause_correct = bool(mentions and names and phrased)

    want_action, want_target = gt["correct_action"], gt["correct_target"]
    action_correct = bool(res.action == want_action
                          and (res.target or "").lower() == want_target.lower())
    # A flag action naming the wrong flag is not the right action, however right the verb is.
    if action_correct and gt.get("correct_params", {}).get("flag"):
        action_correct = ((res.params or {}).get("flag") or "").lower() \
                         == gt["correct_params"]["flag"].lower()

    proposed_wrong = bool(res.action and not action_correct)
    # Either marked at run time, or recognisable afterwards from the error a saved run recorded.
    # Only the safety layer's refusal counts: "from_agents contains names that did not
    # investigate" is the RCA genuinely failing and must stay scored as a failure.
    unmeasurable = ("remediation_unmeasurable" in (res.detail or {})
                    or "would be refused by AIRA" in (res.error or ""))
    return {
        "cause_correct": cause_correct,
        "action_correct": action_correct and not unmeasurable,
        "proposed_wrong_action": proposed_wrong and not unmeasurable,
        "proposed_nothing": res.action is None and not unmeasurable,
        "remediation_unmeasurable": unmeasurable,
        "errored": res.error is not None,
    }


# ---------------------------------------------------------------- running

async def one_cell(arm: str, inc: dict, findings: list[dict], scenarios: dict) -> ArmResult:
    if arm == "baseline-rules":
        return baseline_rules(inc, findings, scenarios)
    if arm == "baseline-single-llm":
        return await asyncio.to_thread(run_single_llm, inc, findings)
    return await run_aira(
        inc, findings,
        use_critic=arm != "aira-no-critic",
        use_memory=arm == "aira-full",
        arm=arm,
    )


async def main_async(args) -> int:
    gt_doc = json.loads(GT_PATH.read_text(encoding="utf-8"))
    scenarios = gt_doc["scenarios"]
    arms = args.arms or ARMS

    chosen, notes = subjects(scenarios, args.repeats)
    print(f"\nReplay subjects (freshest evidence first, {args.repeats} wanted per scenario):")
    for name, ids in chosen.items():
        lag = {n["incident_id"]: n["evidence_lag_s"] for n in notes if n["scenario"] == name}
        shown = ", ".join(f"#{i} (+{lag[i]}s)" if lag.get(i) is not None else f"#{i} (lag ?)"
                          for i in ids)
        print(f"  {name:<26} {shown or 'NONE FOUND - scenario will be skipped'}")

    rows = []
    total = sum(len(ids) for ids in chosen.values()) * len(arms)
    n = 0
    t_start = time.perf_counter()

    for sname, ids in chosen.items():
        gt = scenarios[sname]["ground_truth"]
        for rep, iid in enumerate(ids, 1):
            inc = db.get(iid)
            findings = db.get_findings(iid)
            for arm in arms:
                n += 1
                print(f"  [{n}/{total}] {sname} rep{rep} inc#{iid} :: {arm} ... ", end="", flush=True)
                res = await one_cell(arm, inc, findings, scenarios)
                sc = score(res, gt)
                rows.append({"scenario": sname, "repeat": rep, "incident_id": iid,
                             "arm": arm, **res.as_dict(), **sc})
                mark = "OK " if sc["cause_correct"] else "miss"
                print(f"{mark} {res.runtime_s:5.1f}s  {(res.cause or res.error or '')[:68]}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"replay-{stamp}.json"
    out.write_text(json.dumps({
        "generated_at": stamp,
        "method": "replay on stored evidence; detection and recovery measured separately live",
        "ground_truth": str(GT_PATH),
        "arms": arms,
        "repeats": args.repeats,
        "subjects": notes,
        "elapsed_s": round(time.perf_counter() - t_start, 1),
        "rows": rows,
    }, indent=1), encoding="utf-8")
    print(f"\nWrote {out}  ({len(rows)} rows, {time.perf_counter() - t_start:.0f}s)")
    report(rows, arms, scenarios)
    return 0


def report(rows: list[dict], arms: list[str], scenarios: dict) -> None:
    print("\n" + "=" * 96)
    print("ROOT-CAUSE CORRECTNESS, by arm and scenario (correct / runs)")
    print("=" * 96)
    names = list(scenarios)
    head = f"{'arm':<34}" + "".join(f"{s[:13]:>15}" for s in names) + f"{'overall':>11}"
    print(head)
    print("-" * len(head))
    for arm in arms:
        cells = []
        tot = ok = 0
        for s in names:
            rs = [r for r in rows if r["arm"] == arm and r["scenario"] == s]
            c = sum(1 for r in rs if r["cause_correct"])
            tot += len(rs); ok += c
            cells.append(f"{c}/{len(rs)}" if rs else "-")
        pct = f"{ok}/{tot} ({ok / tot:.0%})" if tot else "-"
        print(f"{ARM_LABEL[arm]:<34}" + "".join(f"{c:>15}" for c in cells) + f"{pct:>11}")

    print("\n" + "=" * 96)
    print("REMEDIATION AND COST")
    print("=" * 96)
    print(f"{'arm':<34}{'right action':>14}{'wrong action':>14}{'no action':>11}"
          f"{'n/a':>6}{'runtime s':>18}{'llm calls':>11}")
    print("-" * 108)
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        if not rs:
            continue
        ts = [r["runtime_s"] for r in rs]
        rt = (f"{statistics.mean(ts):.1f} +/- {statistics.stdev(ts):.1f}"
              if len(ts) > 1 else f"{ts[0]:.1f}")
        na = sum(r.get("remediation_unmeasurable", False) for r in rs)
        meas = len(rs) - na
        print(f"{ARM_LABEL[arm]:<34}"
              f"{sum(r['action_correct'] for r in rs):>8}/{meas:<5}"
              f"{sum(r['proposed_wrong_action'] for r in rs):>14}"
              f"{sum(r['proposed_nothing'] for r in rs):>11}"
              f"{na:>6}"
              f"{rt:>18}"
              f"{statistics.mean([r['llm_calls'] for r in rs]):>11.1f}")
    print("\n  'n/a' = the safety layer refused the proposal because the live system is already")
    print("  healthy. A replay cannot ask 'would you fix this?' of a system with nothing wrong,")
    print("  so remediation correctness for the AIRA arms is measured from live runs, not here.")

    errs = [r for r in rows if r["errored"]]
    if errs:
        print(f"\n{len(errs)} run(s) errored:")
        for r in errs[:10]:
            print(f"  {r['arm']} / {r['scenario']} #{r['incident_id']}: {r['error'][:90]}")


def rescore(path: Path) -> int:
    """Apply the current scoring rules to a saved run, without calling the model again."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    gt_doc = json.loads(GT_PATH.read_text(encoding="utf-8"))
    scenarios = gt_doc["scenarios"]
    rows = []
    for r in doc["rows"]:
        res = ArmResult(arm=r["arm"], incident_id=r["incident_id"], cause=r.get("cause") or "",
                        action=r.get("action"), target=r.get("target"),
                        params=r.get("params") or {}, runtime_s=r.get("runtime_s", 0.0),
                        llm_calls=r.get("llm_calls", 0), error=r.get("error"))
        rows.append({**r, **score(res, scenarios[r["scenario"]]["ground_truth"])})
    doc["rows"] = rows
    doc["rescored"] = True
    out = path.with_name(path.stem + "-rescored.json")
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"Re-scored {len(rows)} rows from {path.name} -> {out.name}")
    report(rows, doc["arms"], scenarios)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--arms", nargs="*", choices=ARMS)
    ap.add_argument("--rescore", type=Path, help="re-apply scoring to a saved results file")
    args = ap.parse_args()
    if args.rescore:
        return rescore(args.rescore)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
