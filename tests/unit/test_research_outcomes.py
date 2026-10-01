from mathagent.evaluation.outcomes import RESEARCH_SUBMISSION_RULE, agent_outcomes


def test_explicit_submission_does_not_depend_on_boxes_or_a_review():
    state = {"session": {"state": "completed", "answer": "7",
                         "solution_revision_id": "selected", "outcome": "solved"},
             "solution": {"body": r"普通推导，正文包含 $\boxed{2}$ 和 $\boxed{3}$，没有缺口。"}}
    steps = [{"body": r"不采用的旧猜测 $\boxed{99}$"}]
    outcome = agent_outcomes(steps, [], [], True, state)
    assert outcome["answer_submission"]["answer"] == "7"
    assert outcome["answer_submission"]["source_id"] == "selected"
    assert outcome["answer_submission"]["selection_rule"] == RESEARCH_SUBMISSION_RULE
    assert outcome["proof_assessment"]["status"] == "not_reviewed"
    assert not outcome["proof_assessment"]["mathematical_correctness_verified"]


def test_budget_exhaustion_never_selects_a_historical_guess():
    state = {"session": {"state": "budget_exhausted", "answer": None,
                         "solution_revision_id": None, "outcome": None}, "solution": None}
    outcome = agent_outcomes([{"body": r"早期猜测 $\boxed{99}$"}], [], [], False, state)
    assert outcome["answer_submission"]["answer"] is None
    assert outcome["candidate_answer"]["answer"] is None


def test_workflow_completion_alone_cannot_imply_a_passed_review():
    outcome = agent_outcomes([], [], [], True)
    assert outcome["proof_assessment"]["status"] == "not_reviewed"
