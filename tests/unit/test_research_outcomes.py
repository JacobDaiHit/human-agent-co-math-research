from mathagent.evaluation.outcomes import RESEARCH_SUBMISSION_RULE, agent_outcomes, usage_summary


def test_explicit_submission_does_not_depend_on_boxes_or_a_review():
    state = {"session": {"state": "completed", "answer": "7",
                         "solution_revision_id": "selected", "outcome": "solved"},
             "solution": {"body": r"普通推导，正文包含 $\boxed{2}$ 和 $\boxed{3}$，没有缺口。"}}
    steps = [{"body": r"不采用的旧猜测 $\boxed{99}$"}]
    outcome = agent_outcomes(steps, [], state)
    assert outcome["answer_submission"]["answer"] == "7"
    assert outcome["answer_submission"]["source_id"] == "selected"
    assert outcome["answer_submission"]["selection_rule"] == RESEARCH_SUBMISSION_RULE
    assert outcome["researcher_outcome"] == "solved"
    assert "proof_assessment" not in outcome


def test_budget_exhaustion_never_selects_a_historical_guess():
    state = {"session": {"state": "budget_exhausted", "answer": None,
                         "solution_revision_id": None, "outcome": None}, "solution": None}
    outcome = agent_outcomes([{"body": r"早期猜测 $\boxed{99}$"}], [], state)
    assert outcome["answer_submission"]["answer"] is None
    assert outcome["answer_submission"]["status"] == "absent"


def test_full_proof_without_short_answer_is_a_submission_not_a_correctness_verdict():
    state = {"session": {"state": "completed", "answer": None,
                         "solution_revision_id": "proof", "outcome": "solved"},
             "solution": {"body": "A full mathematical proof without a short-answer field."}}
    outcome = agent_outcomes([], [], state)
    assert outcome["answer_submission"]["status"] == "submitted"
    assert outcome["answer_submission"]["answer"] is None
    assert outcome["researcher_outcome"] == "solved"
    assert set(outcome) == {"answer_submission", "researcher_outcome"}


def test_missing_cache_usage_is_unknown_and_known_usage_is_input_weighted():
    calls = [{"usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11,
                        "prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 0}},
             {"usage": {"prompt_tokens": 90, "completion_tokens": 1, "total_tokens": 91,
                        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 90}},
             {"usage": {"prompt_tokens": 50, "completion_tokens": 1, "total_tokens": 51}}]
    result = usage_summary(calls)
    assert result["cache"]["hit_fraction"] == 0.1
    assert result["cache"]["calls_with_cache_usage"] == 2
    assert result["cache"]["calls_without_cache_usage"] == 1
    assert usage_summary(calls[-1:])["reported_tokens"]["prompt_cache_hit_tokens"] is None
