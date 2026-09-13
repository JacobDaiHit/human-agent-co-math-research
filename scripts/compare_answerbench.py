"""Offline paired comparison. Never dispatch inference or choose answers using gold."""

import argparse
import copy
import json
import math
import random
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def paired_comparison(baseline_dirs, agent_dirs, *, bootstrap_seed=20260913, bootstrap_samples=5000):
    if not baseline_dirs or len(baseline_dirs) != len(agent_dirs):
        raise ValueError("Supply one matched baseline and agent batch per repeat")
    if type(bootstrap_samples) is not int or bootstrap_samples < 20:
        raise ValueError("At least 20 bootstrap samples are required")
    observations, clusters, signatures, seen = [], {}, [], set()
    totals = {arm: {"requests": 0, "reported_total_tokens": 0, "reported_input_tokens": 0,
                   "reported_output_tokens": 0, "reported_cache_hit_tokens": 0,
                   "elapsed_seconds": 0, "usage_missing_cases": 0} for arm in ("baseline", "agent")}
    for repeat, (left, right) in enumerate(zip(baseline_dirs, agent_dirs, strict=True)):
        paths = [Path(left).resolve(), Path(right).resolve()]
        if any(p in seen for p in paths) or paths[0] == paths[1]:
            raise ValueError("Batch reuse would double-count observations")
        seen.update(paths)
        plans = [read(p / "plan.json")["configuration"] for p in paths]
        if any(read(p / "report.json").get("source_unchanged") is not True for p in paths):
            raise ValueError("Both batches must have terminal frozen-source verification")
        # Explicit scores.json only; callers must not silently choose a favorable revision.
        scores = [read(p / "scores.json") for p in paths]
        for field in ("answer_key_sha256", "scorer_sha256", "grading_method"):
            if scores[0][field] != scores[1][field]:
                raise ValueError("Both arms must use the same frozen grader and answer key")
        for field in ("problems_sha256", "provider", "requested_model", "submission_rule"):
            if plans[0][field] != plans[1][field]:
                raise ValueError("Unmatched problem/model/submission configuration: " + field)
        if plans[0]["source"] != plans[1]["source"]:
            raise ValueError("Both arms must use the same frozen harness source")
        if sorted(plans[0]["case_ids"]) != sorted(plans[1]["case_ids"]):
            raise ValueError("Unmatched case IDs")
        ls, rs = (p["limits"] for p in plans)
        for field in ("thinking_mode", "reasoning_effort", "max_output_tokens", "request_timeout_seconds",
                      "case_timeout_seconds", "unknown_recovery", "parallel_cases", "cumulative_output_token_budget", "case_order_seed"):
            if ls[field] != rs[field]:
                raise ValueError("Unmatched comparison limit: " + field)
        if ls["evaluation_mode"] != "answer" or rs["evaluation_mode"] != "answer" or rs["solver"] != "agent":
            raise ValueError("Primary answer comparison requires answer mode and an agent right arm")
        if ls["solver"] not in {"direct", "self_refine"}:
            raise ValueError("Left arm must be direct or self_refine")
        if ls["solver"] == "self_refine" and (ls["request_budget"] != rs["request_budget"] or ls["cumulative_output_token_budget"] is None):
            raise ValueError("Self-refine comparison requires equal request and explicit output budgets")
        normalized = copy.deepcopy(plans)
        for plan in normalized:
            plan["case_ids"] = sorted(plan["case_ids"])
            plan["limits"].pop("case_order_seed", None)
        signature = {"plans": normalized, "grader": scores[0]["scorer_sha256"], "key": scores[0]["answer_key_sha256"]}
        if signatures and signature != signatures[0]:
            raise ValueError("All repeats must freeze the same arm configurations, source, grader and key")
        signatures.append(signature)
        expected = plans[0]["case_ids"]
        if not expected or len(set(expected)) != len(expected):
            raise ValueError("Expected a nonempty unique case set")
        maps = [{r["problem_id"]: r for r in s["cases"]} for s in scores]
        if any(len(s["cases"]) != len(expected) or set(m) != set(expected) for s, m in zip(scores, maps, strict=True)):
            raise ValueError("Missing, extra or duplicate scored cases; keep failures in the denominator")
        for case in expected:
            pair = [m[case] for m in maps]
            reports = [read(p / case / "report.json") for p in paths]
            if any(r.get("unattended_eligible") is not True for r in reports):
                raise ValueError("Intervened/unverified runs cannot enter an unattended comparison")
            success = [int(r["grade"] == "correct") for r in pair]
            unknown = [int(r["grade"] == "ungraded") for r in pair]
            delta = success[1] - success[0]
            observations.append({"repeat": repeat + 1, "case_id": case, "baseline_correct": success[0],
                "agent_correct": success[1], "baseline_ungraded": unknown[0], "agent_ungraded": unknown[1]})
            clusters.setdefault(case, []).append(delta)
            for arm, report in zip(("baseline", "agent"), reports, strict=True):
                usage = report.get("usage_summary") or {}
                totals[arm]["requests"] += report.get("requests") or 0
                totals[arm]["reported_total_tokens"] += usage.get("reported_tokens", {}).get("total_tokens", 0)
                for target, key in (("reported_input_tokens", "prompt_tokens"),
                                    ("reported_output_tokens", "completion_tokens"),
                                    ("reported_cache_hit_tokens", "prompt_cache_hit_tokens")):
                    totals[arm][target] += usage.get("reported_tokens", {}).get(key, 0)
                totals[arm]["elapsed_seconds"] += report.get("elapsed_seconds", 0)
                totals[arm]["usage_missing_cases"] += int(usage.get("all_usage_known") is not True)
    n = len(observations)
    wins = sum(r["agent_correct"] > r["baseline_correct"] for r in observations)
    losses = sum(r["agent_correct"] < r["baseline_correct"] for r in observations)
    means = [sum(v) / len(v) for v in clusters.values()]
    rng = random.Random(bootstrap_seed)
    boot = sorted(sum(rng.choices(means, k=len(means))) / len(means) for _ in range(bootstrap_samples))
    a = sum(r["agent_correct"] for r in observations)
    b = sum(r["baseline_correct"] for r in observations)
    au = sum(r["agent_ungraded"] for r in observations)
    bu = sum(r["baseline_ungraded"] for r in observations)
    discordant = wins + losses
    # Repeats within a problem are correlated: exact sign test only for one repeat.
    pvalue = min(1, 2 * sum(math.comb(discordant, k) for k in range(min(wins, losses) + 1)) / 2**discordant) if discordant and len(baseline_dirs) == 1 else None
    return {"comparison": "single_call_reference" if ls["solver"] == "direct" else "matched_output_budget",
        "unique_problems": len(clusters), "repeats": len(baseline_dirs), "observations": n,
        "baseline_accuracy": b/n, "agent_accuracy": a/n, "accuracy_delta": (a-b)/n,
        "agent_wins": wins, "agent_losses": losses, "ties": n-wins-losses,
        "ungraded": {"baseline": bu, "agent": au},
        "delta_bounds_if_ungraded_resolved": [(a-b-bu)/n, (a+au-b)/n],
        "problem_cluster_bootstrap_95_interval": [boot[int(.025*bootstrap_samples)], boot[int(.975*bootstrap_samples)-1]],
        "bootstrap_seed": bootstrap_seed, "bootstrap_samples": bootstrap_samples,
        "single_repeat_exact_mcnemar_p": pvalue, "costs": totals,
        "agent_calculation_tools": {"builtin": rs["builtin_calculator"], "sandbox": rs["code_sandbox"]},
        "notice": "Equal output ceilings are not equal actual total tokens or money. Public-data contamination is unknown. "
                  "Ungraded is counted as not verified correct; resolve it blind with the same policy in both arms. "
                  "Small/development samples do not establish general improvement. Internal review is not independent proof grading.",
        "pairs": observations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="append", required=True, type=Path)
    parser.add_argument("--agent", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = paired_comparison(args.baseline, args.agent)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "pairs"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
