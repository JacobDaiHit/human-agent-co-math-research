"""Mode-dependent constraints are visible before generation and remain enforced."""

import json
from pathlib import Path

import pytest
from mathagent.providers.protocol import messages_for, result_schema, validate_result
from pydantic import ValidationError


@pytest.mark.parametrize("mode", ["research", "review"])
def test_actual_prompt_uses_the_assigned_mode_schema(mode):
    task = {"mode": mode, "goal_object_id": "goal", "instruction": "Check the target", "inputs": []}
    system = messages_for(task)[0]["content"]
    schema = json.loads(system.rsplit("\n", 1)[1])
    assert schema == result_schema(mode)
    assert schema["properties"]["mode"] == {"type": "string", "const": mode}
    assert "verdict" in schema["required"]
    if mode == "research":
        assert schema["properties"]["verdict"]["type"] == "null"
        assert schema["properties"]["verdict"]["const"] is None
        assert "顶层 verdict 必须为 null" in system
    else:
        assert schema["properties"]["verdict"]["enum"] == ["passed", "issues", "inconclusive"]
        assert "scope" in schema["required"]
        assert schema["properties"]["scope"]["type"] == "string"
    assert result_schema("research")["properties"]["verdict"]["type"] == "null"


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
