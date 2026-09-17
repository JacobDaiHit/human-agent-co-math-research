"""Offline paired comparison. Never dispatch inference or choose answers using gold."""

import argparse
import copy
import json
import math
import random
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _search_diagnostics(state):
    """Summarize controller telemetry; these values are not proof or accuracy."""
    if not isinstance(state, dict):
        return None
    routes = state.get("routes")
    decisions = state.get("decisions")
    gaps = state.get("gaps")
    memory = state.get("memory")
    actions = 0
    if isinstance(decisions, list):
        actions = sum(len(item.get("actions", [])) if isinstance(item, dict) and isinstance(item.get("actions", []), list) else 0 for item in decisions)
    return {"route_count": len(routes) if isinstance(routes, list) else state.get("route_count", 0),
            "gap_count": len(gaps) if isinstance(gaps, list) else state.get("gap_count", 0),
            "memory_count": len(memory) if isinstance(memory, (list, dict)) else state.get("memory_count", 0),
            "decision_count": len(decisions) if isinstance(decisions, list) else state.get("decision_count", 0),
            "work_action_count": actions}


def paired_comparison(baseline_dirs, agent_dirs, *, bootstrap_seed=20260913, bootstrap_samples=5000,
                      agent_ablation=False):
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
        if not agent_ablation:
            for field in ("builtin_calculator", "code_sandbox"):
                if ls.get(field) != rs.get(field):
                    raise ValueError("Unmatched comparison tool: " + field)
        if ls["evaluation_mode"] != "answer" or rs["evaluation_mode"] != "answer" or rs["solver"] != "agent":
            raise ValueError("Primary answer comparison requires answer mode and an agent right arm")
        if agent_ablation:
            if ls["solver"] != "agent":
                raise ValueError("Agent ablation requires agent solvers in both arms")
            if ls["request_budget"] != rs["request_budget"] or ls["cumulative_output_token_budget"] is None:
                raise ValueError("Agent ablation requires equal request and explicit output budgets")
        elif ls["solver"] not in {"direct", "self_refine", "independent_samples"}:
            raise ValueError("Left arm must be direct, self_refine or independent_samples")
        if ls["solver"] in {"self_refine", "independent_samples"} and (ls["request_budget"] != rs["request_budget"] or ls["cumulative_output_token_budget"] is None):
            raise ValueError("Multi-call baseline comparison requires equal request and explicit output budgets")
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
            for arm, report in zip(("baseline", "agent"), reports, strict=True):
                diagnostics = _search_diagnostics(report.get("search_state"))
                if diagnostics is not None:
                    totals[arm].setdefault("search_diagnostics", []).append(diagnostics)
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
    tools_ablation = bool(agent_ablation and (ls.get("builtin_calculator") != rs.get("builtin_calculator") or
                                              ls.get("code_sandbox") != rs.get("code_sandbox")))
    return {"comparison": "agent_controller_ablation" if agent_ablation else ("single_call_reference" if ls["solver"] == "direct" else "matched_output_budget"),
        "agent_ablation": agent_ablation,
        "arm_configuration": {"baseline": {"solver": ls["solver"], "solver_controller": ls.get("solver_controller", "legacy"), "search_config": ls.get("search_config", {})},
                               "agent": {"solver": rs["solver"], "solver_controller": rs.get("solver_controller", "legacy"), "search_config": rs.get("search_config", {})}},
        "tools_ablation": tools_ablation,
        "unique_problems": len(clusters), "repeats": len(baseline_dirs), "observations": n,
        "baseline_accuracy": b/n, "agent_accuracy": a/n, "accuracy_delta": (a-b)/n,
        "agent_wins": wins, "agent_losses": losses, "ties": n-wins-losses,
        "ungraded": {"baseline": bu, "agent": au},
        "delta_bounds_if_ungraded_resolved": [(a-b-bu)/n, (a+au-b)/n],
        "problem_cluster_bootstrap_95_interval": [boot[int(.025*bootstrap_samples)], boot[int(.975*bootstrap_samples)-1]],
        "bootstrap_seed": bootstrap_seed, "bootstrap_samples": bootstrap_samples,
        "single_repeat_exact_mcnemar_p": pvalue, "costs": totals,
        "agent_calculation_tools": {"baseline": {"builtin": ls["builtin_calculator"], "sandbox": ls["code_sandbox"]},
                                     "agent": {"builtin": rs["builtin_calculator"], "sandbox": rs["code_sandbox"]}},
        "diagnostic_notice": "search_diagnostics are runtime controller telemetry only; they are not mathematical correctness or proof evidence.",
        "notice": "Equal output ceilings are not equal actual total tokens or money. Public-data contamination is unknown. "
                  "Ungraded is counted as not verified correct; resolve it blind with the same policy in both arms. "
                  "Small/development samples do not establish general improvement. Internal review is not independent proof grading.",
        "pairs": observations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="append", required=True, type=Path)
    parser.add_argument("--agent", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--agent-ablation", action="store_true",
                        help="Permit an explicit agent-vs-agent controller ablation")
    args = parser.parse_args()
    result = paired_comparison(args.baseline, args.agent, agent_ablation=args.agent_ablation)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "pairs"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
