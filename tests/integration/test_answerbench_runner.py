"""The batch runner uses real isolated API processes and synthetic inference only."""

import asyncio
import builtins
import json
import socket
import sqlite3
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from mathagent.evaluation import answerbench
from mathagent.providers.remote import ProviderConfig

SECRET = "synthetic-answerbench-key-never-sent-to-a-vendor"
ANSWER_SENTINEL = "ANSWER_KEY_MUST_NEVER_ENTER_THE_RUNNER"
CONFIG = ProviderConfig("deepseek", SECRET, "deepseek-v4-flash", "https://api.deepseek.com", True)


def test_sandbox_preflight_blocks_before_any_inference(workspace, tmp_path, monkeypatch):
    problems, root, _ = workspace
    monkeypatch.setattr("mathagent.tools.code_sandbox.CodeSandbox.status", lambda _: {
        "ready": False, "reason": "docker_unavailable", "image_id": None})
    output = tmp_path / "must-not-run"
    with pytest.raises(ValueError, match="Sandbox unavailable before benchmark"):
        asyncio.run(answerbench.run_batch(problems, output, CONFIG,
            replace(answerbench.Limits(), code_sandbox=True), root))
    assert not output.exists()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    cases = [{"id": f"case-{index}", "category": category,
              "problem": f"Private problem marker ONLY_CASE_{index}. Compute $1+1$."}
             for index, category in enumerate(("algebra", "combinatorics", "geometry", "number_theory"))]
    problems = tmp_path / "problem-only.json"
    problems.write_text(json.dumps({"schema_version": 1, "benchmark": "synthetic", "problems": cases}), encoding="utf-8")
    answer_key = tmp_path / "answer-key.json"
    answer_key.write_text(json.dumps({"answer": ANSWER_SENTINEL}), encoding="utf-8")
    # A fixed source fixture avoids unrelated parallel development changing this
    # test's fingerprint. The API subprocess still imports the real application.
    source = tmp_path / "frozen-source"
    (source / "services/api/src/mathagent").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "services/api/src/mathagent/app.py").write_text("# frozen fixture\n", encoding="utf-8")
    (source / "scripts/imo_answerbench.py").write_text("# frozen runner\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("# frozen project\n", encoding="utf-8")
    (source / "uv.lock").write_text("# frozen dependencies\n", encoding="utf-8")

    original_path_open, original_open = Path.open, builtins.open

    def check_open(file, mode):
        if isinstance(file, (str, Path)) and Path(file).resolve() == answer_key.resolve() and "r" in mode:
            pytest.fail("The batch must never open the separate answer key")

    def guarded_path_open(path, mode="r", *args, **kwargs):
        check_open(path, mode)
        return original_path_open(path, mode, *args, **kwargs)

    def guarded_open(file, mode="r", *args, **kwargs):
        check_open(file, mode)
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    original_connect, original_resolve = socket.socket.connect, socket.getaddrinfo

    def loopback_connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            assert address[0] in {"127.0.0.1", "::1"}, "No external test sockets"
        return original_connect(sock, address)

    def loopback_resolve(host, *args, **kwargs):
        assert host in {None, "127.0.0.1", "::1", "localhost", b"127.0.0.1", b"localhost"}
        return original_resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", loopback_connect)
    monkeypatch.setattr(socket, "getaddrinfo", loopback_resolve)
    return problems, source, cases


def limits(**changes):
    return replace(answerbench.Limits(request_budget=5, max_steps=3, max_children=1,
        max_depth=1, max_review_rounds=1, max_output_tokens=256,
        request_timeout_seconds=10, case_timeout_seconds=30, parallel_cases=2), **changes)


def completion(body, *, actions=()):
    if not actions:
        actions = [("submit_solution", {"outcome": "solved", "answer": "2"})]
    message = {"content": body, "tool_calls": [
        {"id": str(uuid4()), "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}
        for name, arguments in actions]}
    return httpx.Response(200, json={"id": "synthetic-receipt", "usage": {"completion_tokens": 12},
        "choices": [{"message": message, "finish_reason": "tool_calls"}]})


class ScriptedInference:
    def __init__(self, *, failed_case=None):
        self.failed_case = failed_case
        self.dispatches = []

    def factory(self, case):
        async def handle(request):
            payload = json.loads(request.content)
            serialized = json.dumps(payload, ensure_ascii=False)
            own = case["id"].split("-")[-1]
            assert f"ONLY_CASE_{own}" in serialized
            assert all(f"ONLY_CASE_{other}" not in serialized for other in range(4) if str(other) != own)
            assert ANSWER_SENTINEL not in serialized and SECRET not in serialized
            assert request.url == httpx.URL("https://api.deepseek.com/chat/completions")
            assert "tools" in payload and "response_format" not in payload
            peer = "你是研究同伴" in serialized
            startup = not peer and any("当前任务：\n理解题目的条件与目标" in message.get("content", "")
                                      for message in payload["messages"])
            if startup:
                assert payload["thinking"] == {"type": "disabled"}
                assert "reasoning_effort" not in payload and payload["max_tokens"] <= 4096
                assert payload["tool_choice"] == "required"
            else:
                assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "max"
                assert "tool_choice" not in payload
            self.dispatches.append((case["id"], "peer" if peer else "lead"))
            await asyncio.sleep(0.08)
            if case["id"] == self.failed_case:
                return httpx.Response(400, json={"error": {"message": "Synthetic rejected request"}})
            if peer:
                assert not any(message["role"] == "assistant" for message in payload["messages"])
                return completion("Independent arithmetic: one plus one is two.", actions=[
                    ("finish_work", {"summary": "Independent arithmetic: one plus one is two."})])
            if not any(message["role"] == "assistant" for message in payload["messages"]):
                return completion("Ask for independent arithmetic and wait for its result.", actions=[
                    ("assign_work", {"member": "peer", "goal": "Compute one plus one independently."}),
                    ("send_message", {"recipient": "peer", "topic": "Arithmetic",
                                      "body": "Compare derivations after your independent attempt.", "wait": True})])
            assert "Independent arithmetic" in serialized
            return completion(r"Adding one and one gives $\boxed{2}$.")
        return httpx.MockTransport(handle)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_unknown_retry_completes_explicit_submission_with_unresolved_cost(workspace, tmp_path):
    problems, source, cases = workspace
    output = tmp_path / "unknown-retry"
    inference = ScriptedInference()
    interrupted = []

    def factory(case):
        inner = inference.factory(case)

        async def handle(request):
            if not interrupted:
                interrupted.append(case["id"])
                raise httpx.ReadError("Synthetic transport interruption", request=request)
            return await inner.handle_async_request(request)

        return httpx.MockTransport(handle)

    frozen_limits = limits(unknown_recovery="once", parallel_cases=1)
    report = asyncio.run(answerbench.run_batch(problems, output, CONFIG, frozen_limits, source,
        case_ids=[cases[0]["id"]], transport_factory=factory))
    assert report["all_completed"] and report["all_unattended"] and report["source_unchanged"]
    case_report = read_json(output / cases[0]["id"] / "report.json")
    assert case_report["workflow_completed"] and not case_report["finalized_after_review"]
    assert case_report["financial_reconciliation_pending"] is True
    assert case_report["unknown_retries_authorized"] == 1
    assert case_report["budget"]["unknown"] == 1 and case_report["budget"]["spent"] == 3
    assert case_report["budget"]["occupied"] == 4 and case_report["budget"]["remaining"] == 1
    assert case_report["network_dispatches"] == 4 and len(inference.dispatches) == 3
    with pytest.raises(ValueError, match="exact frozen"):
        asyncio.run(answerbench.run_batch(problems, output, CONFIG, limits(), source,
            resume=True, case_ids=[cases[0]["id"]], transport_factory=factory))


def test_no_discussion_ablation_emits_explicit_submission_and_frozen_choice(workspace, tmp_path):
    problems, source, cases = workspace
    output = tmp_path / "no-discussion"

    def factory(_case):
        async def handle(request):
            payload = json.loads(request.content)
            names = [tool["function"]["name"] for tool in payload["tools"]]
            assert "send_message" not in names
            assign = next(tool["function"] for tool in payload["tools"] if tool["function"]["name"] == "assign_work")
            assert assign["parameters"]["properties"]["member"]["enum"] == ["self"]
            assert SECRET not in json.dumps(payload)
            return completion(r"$1+1=2$, $\boxed{2}$.")
        return httpx.MockTransport(handle)

    configured = limits(discussion=False, parallel_cases=1)
    result = asyncio.run(answerbench.run_batch(problems, output, CONFIG, configured, source,
        case_ids=[cases[0]["id"]], transport_factory=factory))
    assert result["all_completed"]
    report = read_json(output / cases[0]["id"] / "report.json")
    assert report["research_state"]["session"]["state"] == "completed"
    assert len(report["research_state"]["members"]) == 1
    assert report.get("search_state") is None
    assert report["final_answer"] == "2" and report["discussion_enabled"] is False
    assert report["answer_submission"]["selection_rule"] == "explicit-root-research-submission-v1"
    assert read_json(output / "plan.json")["configuration"]["limits"]["discussion"] is False


def test_four_cases_two_real_api_processes_discussion_export_and_frozen_resume(workspace, tmp_path):
    problems, source, cases = workspace
    output = tmp_path / "normal-batch"
    inference = ScriptedInference()
    active, peak = 0, 0

    async def scenario():
        first_pair = asyncio.Event()

        @asynccontextmanager
        async def observed_api(directory, config):
            nonlocal active, peak
            async with answerbench.local_api(directory, config) as connection:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    first_pair.set()
                try:
                    await asyncio.wait_for(first_pair.wait(), 15)
                    yield connection
                finally:
                    active -= 1

        result = await answerbench.run_batch(problems, output, CONFIG, limits(), source,
            api_factory=observed_api, transport_factory=inference.factory)
        assert result["all_completed"] is True, result
        assert result["answer_key_loaded"] is False and result["source_unchanged"] is True
        assert peak == 2 and active == 0
        assert len(inference.dispatches) == 12

        @asynccontextmanager
        async def never_start_api(*_):
            pytest.fail("Completed resume must not even restart an API or worker")
            yield

        resumed = await answerbench.run_batch(problems, output, CONFIG, limits(), source,
            resume=True, api_factory=never_start_api, transport_factory=inference.factory)
        assert resumed["all_completed"] is True and len(inference.dispatches) == 12
        for config, frozen_limits in [(replace(CONFIG, model="different-model"), limits()),
                                      (CONFIG, limits(reasoning_effort="high"))]:
            with pytest.raises(ValueError, match="exact frozen"):
                await answerbench.run_batch(problems, output, config, frozen_limits, source, resume=True,
                    api_factory=never_start_api, transport_factory=inference.factory)
        changed_problems = tmp_path / "changed-problems.json"
        changed_problems.write_text(problems.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="exact frozen"):
            await answerbench.run_batch(changed_problems, output, CONFIG, limits(), source, resume=True)
        (source / "scripts/imo_answerbench.py").write_text("# changed source\n", encoding="utf-8")
        with pytest.raises(ValueError, match="exact frozen"):
            await answerbench.run_batch(problems, output, CONFIG, limits(), source, resume=True)

    asyncio.run(scenario())
    for case in cases:
        directory = output / case["id"]
        report = read_json(directory / "report.json")
        assert report["completed"] and report["final_answer"] == "2"
        assert report["requests"] == 3 and report["independent_reviews"] == 0
        assert len(report["calls"]) == 3 and len(report["steps"]) == 2
        assert report["human_interventions"] == 0 and not report["model_web_tools"]
        parameters = [call["call_config"]["parameters"] for call in report["calls"]]
        assert sum(p["thinking"] == {"type": "enabled"} for p in parameters) == 1
        assert sum(p["thinking"] == {"type": "disabled"} for p in parameters) == 2
        assert all(p["reasoning_effort"] == "max" for p in parameters if p["thinking"]["type"] == "enabled")
        assert len(list(directory.glob("dispatch-*.json"))) == 3
        assert (directory / "api.log").exists()
        with sqlite3.connect(directory / "research.sqlite3") as database:
            assert database.execute("SELECT title FROM projects").fetchall() == [(case["id"],)]
        with zipfile.ZipFile(directory / "research-export.zip") as archive:
            manifest = archive.read("manifest.json").decode()
            assert ANSWER_SENTINEL not in manifest and SECRET not in manifest
            assert "provider_calls" in manifest
        for dispatch in directory.glob("dispatch-*.json"):
            assert SECRET not in dispatch.read_text(encoding="utf-8")


def test_one_rejected_case_does_not_stop_the_other_three(workspace, tmp_path):
    problems, source, _ = workspace
    output = tmp_path / "failure-batch"
    inference = ScriptedInference(failed_case="case-1")
    result = asyncio.run(answerbench.run_batch(problems, output, CONFIG, limits(), source,
        transport_factory=inference.factory))
    completed = {case["case_id"]: case["completed"] for case in result["cases"]}
    assert completed == {"case-0": True, "case-1": False, "case-2": True, "case-3": True}
    failure = read_json(output / "case-1/report.json")
    assert failure["state"] == "failed" and failure["final_answer"] is None
    assert len(failure["calls"]) == 1 and failure["calls"][0]["complete"] is False
    assert len(inference.dispatches) == 10
    assert all((output / f"case-{index}/research-export.zip").exists() for index in range(4))


def test_resume_after_export_failure_preserves_paid_results_and_dispatch_total(workspace, tmp_path):
    problems, source, cases = workspace
    data = read_json(problems)
    data["problems"] = cases[:1]
    problems.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "export-recovery-batch"
    inference = ScriptedInference()
    frozen_limits = limits(parallel_cases=1)

    @asynccontextmanager
    async def export_fails(directory, config):
        async with answerbench.local_api(directory, config) as (client, human):
            async def fail_download(response):
                if response.request.url.path.endswith("/download"):
                    raise httpx.ReadError("Synthetic lost export response")
            client.event_hooks["response"].append(fail_download)
            yield client, human

    async def scenario():
        interrupted = await answerbench.run_batch(problems, output, CONFIG, frozen_limits, source,
            api_factory=export_fails, transport_factory=inference.factory)
        assert interrupted["all_completed"] is False
        partial = read_json(output / "case-0/report.json")
        assert partial["state"] == "runner_error" and partial["previous_state"] == "completed"
        assert not partial.get("finished_at") and partial["retry_requires_resume"] is True
        assert len(partial["calls"]) == 3 and len(partial["steps"]) == 2
        assert partial["final_answer"] == "2"
        assert len(inference.dispatches) == 3
        resumed = await answerbench.run_batch(problems, output, CONFIG, frozen_limits, source,
            resume=True, transport_factory=inference.factory)
        assert resumed["all_completed"] is True
        assert len(inference.dispatches) == 3
        report = read_json(output / "case-0/report.json")
        assert report["requests"] == report["network_dispatches"] == 3
        assert report["finished_at"] and report["final_answer"] == "2"
        assert [call["request_id"] for call in report["calls"]] == [call["request_id"] for call in partial["calls"]]
        assert (output / "case-0/research-export.zip").exists()

    asyncio.run(scenario())


def test_case_deadline_preserves_prior_step_and_unknown_request_without_redispatch(workspace, tmp_path):
    problems, source, cases = workspace
    data = read_json(problems)
    data["problems"] = cases[:1]
    problems.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "deadline-batch"
    dispatched, cancelled = 0, False

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal cancelled
            try:
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                await asyncio.Event().wait()
            finally:
                cancelled = True

    def factory(case):
        async def handle(request):
            nonlocal dispatched
            dispatched += 1
            if dispatched == 1:
                return completion("A durable candidate step before the deadline.", actions=[("read_material", {"ref": "original"})])
            return httpx.Response(200, stream=WaitingStream(), headers={"content-type": "text/event-stream"})
        return httpx.MockTransport(handle)

    frozen_limits = limits(case_timeout_seconds=12, request_timeout_seconds=30, parallel_cases=1)

    async def scenario():
        result = await answerbench.run_batch(problems, output, CONFIG, frozen_limits, source,
            transport_factory=factory)
        assert result["all_completed"] is False
        report = read_json(output / "case-0/report.json")
        assert report["terminal_reason"] == "case_timeout", report
        assert report["completed"] is False and report["final_answer"] is None
        assert report["final_body"] is None  # Earlier progress is not a final submission.
        assert report["steps"][0]["body"] == "A durable candidate step before the deadline."
        assert len(report["steps"]) == 1 and len(report["calls"]) == 2
        assert report["budget"]["spent"] == 1 and report["budget"]["unknown"] == 1
        assert report["interventions"][0]["actor"] == "benchmark_scheduler"
        assert (output / "case-0/research-export.zip").exists()
        await answerbench.run_batch(problems, output, CONFIG, frozen_limits, source, resume=True,
            transport_factory=factory)
        assert dispatched == 2

    asyncio.run(scenario())
    assert cancelled and dispatched == 2


def test_expired_checkpoint_cannot_start_a_new_paid_dispatch(workspace, tmp_path):
    _, _, cases = workspace
    directory = tmp_path / "expired-case"
    directory.mkdir()
    answerbench.write_json(directory / "checkpoint.json", {
        "started_at": answerbench.stamp(), "deadline_epoch": time.time() - 1,
    })
    dispatched = []

    def factory(case):
        def handle(request):
            dispatched.append(request)
            return completion(r"$\boxed{2}$")
        return httpx.MockTransport(handle)

    report = asyncio.run(answerbench.run_case(cases[0], directory, CONFIG, limits(),
        "expired-checkpoint-test", transport_factory=factory))
    assert not dispatched
    assert report["completed"] is False and report["final_answer"] is None
    assert report["terminal_reason"] == "case_timeout"


def test_explicit_target_only_dispatches_selected_case(workspace, tmp_path):
    problems, source, _ = workspace
    output = tmp_path / "targeted"
    inference = ScriptedInference()
    result = asyncio.run(answerbench.run_batch(problems, output, CONFIG, limits(), source,
        case_ids=["case-3"], transport_factory=inference.factory))
    assert result["all_completed"]
    assert set(case for case, _ in inference.dispatches) == {"case-3"}
    plan = read_json(output / "plan.json")
    assert plan["max_requests_total"] == 5
    assert plan["configuration"]["scope"] == "targeted_retest"
    assert [case["id"] for case in read_json(output / "problem-only.json")["problems"]] == ["case-3"]
    for ids in (["absent"], ["case-3", "case-3"], []):
        with pytest.raises(ValueError, match="case IDs"):
            asyncio.run(answerbench.run_batch(problems, tmp_path / "invalid", CONFIG, limits(), source, case_ids=ids))


def test_intervention_audit_excludes_only_receipted_scheduler_controls():
    receipt = {"affected_runs": [{"intervention_id": "automatic"}]}
    events = [{"id": identifier, "type": "run.intervention", "payload": {"action": "pause"}}
              for identifier in ("automatic", "manual")]
    audit = answerbench.intervention_audit(events, receipt)
    assert audit["human_interventions"] == 1 and not audit["unattended_eligible"]
    assert [event["actor"] for event in audit["interventions"]] == ["benchmark_scheduler", "human"]


def test_search_route_controls_disqualify_unattended_comparison():
    events = [
        {"id": "dispatch", "type": "search.decision", "payload": {"action": "advance"}},
        {"id": "priority", "type": "search.decision", "payload": {
            "action": "human_intervention", "details": {"action": "priority", "priority": 2}}},
    ]
    audit = answerbench.intervention_audit(events)
    assert audit["human_interventions"] == 1 and not audit["unattended_eligible"]
    assert audit["interventions"][0]["event_id"] == "priority"
