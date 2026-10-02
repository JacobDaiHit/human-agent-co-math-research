"""End-to-end engineering tests, not claims about mathematical ability."""

import asyncio
import json
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app
from mathagent.exports.service import ExportService
from mathagent.persistence.models import Attempt, Project, Revision, RevisionParent, Run
from mathagent.persistence.solver_models import ResearchMember, ResearchSession
from mathagent.providers.remote import DeepSeekProvider, ProviderConfig, ProviderFailure
from mathagent.runtime.service import Runtime
from mathagent.runtime.worker import HTTPWorker
from mathagent.tools.code_sandbox import CodeSandbox
from sqlalchemy import select


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "research.sqlite3", token="human", worker_token="worker")
    app.state.database.migrate()
    yield app
    app.state.database.close()


def output(body, *actions):
    message = {"role": "assistant", "content": body}
    if actions:
        message["tool_calls"] = [{"id": str(uuid4()), "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            for name, arguments in actions]
    return {"result": {"message": message}, "usage": {"completion_tokens": 8}}


async def write(client, path, payload):
    response = await client.post(path, json=payload, headers={
        "Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
    assert response.is_success, response.text
    return response.json()


async def read(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer human"})
    assert response.is_success, response.text
    return response.json()


async def setup(client, **options):
    project = await write(client, "/projects", {"title": "连续研究工程夹具", "body": "研究给定数学问题。"})
    run = await write(client, "/runs", {"branch_id": project["branch_id"],
        "goal_object_id": project["object_id"], "autonomous": True,
        "solver_controller": "continuous_research", "request_budget": 16,
        "max_steps": 1, **options})
    return project, run


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer worker"}) as client:
            await scenario(client)
    asyncio.run(run())


def test_dependent_computations_use_one_attempt_and_submit_without_review(app, monkeypatch):
    attempts, code = [], []

    def computation(self, project_id, execution_id, source, timeout, **options):
        code.append(source)
        return {"ok": True, "stdout": "2" if len(code) == 1 else "5"}

    monkeypatch.setattr(CodeSandbox, "execute", computation)

    class Model:
        async def generate(self, task):
            attempts.append(task["attempt_id"])
            replies = [json.loads(message["content"]) for message in task["conversation"]
                       if message["role"] == "tool"]
            if len(attempts) == 1:
                return output("先做第一项计算。", ("compute", {"code": "print(1 + 1)"}))
            if len(attempts) == 2:
                assert replies[-1]["stdout"] == "2"
                return output("利用返回值继续计算。", ("compute", {"code": "print(2 + 3)"}))
            assert replies[-1]["stdout"] == "5"
            return output(r"工程夹具正文：证明没有缺口。展示 $\boxed{2}$ 与 $\boxed{5}$。",
                ("submit_solution", {"outcome": "solved", "answer": "5"}))

    async def scenario(client):
        project, run = await setup(client, completion_policy="reviewed_answer",
                                  answer_requires_exhaustiveness=True)
        with app.state.database.sessions.begin() as session:
            row = session.get(Project, project["project_id"])
            row.policies = {**row.policies, "agent_operations": ["run_code"],
                            "code_sandbox": {"enabled": True, "image_id": "fixture"}}
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "completed", state
        assert state["session"]["answer"] == "5"
        assert len(attempts) == 3 and len(set(attempts)) == 1
        assert code == ["print(1 + 1)", "print(2 + 3)"]
        assert (await read(client, f"/runs/{run['run_id']}/budget"))["spent"] == 3
        snapshot = await read(client, f"/projects/{project['project_id']}/snapshot")
        assert snapshot["reviews"] == [] and len(snapshot["runs"]) == 1
    exercise(app, scenario)


async def worker_write(client, path, payload):
    response = await client.post(path, json=payload, headers={
        "Authorization": "Bearer worker", "Idempotency-Key": str(uuid4())})
    assert response.is_success, response.text
    return response.json()


def test_server_restart_recovers_a_saved_response_and_its_pending_tool(app):
    calls = 0

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            visible = json.dumps(task["conversation"], ensure_ascii=False)
            assert "SAVED_BEFORE_RESTART" in visible
            assert any(message["role"] == "tool" for message in task["conversation"])
            return output("恢复后继续研究并明确提交。", ("submit_solution", {"outcome": "solved", "answer": "restored"}))

    async def scenario(client):
        _, run = await setup(client)
        task = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        execution = {"token": task["token"]}
        reservation = await worker_write(client, f"/attempts/{task['attempt_id']}/requests", {
            **execution, "requested_output_tokens": 256})
        path = f"/requests/{reservation['request_id']}"
        await worker_write(client, path + "/start", execution)
        saved = output("SAVED_BEFORE_RESTART", ("read_material", {"ref": "original"}))
        await worker_write(client, path + "/observation", {**execution,
            "observation": {"raw_text": json.dumps(saved["result"]), "complete": True,
                            "finish_reason": "tool_calls", "usage": saved["usage"]},
            "result": saved["result"]})
        with app.state.database.sessions.begin() as session:
            session.get(Attempt, task["attempt_id"]).lease_until = (datetime.now(UTC)-timedelta(seconds=5)).isoformat()
        restarted = create_app(app.state.database.path, token="human", worker_token="worker")
        restarted.state.database.migrate()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),
                base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer worker"}) as other:
                resumed = (await worker_write(other, "/worker/claim-next", {"providers": ["fake"]}))["task"]
                assert resumed["attempt_id"] != task["attempt_id"]
                await HTTPWorker(other, provider_factory=lambda _: Model(), fake_delay_seconds=0).execute(resumed)
                state = await read(other, f"/runs/{run['run_id']}/research")
                assert state["session"]["answer"] == "restored" and calls == 1
                assert (await read(other, f"/runs/{run['run_id']}/budget"))["spent"] == 2
        finally:
            restarted.state.database.close()
    exercise(app, scenario)


def test_export_freezes_notes_tasks_messages_and_the_explicit_solution(app):
    class Model:
        async def generate(self, task):
            if len(task["conversation"]) == 1:
                return output("第一份普通推导。",
                    ("save_note", {"scope": "personal", "body": "个人研究工作稿。"}),
                    ("save_note", {"scope": "shared", "body": "共享推导与当前困难。"}))
            return output("完整普通正文。", ("submit_solution", {"outcome": "solved", "answer": "explicit"}))

    async def scenario(client):
        project, run = await setup(client)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        exported = await write(client, "/exports", {"project_id": project["project_id"], "branch_id": project["branch_id"]})
        bundle = ExportService(app.state.service).get(exported["export_id"])
        saved = bundle["snapshot"]["research_state"]["sessions"][0]
        assert saved["session"]["answer"] == "explicit"
        assert saved["shared_note"]["body"] == "共享推导与当前困难。"
        assert saved["members"][0]["personal_note"]["body"] == "个人研究工作稿。"
        assert saved["work"][0]["state"] == "completed"
        assert saved["solution"]["id"] in {revision["id"] for revision in bundle["revisions"]}
        assert not bundle["is_truth_verdict"]
    exercise(app, scenario)


def test_plain_prose_continues_research_without_a_copy_only_finalizer(app):
    calls = 0

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            if calls == 2:
                visible = json.dumps(task["conversation"], ensure_ascii=False)
                assert "尚未明确提交" in visible
                return output("新的推导解决了剩余问题。", ("submit_solution", {"outcome": "solved", "answer": "fixture"}))
            return output("一个局部任务的研究结果，尚未明确提交原题解答。")

    async def scenario(client):
        _, run = await setup(client)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert calls == 2 and len(state["work"]) == 1
        assert state["members"][0]["state"] == "completed"
        assert state["solution"]["body"] == "新的推导解决了剩余问题。"
        assert state["session"]["answer"] == "fixture"
    exercise(app, scenario)


def test_local_task_changes_direction_and_notes_have_immutable_history(app):
    calls = 0
    opening = None
    dialogue = None

    class Model:
        async def generate(self, task):
            nonlocal calls, opening, dialogue
            calls += 1
            if calls == 1:
                opening, dialogue = task["conversation"][0], task["research_dialogue_id"]
                return output("记录目前已得到的整除关系。",
                    ("save_note", {"scope": "personal", "body": "已得到整除关系，下一步研究变量范围。"}),
                    ("assign_work", {"member": "self", "goal": "利用整除关系研究变量范围。"}))
            if calls == 2:
                assert task["conversation"][0] == opening and task["research_dialogue_id"] == dialogue
                visible = json.dumps(task["conversation"], ensure_ascii=False)
                assert "当前任务更新：\\n利用整除关系研究变量范围。" in visible
                assert "已得到整除关系" in visible
                return output("局部研究得到一个范围。",
                    ("save_note", {"scope": "personal", "body": "保留整除关系；已得到变量范围，准备组合推导。"}))
            return output("工程夹具完整正文。", ("submit_solution", {"outcome": "solved", "answer": "fixture"}))

    async def scenario(client):
        _, run = await setup(client)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "completed", state
        assert len(state["work"]) == 2
        assert {work["state"] for work in state["work"]} == {"replaced", "completed"}
        with app.state.database.sessions() as session:
            member = session.get(ResearchMember, run["run_id"])
            versions = session.scalars(select(Revision).where(
                Revision.object_id == member.personal_note_id).order_by(Revision.created_at)).all()
            assert len(versions) == 2 and versions[0].body != versions[1].body
            assert session.scalar(select(RevisionParent).where(RevisionParent.revision_id == versions[1].id))
    exercise(app, scenario)


def test_full_mathematical_capacity_and_user_settings_apply_from_first_turn(app):
    calls = 0

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            opening = task["conversation"][0]["content"]
            assert "用户的研究要求：\nKeep the given assumptions." in opening
            if calls == 1:
                assert task["max_output_tokens"] == 32768
                assert task["thinking_mode"] == "enabled" and task["reasoning_effort"] == "high"
                assert "当前任务：\n研究原题" in opening
                return output("先研究一个明确的小问题。",
                    ("assign_work", {"member": "self", "goal": "Derive the local relation."}))
            assert task["max_output_tokens"] == 32768
            assert task["thinking_mode"] == "enabled" and task["reasoning_effort"] == "high"
            assert any("当前任务更新：\nDerive the local relation." in message["content"]
                       for message in task["conversation"])
            return output("Synthetic solution.", ("submit_solution", {"outcome": "solved", "answer": "fixture"}))

    async def scenario(client):
        _, run = await setup(client, max_output_tokens=32768, thinking_mode="enabled",
                             reasoning_effort="high", instruction="Keep the given assumptions.")
        task = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        with app.state.database.sessions.begin() as session:
            record = session.get(Run, run["run_id"])
            record.provider = "deepseek"
            full = Runtime(app.state.service).research.enrich(session, task)
            described = DeepSeekProvider(ProviderConfig("deepseek", "synthetic-secret", "fixture",
                                                        "https://fixture.invalid", True)).describe(full)
            assert described["parameters"]["thinking"] == {"type": "enabled"}
            assert described["parameters"]["reasoning_effort"] == "high"
            assert described["parameters"]["max_tokens"] == 32768
            assert "tool_choice" not in described["parameters"]
            record.provider = "fake"
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).execute_research(task)
        assert calls == 2
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "completed"
        settings = await read(client, f"/runs/{run['run_id']}/options")
        assert settings["thinking_mode"] == "enabled" and settings["reasoning_effort"] == "high"
    exercise(app, scenario)


def test_model_compacts_its_own_context_and_can_read_the_full_saved_derivation(app):
    calls = 0
    saved_body = "LONG_DERIVATION_SENTINEL " + "x = x + 0. " * 5000

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            visible = json.dumps(task["conversation"], ensure_ascii=False)
            if calls == 1:
                return output(saved_body)
            if calls == 2:
                assert "LONG_DERIVATION_SENTINEL" in visible
                return output("重整工作稿后继续。", ("compact_context", {
                    "summary": "COMPACT_NOTE: 已得到恒等关系；仍需解决剩余条件。"}))
            if calls == 3:
                assert "COMPACT_NOTE" in visible and "LONG_DERIVATION_SENTINEL" not in visible
                assert len(task["conversation"]) == 1
                return output("找回长推导。", ("list_materials", {"query": "LONG_DERIVATION_SENTINEL"}))
            replies = [json.loads(item["content"]) for item in task["conversation"] if item["role"] == "tool"]
            if calls == 4:
                ref = replies[-1]["materials"][0]["ref"]
                assert ref.startswith("revision:")
                return output("读取完整来源。", ("read_material", {"ref": ref}))
            assert replies[-1]["body"] == saved_body[:50000]
            assert replies[-1]["next_offset"] == 50000
            return output("新的推导已组合成解答。", ("submit_solution", {"outcome": "solved"}))

    async def scenario(client):
        _, run = await setup(client)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert calls == 5 and state["session"]["state"] == "completed"
        with app.state.database.sessions() as session:
            assert session.scalar(select(Revision).where(Revision.body == saved_body))
        assert len(state["work"]) == 2
    exercise(app, scenario)


def test_later_research_reuses_notes_and_original_material_without_cross_problem_leakage(app):
    calls = 0

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            if calls == 1:
                return output("先前进展。",
                    ("save_note", {"scope": "personal", "body": "PERSISTENT_PERSONAL_NOTE"}),
                    ("save_note", {"scope": "shared", "body": "PERSISTENT_SHARED_NOTE"}),
                    ("save_note", {"scope": "material", "body": "FULL_OLD_DERIVATION", "title": "旧推导"}),
                    ("submit_solution", {"outcome": "unresolved"}))
            visible = json.dumps(task["conversation"], ensure_ascii=False)
            if calls == 2:
                assert "PERSISTENT_PERSONAL_NOTE" in visible and "PERSISTENT_SHARED_NOTE" in visible
                assert "UNRELATED_PROBLEM_SECRET" not in visible
                return output("查找先前原始材料。", ("list_materials", {"query": "FULL_OLD_DERIVATION"}))
            replies = [json.loads(item["content"]) for item in task["conversation"] if item["role"] == "tool"]
            if calls == 3:
                assert replies[-1]["materials"][0]["previous_research"] is True
                return output("读取旧推导。", ("read_material", {"ref": replies[-1]["materials"][0]["ref"]}))
            assert replies[-1]["body"] == "FULL_OLD_DERIVATION"
            return output("接续得到新推导。", ("submit_solution", {"outcome": "solved"}))

    async def scenario(client):
        project, _ = await setup(client)
        worker = HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0)
        await worker.run(once=True)
        await write(client, "/projects", {"title": "另一道题", "body": "UNRELATED_PROBLEM_SECRET"})
        run = await write(client, "/runs", {"branch_id": project["branch_id"],
            "goal_object_id": project["object_id"], "autonomous": True, "request_budget": 12})
        await worker.run(once=True)
        assert calls == 4
        assert (await read(client, f"/runs/{run['run_id']}/research"))["session"]["state"] == "completed"
    exercise(app, scenario)


def test_reasoning_only_length_stop_becomes_visible_research_not_ignored_hidden_state(app):
    calls = 0
    fragment = "PAID_MATHEMATICAL_PROGRESS " + "推导细节。" * 60000

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderFailure("incomplete_output", outcome="spent", observation={
                    "finish_reason": "length", "raw_text_truncated": False,
                    "raw_text": json.dumps({"content": "", "reasoning_content": fragment}, ensure_ascii=False),
                    "usage": {"completion_tokens": 32}})
            assert any(item["role"] == "assistant" and fragment == item.get("reasoning_content")
                       for item in task["conversation"])
            return output("利用已付费的推导继续。", ("submit_solution", {"outcome": "unresolved"}))

    async def scenario(client):
        _, run = await setup(client)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert calls == 2 and state["session"]["outcome"] == "unresolved"
        assert state["output_budget"]["reported_output_tokens"] == 40
    exercise(app, scenario)


def test_independent_peer_then_two_way_topic_discussion_without_consensus_gate(app):
    calls = defaultdict(int)
    member_ids = defaultdict(set)

    class Model:
        async def generate(self, task):
            member = task["research_member"]
            calls[member] += 1
            member_ids[member].add(task["run_id"])
            number = calls[member]
            visible = json.dumps(task["conversation"], ensure_ascii=False)
            if member == "peer":
                if number == 1:
                    assert "LEAD_SECRET_CONCLUSION" not in visible
                    assert "先形成自己的推导" in visible
                    await asyncio.sleep(0.03)
                    return output("PEER_INDEPENDENT_ARGUMENT：不同方法得到的局部推导。",
                        ("save_note", {"scope": "personal", "body": "PEER_OWN_NOTE：先前的独立尝试。"}),
                        ("finish_work", {}))
                assert "PEER_OWN_NOTE" in visible
                if "请解释你得到范围的关键一步" not in visible:
                    # Four execution slots allow this reply before the lead's
                    # follow-up arrives. Discussion must not depend on timing.
                    return output("独立推导已完成，等待具体的问题。", ("send_message", {
                        "recipient": "lead", "topic": "变量范围", "body": "可以讨论具体推导。", "wait": True}))
                return output("PEER_NEW_DERIVATION：补出了关键一步，另一个问题仍有分歧。",
                    ("send_message", {"recipient": "lead", "topic": "变量范围",
                                      "body": "PEER_REPLY：具体的新推导，而非同意票。"}),
                    ("finish_work", {}))
            if number == 1:
                return output("LEAD_SECRET_CONCLUSION：主研究者自己的初步判断。",
                    ("save_note", {"scope": "shared", "body": "LEAD_SECRET_CONCLUSION"}),
                    ("assign_work", {"member": "peer", "goal": "独立从原题研究变量范围。",
                                     "independent": True, "materials": []}))
            if number == 2:
                return output("请求同伴研究后交流。", ("send_message", {
                    "recipient": "peer", "topic": "变量范围", "body": "LEAD_SECRET_CONCLUSION", "wait": True}))
            if "PEER_REPLY" not in visible:
                assert "PEER_INDEPENDENT_ARGUMENT" in visible
                return output("回应同伴的不同推导。", ("send_message", {
                    "recipient": "peer", "topic": "变量范围", "body": "请解释你得到范围的关键一步。", "wait": True}))
            return output("利用已有推导形成解答，另一个意见不同不构成交付审批条件。",
                          ("submit_solution", {"outcome": "solved", "answer": "fixture"}))

    async def scenario(client):
        _, run = await setup(client)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "completed", state
        assert len(state["members"]) == 2
        assert all(len(ids) == 1 for ids in member_ids.values())
        assert calls["lead"] >= 4 and calls["peer"] >= 2
        assert len(state["messages"]) >= 4
        assert {message["topic"] for message in state["messages"]} >= {"变量范围"}
    exercise(app, scenario)


def test_pause_preserves_paid_turn_and_resume_does_not_buy_it_again(app):
    calls = 0

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            await write(client_ref[0], f"/runs/{task['run_id']}/interventions", {"action": "pause"})
            return output("在途模型回答仍然保存为普通材料。",
                          ("submit_solution", {"outcome": "solved", "answer": "saved"}))

    client_ref = []

    async def scenario(client):
        client_ref.append(client)
        _, run = await setup(client)
        worker = HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0)
        await worker.run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "paused" and state["solution"] is None
        assert (await read(client, f"/runs/{run['run_id']}/steps"))["steps"][0]["body"]
        await write(client, f"/runs/{run['run_id']}/resume", {})
        await worker.run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["answer"] == "saved" and calls == 1
        assert (await read(client, f"/runs/{run['run_id']}/budget"))["spent"] == 1
    exercise(app, scenario)


def test_root_budget_stops_both_members_without_promoting_a_guess(app):
    calls = []

    class Model:
        async def generate(self, task):
            calls.append(task["research_member"])
            if task["research_member"] == "lead":
                return output(r"只是早期猜测 $\boxed{999}$，没有提交。",
                    ("assign_work", {"member": "peer", "goal": "独立寻找一个不同方法。"}),
                    ("send_message", {"recipient": "peer", "topic": "不同方法", "body": "完成后交换推导。", "wait": True}))
            return output("同伴留下了局部推导。", ("finish_work", {}))

    async def scenario(client):
        _, run = await setup(client, request_budget=2)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "budget_exhausted"
        assert state["session"]["answer"] is None and state["solution"] is None
        assert len(calls) == 2
        assert (await read(client, f"/runs/{run['run_id']}/budget"))["spent"] == 2
        assert all(member["state"] != "queued" for member in state["members"])
    exercise(app, scenario)


def test_computation_does_not_hold_the_database_write_lock(app, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = 0

    def computation(self, project_id, execution_id, source, timeout, **options):
        started.set()
        assert release.wait(5), "A separate database write was blocked by computation"
        return {"ok": True, "stdout": "2"}

    monkeypatch.setattr(CodeSandbox, "execute", computation)

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            if calls == 1:
                return output("请求计算。", ("compute", {"code": "print(1+1)"}))
            return output("利用计算结果形成正文。", ("submit_solution", {"outcome": "solved", "answer": "2"}))

    async def scenario(client):
        project, run = await setup(client)
        with app.state.database.sessions.begin() as session:
            row = session.get(Project, project["project_id"])
            row.policies = {"agent_operations": ["run_code"], "code_sandbox": {"enabled": True, "image_id": "fixture"}}
        worker = asyncio.create_task(HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True))
        assert await asyncio.to_thread(started.wait, 3)
        try:
            await write(client, "/objects", {"branch_id": project["branch_id"], "kind": "artifact", "body": "计算期间写入工作台。"})
        finally:
            release.set()
        await worker
        assert (await read(client, f"/runs/{run['run_id']}/research"))["session"]["answer"] == "2"
    exercise(app, scenario)


def test_returned_model_label_is_saved_without_overwriting_frozen_requested_model(app):
    alias, returned = "synthetic-api-alias", "synthetic-provider-version"

    def transport(request):
        assert json.loads(request.content)["model"] == alias
        return httpx.Response(200, json={"model": returned, "id": "synthetic-model-receipt",
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            "choices": [{"message": output("", ("submit_solution", {
                "outcome": "solved", "body": "Synthetic full proof"}))["result"]["message"],
                "finish_reason": "tool_calls"}]})

    async def scenario(client):
        _, run = await setup(client)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as model_client:
            provider = DeepSeekProvider(ProviderConfig("deepseek", "synthetic-unused", alias,
                "https://mock.invalid", True), model_client)
            await HTTPWorker(client, provider_factory=lambda _: provider, fake_delay_seconds=0).run(once=True)
        calls = (await read(client, f"/runs/{run['run_id']}/calls"))["calls"]
        assert len(calls) == 1
        assert calls[0]["call_config"]["model"] == alias
        assert calls[0]["response_metadata"] == {"model": returned}
        frozen = calls[0]["call_config"]
        with app.state.database.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE provider_calls DROP COLUMN response_metadata")
            connection.exec_driver_sql("UPDATE alembic_version SET version_num='0009_continuous_research'")
        app.state.database.migrate()
        historical = (await read(client, f"/runs/{run['run_id']}/calls"))["calls"][0]
        assert historical["response_metadata"] == {} and historical["call_config"] == frozen
        assert historical["raw_text"] == calls[0]["raw_text"]
    exercise(app, scenario)


def test_worker_notifies_finished_background_calculation_without_paid_polling(app, monkeypatch):
    job_id, polls, calls, attempts = "e" * 64, 0, 0, []

    def computation(self, project_id, execution_id, source, timeout, **options):
        return {"job_id": job_id, "status": "running", "code": source}

    def poll(self, project_id, requested_job):
        nonlocal polls
        polls += 1
        done = polls >= 4
        return {"job_id": job_id, "status": "complete" if done else "running",
                "ok": done, "reason": None, "stdout": "BACKGROUND_RESULT" if done else ""}

    monkeypatch.setattr(CodeSandbox, "execute", computation)
    monkeypatch.setattr(CodeSandbox, "poll", poll)

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            attempts.append(task["attempt_id"])
            if calls == 1:
                return output("Start a computation.", ("compute", {"code": "print(1)"}))
            if calls == 2:
                return output("Let the backend wait.", ("poll_computation", {"job_id": job_id, "wait": True}))
            assert calls == 3 and "BACKGROUND_RESULT" in json.dumps(task["conversation"])
            return output("", ("submit_solution", {"body": "Proof based on the saved computation", "outcome": "solved"}))

    async def scenario(client):
        project, run = await setup(client, discussion=False)
        with app.state.database.sessions.begin() as session:
            row = session.get(Project, project["project_id"])
            row.policies = {**row.policies, "agent_operations": ["run_code"],
                            "code_sandbox": {"enabled": True, "image_id": "fixture"}}
        worker = HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0, poll_seconds=0.05)
        await asyncio.wait_for(worker.run(once=True), timeout=15)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "completed" and calls == 3
        assert attempts[0] == attempts[1] and attempts[2] != attempts[1]
        assert len(state["computations"]) == 1 and state["computations"][0]["status"] == "complete"
    exercise(app, scenario)


