"""Run a fixed problem-only batch; scoring is a separate, offline command."""

import argparse
import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

from mathagent.config import load_local_environment
from mathagent.evaluation.answerbench import Limits, run_batch
from mathagent.providers.remote import ProviderConfig


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run this bounded paid batch")
    parser.add_argument("--problems", type=Path, default=root / "fixtures/imo_answerbench/problem-only.json")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--effort", choices=["high", "max"], default="max")
    parser.add_argument("--unknown-recovery", choices=["stop", "once"], default="stop",
                        help="Retain unknown request occupancy; allow at most one budgeted retry per case tree")
    parser.add_argument("--case-id", action="append", help="Run only this exact frozen case; repeatable")
    parser.add_argument("--code-sandbox", action="store_true", help="Require the preconfigured local offline Docker sandbox; fail before inference if unavailable")
    parser.add_argument("--evaluation-mode", choices=["answer", "research"], default="research")
    parser.add_argument("--solver", choices=["agent", "direct", "self_refine", "independent_samples"], default="agent",
                        help="direct: one call; self_refine: fixed refinement; independent_samples: independent vote")
    parser.add_argument("--solver-controller", choices=["continuous_research"], default="continuous_research")
    parser.add_argument("--no-discussion", action="store_true", help="Disable the research peer for a same-budget comparison")
    parser.add_argument("--cumulative-output-token-budget", type=int,
                        help="Shared output/thinking token cap; unknown usage keeps its reservation. Input tokens reported separately.")
    parser.add_argument("--case-order-seed", type=int, default=0,
                        help="Local task ordering only, not a provider sampling seed")
    parser.add_argument("--request-budget", type=int, default=12)
    parser.add_argument("--max-output-tokens", type=int, default=65536)
    parser.add_argument("--request-timeout", type=int, default=600)
    parser.add_argument("--case-timeout", type=int, default=1800)
    parser.add_argument("--parallel-cases", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    if not args.execute:
        parser.error("No requests made. Use --execute for a bounded real batch.")
    load_local_environment()
    config = replace(ProviderConfig.from_env("deepseek"), model=args.model)
    limits = Limits(request_budget=args.request_budget,
        max_output_tokens=args.max_output_tokens, request_timeout_seconds=args.request_timeout,
        case_timeout_seconds=args.case_timeout, parallel_cases=args.parallel_cases,
        reasoning_effort=args.effort,
        unknown_recovery=args.unknown_recovery, code_sandbox=args.code_sandbox,
        evaluation_mode=args.evaluation_mode, solver=args.solver,
        solver_controller=args.solver_controller,
        discussion=not args.no_discussion, research_deadline_seconds=args.case_timeout,
        cumulative_output_token_budget=args.cumulative_output_token_budget,
        case_order_seed=args.case_order_seed)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    try:
        result = asyncio.run(run_batch(args.problems, args.output, config, limits, root,
                                      resume=args.resume, case_ids=args.case_id))
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["all_completed"] and result["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
