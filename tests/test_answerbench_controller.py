"""The only dispatchable autonomous solver uses explicit research submission."""
import pytest
from mathagent.evaluation.answerbench import Limits, instruction_for


def test_default_solver_and_independent_discussion_instructions():
    limits = Limits()
    limits.validate()
    assert limits.solver_controller == "continuous_research"
    assert limits.discussion is True
    text = instruction_for(limits)
    assert "neutral research goal" in text and "not your candidate answer" in text
    assert "not an approving authority" in text and "explicitly submit" in text


@pytest.mark.parametrize("controller", ["legacy", "bounded_search_v1"])
def test_historical_solver_configurations_cannot_start_paid_runs(controller):
    with pytest.raises(ValueError, match="Retired"):
        Limits(solver_controller=controller).validate()


def test_invalid_discussion_configuration_rejected_before_dispatch():
    with pytest.raises(ValueError, match="discussion"):
        Limits(discussion="yes").validate()
