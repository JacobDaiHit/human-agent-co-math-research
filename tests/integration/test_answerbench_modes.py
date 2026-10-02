"""Synthetic inference only: record submissions and costs, never grade mathematics."""

import asyncio
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
    report = json.loads((output / "synthetic/report.json").read_text(encoding="utf-8"))
    assert report["answer_submission"]["answer"] == "3"
    assert "proof_assessment" not in report
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
    report = json.loads((output / "synthetic/report.json").read_text(encoding="utf-8"))
    assert len(dispatches) == 1
    assert report["occupied_output_tokens"] == 256
    assert report["budget"]["unknown"] == 1 and report["final_answer"] is None


def test_agent_answer_mode_can_submit_without_claiming_proof_review(tmp_path):
    def factory(case):
        async def respond(request):
            payload = json.loads(request.content)
            assert "response_format" not in payload
            assert "compute" not in [tool["function"]["name"] for tool in payload["tools"]]
            result = {"content": r"Candidate $\boxed{2}$; proof incomplete.",
                "tool_calls": [{"id": "submission", "type": "function", "function": {
                    "name": "submit_solution", "arguments": json.dumps({"outcome": "unresolved", "answer": "2"})}}]}
            return httpx.Response(200, json={"id": "synthetic", "choices": [{"finish_reason": "stop",
                "message": result}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 32, "total_tokens": 52}})
        return httpx.MockTransport(respond)

    output = tmp_path / "run"
    asyncio.run(run_batch(problems(tmp_path), output, CONFIG,
        options(solver="agent", builtin_calculator=False), ROOT, transport_factory=factory))
    report = json.loads((output / "synthetic/report.json").read_text(encoding="utf-8"))
    assert report["completed"] is True and report["final_answer"] == "2"
    assert report["researcher_outcome"] == "unresolved"
    assert "proof_assessment" not in report and "workflow_completed" not in report


def test_unfinished_candidate_is_not_a_submission_and_old_guess_is_not_selected():
    steps = [{"request_id": "a", "body": r"\boxed{2}"}, {"request_id": "b", "body": "Unresolved"}]
    calls = [{"request_id": "a", "result": {"next_action": "finish"}},
             {"request_id": "b", "result": {"next_action": "continue"}}]
    result = agent_outcomes(steps, calls)
    assert result["answer_submission"]["answer"] is None
    result = agent_outcomes(steps[:1], [{"request_id": "a", "result": {"next_action": "continue"}}])
    assert result["answer_submission"]["answer"] is None


def test_baseline_validation_rejects_unfair_or_unsupported_modes():
    with pytest.raises(ValueError):
        options(request_budget=2).validate()
    with pytest.raises(ValueError):
        options(code_sandbox=True).validate()


def test_historical_calls_do_not_turn_into_a_proof_assessment():
    steps = [{"request_id": "last", "body": r"\boxed{2}"}]
    calls = [{"request_id": "last", "result": {"next_action": "finish", "cited_revision_ids": ["new"]}}]
    result = agent_outcomes(steps, calls)
    assert result["answer_submission"]["answer"] == "2"
    assert "proof_assessment" not in result


def test_proof_only_submission_has_no_grader_or_review_fields(tmp_path):
    def factory(case):
        async def respond(request):
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "content": "PROOF_ONLY_FIXTURE: a saved proof body, not a short answer.",
                "tool_calls": [{"id": "proof", "type": "function", "function": {
                    "name": "submit_solution", "arguments": json.dumps({"outcome": "solved"})}}]}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 32, "total_tokens": 52}})
        return httpx.MockTransport(respond)

    output = tmp_path / "proof-only"
    summary = asyncio.run(run_batch(problems(tmp_path), output, CONFIG,
        options(solver="agent", builtin_calculator=False), ROOT, transport_factory=factory))
    report = json.loads((output / "synthetic/report.json").read_text(encoding="utf-8"))
    assert summary["all_completed"] and report["completed"] and report["submission_present"]
    assert report["final_answer"] is None and report["terminal_reason"] == "completed"
    assert report["researcher_outcome"] == "solved" and report["report_schema_version"] == "3.0"
    assert not {"scored", "proof_assessment", "review_completed", "workflow_completed"}.intersection(report)
