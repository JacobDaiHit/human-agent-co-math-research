"""Mode-dependent constraints are visible before generation and remain enforced."""

import json
from pathlib import Path

import pytest
from mathagent.providers.protocol import messages_for, result_schema, validate_result
from pydantic import ValidationError


@pytest.mark.parametrize("mode", ["research", "review"])
@pytest.mark.parametrize("autonomous", [False, True])
def test_actual_prompt_uses_the_assigned_mode_schema(mode, autonomous):
    task = {"mode": mode, "goal_object_id": "goal", "instruction": "Check the target", "inputs": [],
            "autonomous": autonomous}
    system = messages_for(task)[0]["content"]
    schema = json.loads(system.rsplit("\n", 1)[1])
    assert schema == result_schema(mode, autonomous=autonomous)
    assert schema["properties"]["mode"] == {"type": "string", "const": mode}
    assert "verdict" in schema["required"]
    assert ("next_action" in schema["required"]) is (autonomous and mode == "research")
    if autonomous and mode == "research":
        assert "default" not in schema["properties"]["next_action"]
    if mode == "research":
        assert schema["properties"]["verdict"]["type"] == "null"
        assert schema["properties"]["verdict"]["const"] is None
        assert "顶层 verdict 必须为 null" in system
    else:
        assert schema["properties"]["verdict"]["enum"] == ["passed", "issues", "inconclusive"]
        assert "scope" in schema["required"]
        assert schema["properties"]["scope"]["type"] == "string"
    assert result_schema("research")["properties"]["verdict"]["type"] == "null"
    assert "恰好是一个 JSON 对象" in system
    assert "第二个 JSON 值" in system
    assert "裸控制字符" in system


def test_autonomous_next_action_is_explicit_while_single_round_and_reviews_stay_compatible():
    value = {"mode": "research", "body": "A candidate response", "findings": ["Needs review"]}
    before = dict(value)
    with pytest.raises(ValueError, match="explicitly provide next_action"):
        validate_result(value, mode="research", read_set={}, autonomous=True)
    assert value == before
    assert validate_result(value, mode="research", read_set={}).next_action == "finish"
    # Already-parsed models also retain whether the caller supplied the field.
    defaulted = validate_result(value, mode="research", read_set={})
    with pytest.raises(ValueError, match="explicitly provide next_action"):
        validate_result(defaulted, mode="research", read_set={}, autonomous=True)
    for action in ("continue", "wait", "finish"):
        assert validate_result({**value, "next_action": action}, mode="research",
                               read_set={}, autonomous=True).next_action == action
    review = {**value, "mode": "review", "scope": "One candidate", "verdict": "inconclusive"}
    assert validate_result(review, mode="review", read_set={}, autonomous=True).next_action == "finish"


def test_completion_requirements_and_live_request_allowance_reach_the_prompt():
    requirements = {"policy": "reviewed_answer", "issues": ["independent_review_required"]}
    budget = {"remaining": 3, "occupied": 2}
    messages = messages_for({"mode": "research", "goal_object_id": "goal", "instruction": "Research",
        "inputs": [], "autonomous": True, "completion_requirements": requirements,
        "request_budget_status": budget})
    user = json.loads(messages[1]["content"])
    assert user["completion_requirements"] == requirements
    assert user["request_budget_status"] == budget
    assert "completion_requirements" in messages[0]["content"]
    assert "request_budget_status" in messages[0]["content"]


def test_actual_pilot_response_missing_next_action_is_rejected_without_rewriting_math():
    report_path = Path(__file__).parents[2] / "data/benchmarks/answerbench-four-run-01/imo-bench-number_theory-081/report.json"
    if not report_path.exists():
        pytest.skip("The local real-pilot evidence is not shipped with the repository")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    raw = report["calls"][0]["raw_text"]
    actual = json.loads(raw)
    assert actual["mode"] == "research" and "next_action" not in actual
    assert [action["type"] for action in actual["actions"]] == ["write_draft"]
    before = json.dumps(actual, sort_keys=True)
    with pytest.raises(ValueError, match="explicitly provide next_action"):
        validate_result(actual, mode="research", read_set={}, autonomous=True,
                        context_revision_ids=actual.get("cited_revision_ids", []))
    assert json.dumps(actual, sort_keys=True) == before


def test_saved_real_research_verdict_stays_invalid_without_silent_normalization():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/research_verdict_mismatch.json").read_text(
            encoding="utf-8"
        )
    )
    actual = json.loads(fixture["raw_text"])
    assert fixture["complete"] and not fixture["raw_text_truncated"]
    assert fixture["finish_reason"] == "stop"
    assert actual["mode"] == "research" and actual["verdict"] == "passed"
    before = json.dumps(actual, sort_keys=True)
    with pytest.raises(ValidationError, match="Research drafts cannot issue review verdicts"):
        validate_result(
            actual, mode="research", read_set={}, context_revision_ids=actual["cited_revision_ids"]
        )
    assert json.dumps(actual, sort_keys=True) == before


def test_review_scope_and_verdict_remain_required_by_validation():
    with pytest.raises(ValidationError):
        validate_result(
            {
                "mode": "review",
                "body": "Partial review",
                "findings": ["Incomplete"],
                "verdict": None,
            },
            mode="review",
            read_set={},
        )