def test_late_background_result_cannot_recreate_permanently_deleted_material(app):
    job_id = "d" * 64

    async def scenario(client):
        project, run = await setup(client)
        with app.state.database.sessions.begin() as session:
            root = session.get(ResearchSession, run["run_id"])
            root.config = {**root.config, "computations": {job_id: {"job_id": job_id,
                "run_id": run["run_id"], "project_id": project["project_id"], "image_id": "fixture", "status": "running"}}}
        preview = await read(client, f"/objects/{project['object_id']}/deletion-preview")
        await write(client, f"/objects/{project['object_id']}/permanent-delete", {
            "preview_token": preview["preview_token"], "confirmation": "永久删除"})
        runtime = Runtime(app.state.service)
        _, receipt = app.state.service.execute("research.computation_ready", "synthetic-late-result", {
            "root_id": run["run_id"], "job_id": job_id,
            "result": {"status": "complete", "stdout": "PRIVATE_LATE_COMPUTATION_RESULT"}},
            runtime.research._computation_ready)
        assert receipt == {"saved": False}
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["session"]["state"] == "cancelled" and state["computations"] == []
        with app.state.database.sessions() as session:
            assert not session.scalar(select(Revision.id).where(Revision.body.contains("PRIVATE_LATE_COMPUTATION_RESULT")))
    exercise(app, scenario)
