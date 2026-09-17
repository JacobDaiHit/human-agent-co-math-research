"""Syntax-only repair is separate from mathematical search and length recovery."""

import json

import pytest
from mathagent.providers.protocol import messages_for, repair_feedback


@pytest.mark.parametrize("raw", [
    '{"body":"Use the "boundary" case.","actions":[]}',
    '{"body":"Still unresolved.","actions":[{"type":"run_code","arguments":{"code":"print(1)"}}},{"type":"report_progress"}]}',
])
def test_json_feedback_locates_error_and_bounds_excerpt(raw):
    item = repair_feedback(raw, {"mode": "research"})[0]
    assert item["type"] == "json_invalid"
    assert item["line"] == 1 and item["column"] == item["offset"] + 1
    assert len(item["excerpt"]) <= 160
    assert "Preserve mathematical content" in item["instruction"]


def test_repair_prompt_does_not_repeat_solver_goal_or_length_recovery_instruction():
    raw = '{"body":"Incomplete reasoning."}'
    task = {"mode": "research", "autonomous": True, "goal_object_id": "goal",
            "instruction": "ORIGINAL_SOLVE_INSTRUCTION", "inputs": [],
            "repair_output": raw, "repair_feedback": [],
            "output_limit_recovery": {"reason": "length"}}
    messages = messages_for(task)
    assert "本次仅修复" in messages[0]["content"]
    assert "唯一一次输出截断恢复" not in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["repair_output"] == raw
    assert "ORIGINAL_SOLVE_INSTRUCTION" not in payload["instruction"]
