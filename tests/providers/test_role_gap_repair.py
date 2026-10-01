"""Research gap-role violations receive one safe, bounded repair attempt."""

import json

import pytest
from mathagent.providers.protocol import repair_feedback, result_schema, validate_result
from pydantic import ValidationError


def test_research_gap_schema_validation_and_feedback_are_role_specific():
    research_schema = result_schema("research", autonomous=True)
    review_schema = result_schema("review")
    gaps = research_schema["properties"]["structured_gaps"]
    assert gaps["maxItems"] == 0
    assert "body/findings" in gaps["description"]
    assert "report_gap" in gaps["description"]
    assert review_schema["properties"]["structured_gaps"]["maxItems"] > 0

    raw = json.dumps({
        "mode": "research", "body": "Keep this candidate argument. do-not-echo",
        "findings": ["The final implication needs a proof."], "verdict": None,
        "structured_gaps": [{"kind": "missing_argument", "anchor": "final-step",
                             "detail": "The implication needs a proof."}],
        "next_action": "finish",
    })
    with pytest.raises(ValidationError, match="Only reviews can report structured_gaps"):
        validate_result(json.loads(raw), mode="research", read_set={}, autonomous=True)
    feedback = repair_feedback(raw, {"mode": "research", "read_set": {}, "autonomous": True})
    assert feedback[0]["loc"] == [] and feedback[0]["type"] == "value_error"
    assert "Only reviews can report structured_gaps" in feedback[0]["msg"]
    assert "input_value" not in json.dumps(feedback)
    assert "do-not-echo" not in json.dumps(feedback)
