"""Record submitted mathematics and reported usage, without grading it."""

import hashlib

from mathagent.providers.observability import usage_summary as usage_summary
from mathagent.runtime.completion import boxed_answers

SUBMISSION_RULE = "declared-body-optional-short-answer-v1"
RESEARCH_SUBMISSION_RULE = "explicit-root-research-submission-v1"


def submission(body, *, declared, source, source_id=None):
    answers = boxed_answers(body)
    valid = declared and isinstance(body, str) and bool(body.strip())
    return {"status": "submitted" if valid else "absent",
            "answer": answers[0] if valid and len(answers) == 1 else None,
            "body_present": bool(body and body.strip()),
            "selection_rule": SUBMISSION_RULE, "source": source, "source_id": source_id,
            "body_sha256": hashlib.sha256((body or "").encode()).hexdigest()}


def agent_outcomes(steps, calls, research_state=None):
    """Never search earlier guesses or child reviews for a matching answer."""
    last = steps[-1] if steps else {}
    if research_state is not None:
        root = research_state["session"]
        body = (research_state.get("solution") or {}).get("body", "")
        submitted = root["state"] == "completed" and bool(body.strip()) and bool(root.get("solution_revision_id"))
        final = {"status": "submitted" if submitted else "absent",
                 "answer": root.get("answer") if submitted else None,
                 "body_present": bool(body.strip()),
                 "selection_rule": RESEARCH_SUBMISSION_RULE, "source": "explicit_research_submission",
                 "source_id": root.get("solution_revision_id"),
                 "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
        return {"answer_submission": final, "researcher_outcome": root.get("outcome")}
    call = next((row for row in calls if row.get("request_id") == last.get("request_id")), {})
    result = call.get("result") or {}
    final = submission(last.get("body"), declared=result.get("next_action") == "finish",
                       source="last_root_step", source_id=last.get("output_revision_id"))
    return {"answer_submission": final, "researcher_outcome": None}
