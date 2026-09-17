"""Gold-free independent-sample baseline tests using only mocked HTTP."""

import asyncio
import json
from dataclasses import replace

import httpx
from mathagent.evaluation.answerbench import Limits, run_batch
from mathagent.providers.remote import ProviderConfig

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
CONFIG = ProviderConfig("deepseek", "synthetic", "deepseek-v4-flash", "https://api.deepseek.com", True)


def setup(tmp_path):
    path = tmp_path / "problems.json"
    path.write_text(json.dumps({"schema_version": 1, "benchmark": "synthetic", "problems": [
        {"id": "one", "category": "test", "problem": "Compute 1+1."}]}), encoding="utf-8")
    return path


def limits(**changes):
    return replace(Limits(evaluation_mode="answer", completion_policy="draft",
        solver="independent_samples", request_budget=2, max_output_tokens=256,
        case_timeout_seconds=30, parallel_cases=1), **changes)


def completion(body):
    return httpx.Response(200, json={"id": "mock", "choices": [{"finish_reason": "stop",
        "message": {"content": body}}], "usage": {"prompt_tokens": 1, "completion_tokens": 10, "total_tokens": 11}})


def test_samples_reset_to_original_problem_and_tie_uses_earliest(tmp_path):
    messages = []
    def factory(case):
        async def respond(request):
            payload = json.loads(request.content)
            messages.append(payload["messages"])
            return completion(r"candidate $\boxed{" + ("2" if len(messages) == 1 else "3") + r"}$")
        return httpx.MockTransport(respond)
    output = tmp_path / "run"
    asyncio.run(run_batch(setup(tmp_path), output, CONFIG, limits(), ROOT, transport_factory=factory))
    assert all(len(item) == 2 and item[1]["content"] == "Compute 1+1." for item in messages)
    report = json.loads((output / "one/report.json").read_text())
    assert report["selection_rule"] == "independent-single-box-whitespace-vote-earliest-v1"
    assert report["final_answer"] == "2" and report["answer_independently_submitted"]


def test_unknown_after_candidate_keeps_candidate_and_cost(tmp_path):
    calls = 0
    def factory(case):
        async def respond(request):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise httpx.ReadError("unknown", request=request)
            return completion(r"candidate $\boxed{2}$")
        return httpx.MockTransport(respond)
    output = tmp_path / "run"
    asyncio.run(run_batch(setup(tmp_path), output, CONFIG, limits(unknown_recovery="once"), ROOT,
                          transport_factory=factory))
    report = json.loads((output / "one/report.json").read_text())
    assert report["final_answer"] == "2" and report["answer_independently_submitted"]
    assert report["state"] == "reconciliation_required" and report["budget"]["unknown"] == 1


def test_malformed_complete_response_counts_request_but_does_not_vote(tmp_path):
    def factory(case):
        async def respond(request):
            return httpx.Response(200, json={"id": "mock", "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 10, "total_tokens": 11}})
        return httpx.MockTransport(respond)
    output = tmp_path / "run"
    asyncio.run(run_batch(setup(tmp_path), output, CONFIG, limits(request_budget=1), ROOT,
                          transport_factory=factory))
    report = json.loads((output / "one/report.json").read_text())
    assert report["requests"] == 1 and report["budget"]["spent"] == 1
    assert report["final_answer"] is None and not report["candidates"]
