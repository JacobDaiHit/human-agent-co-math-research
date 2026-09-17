"""The same neutral possibility is visible to every arm; no benchmark answer hint."""

import pytest
from mathagent.evaluation.direct import DIRECT_INSTRUCTION
from mathagent.providers.actions import ReportProgress
from mathagent.providers.mathematical_guidance import SOLUTION_SET_GUIDANCE
from mathagent.providers.protocol import messages_for
from mathagent.runtime.completion import boxed_answers


@pytest.mark.parametrize("mode", ["research", "review"])
def test_solution_set_guidance_in_both_agent_roles_and_baseline(mode):
    task = {"mode": mode, "inputs": [], "instruction": "Do the assigned task.",
            "goal_object_id": "goal", "autonomous": mode == "research"}
    system = messages_for(task)[0]["content"]
    assert system.count(SOLUTION_SET_GUIDANCE) == 1
    assert SOLUTION_SET_GUIDANCE in DIRECT_INSTRUCTION
    assert "empty, finite or infinite" in system
    assert "unless a proved bound" in system
    assert "one valid\nsolution refutes" in system
    assert not any(hint in SOLUTION_SET_GUIDANCE for hint in ("2014", "4028", "Thue", "number_theory-081"))


def test_empty_set_answer_is_distinct_from_absent_candidate_id_and_abstention():
    report = ReportProgress(status="candidate")
    assert report.candidate_revision_id is None  # Use this response's saved body.
    assert boxed_answers(r"No admissible values; the solution set is $\boxed{\varnothing}$.") == [r"\varnothing"]
    assert boxed_answers("No answer established; the search is unresolved.") == []
