"""Gold-independent, predeclared submission selection and separate outcome axes."""

import hashlib

from mathagent.runtime.completion import boxed_answers

SUBMISSION_RULE = "last-root-step-explicit-finish-single-box-v1"
RESEARCH_SUBMISSION_RULE = "explicit-root-research-submission-v1"


def submission(body, *, declared, source, source_id=None):
    answers = boxed_answers(body)
    valid = declared and len(answers) == 1
    return {"status": "submitted" if valid else "absent", "answer": answers[0] if valid else None,
            "selection_rule": SUBMISSION_RULE, "source": source, "source_id": source_id,
            "body_sha256": hashlib.sha256((body or "").encode()).hexdigest()}


def agent_outcomes(steps, calls, reviews, workflow_completed, research_state=None):
    """Never search earlier guesses or child reviews for a matching answer."""
    last = steps[-1] if steps else {}
    if research_state is not None:
        root = research_state["session"]
        body = (research_state.get("solution") or {}).get("body", "")
        submitted = root["state"] == "completed" and isinstance(root.get("answer"), str) and bool(root["answer"].strip())
        final = {"status": "submitted" if submitted else "absent",
                 "answer": root["answer"] if submitted else None,
                 "selection_rule": RESEARCH_SUBMISSION_RULE, "source": "explicit_research_submission",
                 "source_id": root.get("solution_revision_id"),
                 "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
        return {"answer_submission": final,
                "candidate_answer": {**final, "status": "absent", "answer": None},
                "proof_assessment": {"status": "not_reviewed", "formal_verification": False,
                    "mathematical_correctness_verified": False, "scope": "submitted_research_body",
                    "researcher_outcome": root.get("outcome"), "reviews": 0,
                    "all_internal_reviews": len(reviews)}}
    call = next((row for row in calls if row.get("request_id") == last.get("request_id")), {})
    result = call.get("result") or {}
    final = submission(last.get("body"), declared=result.get("next_action") == "finish",
                       source="last_root_step", source_id=last.get("output_revision_id"))
    # Diagnostic only: an unfinished last step's box is never promoted into a final.
    candidate = submission(last.get("body"), declared=True, source="last_root_step",
                           source_id=last.get("output_revision_id"))
    candidate["status"] = "candidate" if candidate["answer"] is not None else "absent"
    cited = set(result.get("cited_revision_ids") or [])
    relevant_reviews = [r for r in reviews if r.get("target_revision_id") in cited]
    return {"answer_submission": final, "candidate_answer": candidate,
            "proof_assessment": {"status": "review_passed" if any(r.get("verdict") == "passed" for r in relevant_reviews) else
                "issues" if any(r.get("verdict") == "issues" for r in relevant_reviews) else
                "inconclusive" if relevant_reviews else "not_reviewed",
                "formal_verification": False, "mathematical_correctness_verified": False,
                "scope": "last_root_step_cited_revisions", "reviews": len(relevant_reviews),
                "all_internal_reviews": len(reviews)}}


def usage_summary(calls):
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens")
    totals = {key: 0 for key in keys}
    missing = 0
    for call in calls:
        usage = call.get("usage") or {}
        if not all(type(usage.get(k)) is int and usage[k] >= 0 for k in keys[:3]):
            missing += 1
        for key in keys:
            if type(usage.get(key)) is int and usage[key] >= 0:
                totals[key] += usage[key]
    return {"reported_tokens": totals, "calls_with_incomplete_usage": missing,
            "all_usage_known": missing == 0, "includes_reviews_and_recovery": True}
