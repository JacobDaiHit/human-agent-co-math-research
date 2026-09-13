"""Synthetic inference only: identical scoring contract, independent proof state."""

import asyncio
import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mathagent.evaluation.answerbench import Limits, run_batch
from mathagent.evaluation.outcomes import agent_outcomes
from mathagent.providers.remote import ProviderConfig

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ProviderConfig("deepseek", "synthetic-never-networked", "deepseek-v4-flash", "https://api.deepseek.com", True)


def problems(tmp_path):
    path = tmp_path / "problems.json"
    path.write_text(json.dumps({"schema_version": 1, "benchmark": "synthetic", "problems": [
        {"id": "synthetic", "category": "Algebra", "problem": "Compute $1+1$."}]}), encoding="utf-8")
    return path


def options(**kwargs):
    return replace(Limits(evaluation_mode="answer", completion_policy="draft", solver="direct",
        request_budget=1, max_output_tokens=256, case_timeout_seconds=30, parallel_cases=1), **kwargs)


def test_direct_and_self_refine_use_last_response_not_best_answer(tmp_path):
    messages = []

    def factory(case):
        async def respond(request):
            payload = json.loads(request.content)
            assert "response_format" not in payload and "tools" not in payload
            messages.append(payload["messages"])
            answer = "2" if len(messages) == 1 else "3"
            return httpx.Response(200, json={"id": "synthetic", "choices": [{"finish_reason": "stop",
                "message": {"content": rf"My final answer is $\boxed{{{answer}}}$."}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 32, "total_tokens": 52}})
        return httpx.MockTransport(respond)

    output = tmp_path / "run"
    asyncio.run(run_batch(problems(tmp_path), output, CONFIG,
        options(solver="self_refine", request_budget=2, cumulative_output_token_budget=512), ROOT,
        transport_factory=factory))
    report = json.loads((output / "synthetic/report.json").read_text())
    assert report["answer_submission"]["answer"] == "3"
    assert report["proof_assessment"]["status"] == "not_reviewed"
    assert report["usage_summary"]["reported_tokens"]["total_tokens"] == 104
    assert report["occupied_output_tokens"] == 64
    assert messages[1][2]["role"] == "assistant"
    assert report["human_interventions"] == 0


def test_unknown_baseline_keeps_output_reservation_and_stops_without_extra_dispatch(tmp_path):
    dispatches = []

    def factory(case):
        async def respond(request):
            dispatches.append(1)
            raise httpx.ReadError("synthetic unknown", request=request)
        return httpx.MockTransport(respond)

    output = tmp_path / "run"
    asyncio.run(run_batch(problems(tmp_path), output, CONFIG,
        options(solver="self_refine", request_budget=3, unknown_recovery="once", cumulative_output_token_budget=256),
        ROOT, transport_factory=factory))
    report = json.loads((output / "synthetic/report.json").read_text())
    assert len(dispatches) == 1
    assert report["occupied_output_tokens"] == 256
    assert report["budget"]["unknown"] == 1 and report["final_answer"] is None


def test_agent_answer_mode_can_submit_without_claiming_proof_review(tmp_path):
    def factory(case):
        async def respond(request):
            payload = json.loads(request.content)
            task = json.loads(payload["messages"][-1]["content"])
            assert task["completion_requirements"]["policy"] == "draft"
            assert '"calculate": {' not in payload["messages"][0]["content"]
            result = {"mode": "research", "body": r"Candidate $\boxed{2}$; proof incomplete.",
                "findings": ["Synthetic gap."], "verdict": None, "next_action": "finish", "actions": []}
            return httpx.Response(200, json={"id": "synthetic", "choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 32, "total_tokens": 52}})
        return httpx.MockTransport(respond)

    output = tmp_path / "run"
    asyncio.run(run_batch(problems(tmp_path), output, CONFIG,
        options(solver="agent", builtin_calculator=False), ROOT, transport_factory=factory))
    report = json.loads((output / "synthetic/report.json").read_text())
    assert report["completed"] is True and report["final_answer"] == "2"
    assert report["workflow_completed"] is False
    assert report["proof_assessment"]["status"] == "not_reviewed"


def test_unfinished_candidate_is_not_a_submission_and_old_guess_is_not_selected():
    steps = [{"request_id": "a", "body": r"\boxed{2}"}, {"request_id": "b", "body": "Unresolved"}]
    calls = [{"request_id": "a", "result": {"next_action": "finish"}},
             {"request_id": "b", "result": {"next_action": "continue"}}]
    result = agent_outcomes(steps, calls, [], False)
    assert result["answer_submission"]["answer"] is None
    assert result["candidate_answer"]["answer"] is None
    result = agent_outcomes(steps[:1], [{"request_id": "a", "result": {"next_action": "continue"}}], [], False)
    assert result["candidate_answer"]["answer"] == "2"
    assert result["answer_submission"]["answer"] is None


def test_v2_answer_grades_independently_of_proof_but_legacy_does_not(monkeypatch):
    spec = importlib.util.spec_from_file_location("grader", ROOT / "scripts/score_answerbench.py")
    grader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grader)
    monkeypatch.setattr(grader, "compare", grader.compare_worker)
    body = r"\boxed{2}"
    report = {"problem_id": "synthetic", "report_schema_version": "2.0", "completed": False,
        "state": "budget_exhausted", "final_body": body, "proof_assessment": {"status": "issues"},
        "answer_submission": {"status": "submitted", "answer": "2",
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "selection_rule": "last-root-step-explicit-finish-single-box-v1"}}
    key = {"id": "synthetic", "short_answer": "2", "answer_type": "integer"}
    scored = grader.score_report(report, key)
    assert scored["grade"] == "correct" and scored["completed"] is False
    assert scored["proof_assessment"]["status"] == "issues"
    report.pop("report_schema_version")
    assert grader.score_report(report, key)["grade"] != "correct"


def test_baseline_validation_rejects_unfair_or_unsupported_modes():
    with pytest.raises(ValueError):
        options(request_budget=2).validate()
    with pytest.raises(ValueError):
        options(code_sandbox=True).validate()


def test_old_rejected_version_does_not_taint_new_submission_proof_status():
    steps = [{"request_id": "last", "body": r"\boxed{2}"}]
    calls = [{"request_id": "last", "result": {"next_action": "finish", "cited_revision_ids": ["new"]}}]
    result = agent_outcomes(steps, calls, [{"target_revision_id": "old", "verdict": "issues"}], False)
    assert result["proof_assessment"]["status"] == "not_reviewed"
    assert result["proof_assessment"]["all_internal_reviews"] == 1
