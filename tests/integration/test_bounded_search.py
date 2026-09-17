"""Controller acceptance with input-dependent synthetic inference, never paid API."""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app
from mathagent.runtime.worker import HTTPWorker


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    app = create_app(tmp_path / "search.sqlite3", token="human", worker_token="worker")
    app.state.database.migrate()
    yield app
    app.state.database.close()


def op(kind, **arguments):
    return {"type": kind, "arguments": arguments}


class Solver:
    def __init__(self, *, bad_first=False, refuse_repair=False):
        self.tasks = []
        self.bad_first = bad_first
        self.refuse_repair = refuse_repair

    async def generate(self, task):
        self.tasks.append(task)
        ctx = task["search_context"]
        kind = ctx["kind"]
        body, actions, verdict, scope = "工程测试局部进展。", [], None, None
        cited = []
        gaps = []
        if kind == "analysis":
            actions = [op("propose_routes", routes=[{
                "title": title, "core_reduction": title, "key_lemmas": [title],
                "assumptions": [], "subgoal": "证明局部恒等式。", "cheap_check": "代入边界值。",
            } for title in ["直接展开", "配方构造"]])]
        elif kind in {"advance", "repair"}:
            if kind == "repair":
                assert ctx["repair_contract"]["gaps"]
                assert task["memory_packet"]["entries"]
            wrong = self.bad_first and ctx["route"]["ordinal"] == 0 and kind == "advance"
            body = r"错误步骤 $1+1=3$，候选 $\boxed{3}$。" if wrong else r"计算 $1+1=2$，答案 $\boxed{2}$。"
            if kind == "repair" and self.refuse_repair:
                actions = [op("report_progress", status="stalled")]
            else:
                actions = [op("report_progress", status="candidate")]
        elif kind == "check":
            candidate = next(i for i in task["inputs"] if i["revision_id"] == task["target_revision_id"])
            wrong = "1+1=3" in candidate["body"]
            verdict = "issues" if wrong else "passed"
            scope = "已读取指定候选、前提及所列依赖；这是合成审查测试。"
            body = "候选的算术步骤有误。" if wrong else "指定版本的算术步骤未发现问题。"
            if wrong:
                gaps = [{"kind": "arithmetic", "anchor": "$1+1=3$", "detail": "精确算术不成立。", "evidence_revision_ids": []}]
        elif kind == "final":
            body = r"按选定候选提交 $\boxed{2}$。"
            cited = [ctx["route"]["candidate_revision_id"]] if ctx["route"] else []
        return {"result": {"mode": task["mode"], "body": body,
            "findings": ["仅用于工程验收。"], "verdict": verdict, "scope": scope,
            "actions": actions, "structured_gaps": gaps,
            "next_action": "finish", "cited_revision_ids": cited},
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}}


async def setup(app, config=None, policy="draft"):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000",
                              headers={"Authorization": "Bearer worker"})
    async def write(path, payload, method="POST", worker=False):
        response = await client.request(method, path, json=payload, headers={
            "Authorization": "Bearer worker" if worker else "Bearer human", "Idempotency-Key": str(uuid4())})
        assert response.is_success, response.text
        return response.json()
    p = await write("/projects", {"title": "控制器隔离验收", "body": "计算 $1+1$。"})
    await write(f"/projects/{p['project_id']}/runtime-settings", {
        "allow_real_api": True, "allowed_providers": ["fake", "deepseek"], "request_budget": 100}, "PUT")
    r = await write("/runs", {"branch_id": p["branch_id"], "goal_object_id": p["object_id"],
        "provider": "deepseek", "autonomous": True, "request_budget": 12,
        "max_output_tokens": 4096, "cumulative_output_token_budget": 32768,
        "solver_controller": "bounded_search_v1", "search_config": config or {},
        "completion_policy": policy})
    return client, write, p, r


async def state(client, root_id):
    response = await client.get(f"/runs/{root_id}/search", headers={"Authorization": "Bearer human"})
    assert response.is_success, response.text
    return response.json()


def test_two_routes_review_final_and_frozen_config(app):
    async def scenario():
        client, write, _, root = await setup(app, policy="reviewed_answer")
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"])
        async with aclosing(client):
            for _ in range(16):
                await worker.run(once=True)
                snapshot = await state(client, root["run_id"])
                if snapshot["session"]["phase"] == "terminated":
                    break
            assert snapshot["session"]["phase"] == "terminated", snapshot
            assert len(snapshot["routes"]) == 2
            assert any(t["search_context"]["kind"] == "check" for t in solver.tasks)
            final = solver.tasks[-1]
            assert final["search_context"]["kind"] == "final"
            assert not final["operation_schemas"]
            detail = await client.get(f"/runs/{root['run_id']}/steps", headers={"Authorization": "Bearer human"})
            assert detail.json()["steps"][-1]["receipt"]["completion_checks"]["passed"]
            before = len(solver.tasks)
            await worker.run(once=True)
            assert len(solver.tasks) == before
            frozen = await client.put(f"/runs/{root['run_id']}/options", json={"max_output_tokens": 8000},
                headers={"Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
            assert frozen.status_code == 409
    asyncio.run(scenario())


def test_arithmetic_gap_creates_local_repair_contract(app):
    async def scenario():
        client, _, _, root = await setup(app, {"active_routes": 1, "max_routes": 1})
        solver = Solver(bad_first=True)
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"])
        async with aclosing(client):
            for _ in range(14):
                await worker.run(once=True)
                snapshot = await state(client, root["run_id"])
                if snapshot["session"]["phase"] == "terminated":
                    break
            assert any(t["search_context"]["kind"] == "repair" for t in solver.tasks), snapshot
            assert snapshot["routes"][0]["repairs"] == 1
            assert snapshot["gaps"][0]["kind"] == "arithmetic"
            assert snapshot["gaps"][0]["state"] == "reviewed_repair"
    asyncio.run(scenario())


def test_route_conditions_survive_empty_candidate_report_and_memory(app):
    class ConditionalSolver(Solver):
        async def generate(self, task):
            if task["search_context"]["kind"] == "analysis":
                self.tasks.append(task)
                return {"result": {"mode": "research", "body": "conditional route", "findings": ["fixture"],
                    "verdict": None, "scope": None, "cited_revision_ids": [], "structured_gaps": [],
                    "next_action": "finish", "actions": [op("propose_routes", routes=[{
                        "title": "conditional", "core_reduction": "conditional", "key_lemmas": ["lemma"],
                        "assumptions": ["Assume an additional unproved positivity condition."],
                        "subgoal": "derive under the added condition", "cheap_check": "check boundary",
                    }])]}}
            return await super().generate(task)

    async def scenario():
        client, _, _, root = await setup(app, {"max_routes": 1, "active_routes": 1})
        solver = ConditionalSolver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"])
        async with aclosing(client):
            for _ in range(5):
                await worker.run(once=True)
                snapshot = await state(client, root["run_id"])
                if any(task["search_context"]["kind"] == "advance" for task in solver.tasks):
                    break
            route = snapshot["routes"][0]
            condition = "Assume an additional unproved positivity condition."
            assert route["progress"]["status"] == "progress"
            assert condition in route["progress"]["assumptions"]
            assert any(condition in entry["assumptions"] for entry in snapshot["memory"])

    asyncio.run(scenario())
