"""Paired statistics keep discordant outcomes, costs, and exclusions visible."""

import importlib.util
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from mathagent.evaluation.answerbench import Limits

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("comparison", ROOT / "scripts/compare_answerbench.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def arm(tmp_path, name, solver, grades, *, controller="legacy", search_config=None,
        builtin_calculator=False, code_sandbox=False, search_state=None):
    path = tmp_path / name
    path.mkdir()
    settings = asdict(Limits(evaluation_mode="answer", completion_policy="draft", solver=solver,
        cumulative_output_token_budget=32768, builtin_calculator=builtin_calculator,
        code_sandbox=code_sandbox, solver_controller=controller, search_config=search_config or {}))
    plan = {"configuration": {"provider": "deepseek", "requested_model": "frozen-model",
        "problems_sha256": "problems", "case_ids": list(grades), "submission_rule": "frozen-rule",
        "source": "frozen-source", "limits": settings}}
    (path / "plan.json").write_text(json.dumps(plan))
    (path / "report.json").write_text(json.dumps({"source_unchanged": True}))
    scores = {"answer_key_sha256": "key", "scorer_sha256": "scorer", "grading_method": "same",
        "cases": [{"problem_id": key, "grade": grade} for key, grade in grades.items()]}
    (path / "scores.json").write_text(json.dumps(scores))
    for key in grades:
        (path / key).mkdir()
        report = {"unattended_eligible": True, "requests": 2,
            "usage_summary": {"all_usage_known": True, "reported_tokens": {"total_tokens": 100}}}
        if search_state is not None:
            report["search_state"] = search_state
        (path / key / "report.json").write_text(json.dumps(report))
    return path


def test_paired_wins_losses_and_unknown_bounds(tmp_path):
    baseline = arm(tmp_path, "baseline", "self_refine", {"a": "correct", "b": "incorrect", "c": "ungraded"})
    agent = arm(tmp_path, "agent", "agent", {"a": "incorrect", "b": "correct", "c": "correct"})
    result = module.paired_comparison([baseline], [agent], bootstrap_samples=100)
    assert result["agent_wins"] == 2 and result["agent_losses"] == 1
    assert result["accuracy_delta"] == pytest.approx(1/3)
    assert result["delta_bounds_if_ungraded_resolved"] == [0, 1/3]
    assert result["costs"]["agent"]["reported_total_tokens"] == 300


def test_configuration_mismatch_and_double_counting_are_rejected(tmp_path):
    baseline = arm(tmp_path, "baseline", "self_refine", {"a": "correct"})
    agent = arm(tmp_path, "agent", "agent", {"a": "correct"})
    with pytest.raises(ValueError, match="double-count"):
        module.paired_comparison([baseline, baseline], [agent, agent], bootstrap_samples=20)
    plan = json.loads((agent / "plan.json").read_text())
    plan["configuration"]["limits"]["reasoning_effort"] = "high"
    (agent / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="reasoning_effort"):
        module.paired_comparison([baseline], [agent], bootstrap_samples=20)


def test_missing_failure_case_cannot_be_dropped(tmp_path):
    baseline = arm(tmp_path, "baseline", "self_refine", {"a": "correct", "b": "incorrect"})
    agent = arm(tmp_path, "agent", "agent", {"a": "correct", "b": "incorrect"})
    scores = json.loads((agent / "scores.json").read_text())
    scores["cases"].pop()
    (agent / "scores.json").write_text(json.dumps(scores))
    with pytest.raises(ValueError, match="Missing"):
        module.paired_comparison([baseline], [agent], bootstrap_samples=20)


def test_agent_ablation_freezes_controller_and_reports_runtime_diagnostics(tmp_path):
    baseline = arm(tmp_path, "legacy-agent", "agent", {"a": "correct"},
                   controller="legacy", builtin_calculator=False,
                   search_state={"routes": [], "gaps": ["g"], "memory": [], "decisions": []})
    agent = arm(tmp_path, "bounded-agent", "agent", {"a": "correct"},
                controller="bounded_search_v1", search_config={"strategy": "x"},
                builtin_calculator=True,
                search_state={"routes": [{"id": "r1"}], "gaps": [], "memory": [{"id": "m"}],
                              "decisions": [{"actions": [{"type": "read"}, {"type": "write"}]}]})
    result = module.paired_comparison([baseline], [agent], bootstrap_samples=20, agent_ablation=True)
    assert result["comparison"] == "agent_controller_ablation"
    assert result["tools_ablation"] is True
    assert result["arm_configuration"]["agent"]["solver_controller"] == "bounded_search_v1"
    assert result["costs"]["agent"]["search_diagnostics"][0]["route_count"] == 1
    assert result["costs"]["agent"]["search_diagnostics"][0]["work_action_count"] == 2


def test_agent_ablation_is_required_for_agent_vs_agent(tmp_path):
    baseline = arm(tmp_path, "legacy-agent", "agent", {"a": "correct"})
    agent = arm(tmp_path, "bounded-agent", "agent", {"a": "correct"})
    with pytest.raises(ValueError, match="Left arm"):
        module.paired_comparison([baseline], [agent], bootstrap_samples=20)
