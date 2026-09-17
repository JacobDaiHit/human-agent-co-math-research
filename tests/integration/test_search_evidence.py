"""Evidence and isolation regressions for bounded search; synthetic provider only."""

import asyncio
from contextlib import aclosing

from mathagent.persistence.models import Run
from mathagent.runtime.worker import HTTPWorker
from test_bounded_search import Solver, op, setup, state
from test_bounded_search import app as app


async def run_until(worker, client, root_id, limit=24):
    for _ in range(limit):
        await worker.run(once=True)
        snapshot = await state(client, root_id)
        if snapshot["session"]["phase"] == "terminated":
            return snapshot
    return await state(client, root_id)


def test_failed_first_route_repair_does_not_block_second_route(app):
    async def scenario():
        client, _, _, root = await setup(app, {"active_routes": 1, "check_requests": 4}, policy="reviewed_answer")
        worker = HTTPWorker(client, provider_factory=lambda _: Solver(bad_first=True, refuse_repair=True),
                            providers=["deepseek"], fake_delay_seconds=0)
        async with aclosing(client):
            snapshot = await run_until(worker, client, root["run_id"])
            assert snapshot["session"]["phase"] == "terminated", snapshot
            first, second = snapshot["routes"]
            assert first["repairs"] == 1 and first["state"] == "paused"
            assert second["candidate_revision_id"] is not None
    asyncio.run(scenario())


class ObligatedCandidate(Solver):
    async def generate(self, task):
        if task["search_context"]["kind"] in {"advance", "repair"}:
            return {"result": {"mode": "research", "body": r"候选 $\boxed{2}$。",
                "findings": ["工程验收。"], "verdict": None, "scope": None,
                "actions": [op("report_progress", status="candidate", open_subgoals=["证明 $a=b$。"],
                               assumptions=["$a>0$"])], "structured_gaps": [],
                "next_action": "finish", "cited_revision_ids": []}, "usage": {"completion_tokens": 100}}
        return await super().generate(task)


def test_candidate_with_open_subgoals_or_assumptions_is_not_complete(app):
    async def scenario():
        client, _, _, root = await setup(app, {"max_routes": 1, "active_routes": 1})
        worker = HTTPWorker(client, provider_factory=lambda _: ObligatedCandidate(), providers=["deepseek"],
                            fake_delay_seconds=0)
        async with aclosing(client):
            for _ in range(5):
                await worker.run(once=True)
            snapshot = await state(client, root["run_id"])
            route = snapshot["routes"][0]
            assert route["progress"]["status"] == "progress"
            assert route["state"] != "needs_check"
            assert snapshot["session"]["phase"] != "final"
    asyncio.run(scenario())


class MismatchedFinal(Solver):
    async def generate(self, task):
        value = await super().generate(task)
        if task["search_context"]["kind"] == "final":
            value["result"]["body"] = r"篡改后的提交 $\boxed{3}$。"
        return value


def test_final_answer_different_from_selected_candidate_is_rejected(app):
    async def scenario():
        client, _, _, root = await setup(app, policy="reviewed_answer")
        worker = HTTPWorker(client, provider_factory=lambda _: MismatchedFinal(), providers=["deepseek"],
                            fake_delay_seconds=0)
        async with aclosing(client):
            await run_until(worker, client, root["run_id"])
            steps = (await client.get(f"/runs/{root['run_id']}/steps", headers={"Authorization": "Bearer human"})).json()["steps"]
            final = steps[-1]
            assert not final["receipt"]["completion_checks"]["passed"]
            assert final["state"] != "completed"
    asyncio.run(scenario())


class ToolEvidenceSolver(Solver):
    def __init__(self):
        super().__init__()
        self.check_inputs = []

    async def generate(self, task):
        kind = task["search_context"]["kind"]
        if kind == "advance":
            return {"result": {"mode": "research", "body": r"候选答案为 $\boxed{2}$。",
                "findings": ["使用局部工具检查。"], "verdict": None, "scope": None,
                "actions": [
                    op("propose_check", target_revision_id=task["target_revision_id"] or task["inputs"][0]["revision_id"],
                       statement=r"$x=x+1$", scope="只检查该多项式恒等式。", tool="calculate",
                       arguments={"tool": "polynomial_identity", "inputs": {"left": "x", "right": "x+1", "variables": ["x"]}}),
                    op("propose_check", target_revision_id=task["target_revision_id"] or task["inputs"][0]["revision_id"],
                       statement="sandbox probe", scope="程序失败不构成数学反例。", tool="run_code",
                       arguments={"code": "raise RuntimeError('synthetic failure')"}),
                    op("report_progress", status="candidate"),
                ], "structured_gaps": [], "next_action": "finish", "cited_revision_ids": []},
                "usage": {"completion_tokens": 100}}
        if kind == "check":
            self.check_inputs = task["inputs"]
        return await super().generate(task)


def test_propose_check_counterexample_and_program_failure_are_local_evidence(app):
    async def scenario():
        client, _, _, root = await setup(app, {"max_routes": 1, "active_routes": 1}, policy="reviewed_answer")
        solver = ToolEvidenceSolver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], fake_delay_seconds=0)
        async with aclosing(client):
            snapshot = await run_until(worker, client, root["run_id"])
            steps = []
            for run_id in {root["run_id"], *(work["run_id"] for work in snapshot["works"])}:
                steps.extend((await client.get(f"/runs/{run_id}/steps", headers={"Authorization": "Bearer human"})).json()["steps"])
            actions = [action for step in steps for action in step["actions"]]
            completed_checks = [action for action in actions if action["type"] == "propose_check" and action["status"] == "completed"]
            assert completed_checks, [(item["type"], item["status"], item.get("error")) for item in actions]
            polynomial = completed_checks[0]
            assert polynomial["result"]["mathematical_correctness_verified"] is False
            assert polynomial["result"]["exact_result"]["identity"] is False
            failed_program = [action for action in actions if action["type"] == "propose_check" and action["status"] != "completed"]
            assert failed_program
            assert solver.check_inputs and any("x+1" in item["body"] for item in solver.check_inputs)
            assert snapshot["session"]["phase"] == "terminated"
    asyncio.run(scenario())


class CounterexampleStallsRepair(Solver):
    def __init__(self):
        super().__init__()
        self.final_body = ""

    async def generate(self, task):
        self.tasks.append(task)
        kind = task["search_context"]["kind"]
        if kind == "advance":
            return {"result": {"mode": "research", "body": r"候选 $\boxed{2}$。", "findings": ["候选。"],
                "verdict": None, "scope": None, "actions": [op("report_progress", status="candidate")],
                "structured_gaps": [], "next_action": "finish", "cited_revision_ids": []}, "usage": {"completion_tokens": 100}}
        if kind == "check":
            return {"result": {"mode": "review", "body": "存在精确反例。", "findings": ["反例。"],
                "verdict": "issues", "scope": "检查候选的关键等式。", "actions": [],
                "structured_gaps": [{"kind": "counterexample", "anchor": "$n=2$", "detail": "候选与反例矛盾。", "evidence_revision_ids": []}],
                "next_action": "finish", "cited_revision_ids": []}, "usage": {"completion_tokens": 100}}
        if kind == "repair":
            return {"result": {"mode": "research", "body": "无法修复该反例。", "findings": ["停滞。"],
                "verdict": None, "scope": None, "actions": [op("report_progress", status="stalled")],
                "structured_gaps": [], "next_action": "finish", "cited_revision_ids": []}, "usage": {"completion_tokens": 100}}
        if kind == "final":
            self.final_body = "存在未修复反例，无法交付答案。"
            return {"result": {"mode": "research", "body": "存在未修复反例，无法交付答案。", "findings": ["弃答。"],
                "verdict": None, "scope": None, "actions": [], "structured_gaps": [], "next_action": "finish", "cited_revision_ids": []}, "usage": {"completion_tokens": 100}}
        return await super().generate(task)


def test_unrepaired_counterexample_terminates_without_paid_final(app):
    async def scenario():
        client, _, _, root = await setup(app, {"max_routes": 1, "active_routes": 1}, policy="draft")
        solver = CounterexampleStallsRepair()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], fake_delay_seconds=0)
        async with aclosing(client):
            snapshot = await run_until(worker, client, root["run_id"])
            assert all(task["search_context"]["kind"] != "final" for task in solver.tasks)
            assert not [work for work in snapshot["works"] if work["kind"] == "final"]
            assert snapshot["gaps"][0]["kind"] == "counterexample"
            assert snapshot["session"]["phase"] == "terminated"
            assert any(decision["details"].get("reason") == "no_deliverable_candidate"
                       for decision in snapshot["decisions"])
            with app.state.database.sessions() as session:
                assert session.get(Run, root["run_id"]).state == "step_limit"
    asyncio.run(scenario())


class NoRoutesSolver(Solver):
    async def generate(self, task):
        self.tasks.append(task)
        assert task["search_context"]["kind"] == "analysis"
        return {"result": {"mode": "research", "body": "未能提出可验证路线。",
            "findings": ["分析未产生路线。"], "verdict": None, "scope": None,
            "actions": [], "structured_gaps": [], "next_action": "finish", "cited_revision_ids": []},
            "usage": {"completion_tokens": 100}}


def test_analysis_without_routes_terminates_after_single_call(app):
    async def scenario():
        client, _, _, root = await setup(app, {"max_routes": 1, "active_routes": 1}, policy="draft")
        solver = NoRoutesSolver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], fake_delay_seconds=0)
        async with aclosing(client):
            snapshot = await run_until(worker, client, root["run_id"])
            assert len(solver.tasks) == 1
            assert solver.tasks[0]["search_context"]["kind"] == "analysis"
            assert snapshot["session"]["phase"] == "terminated"
            assert not [work for work in snapshot["works"] if work["kind"] == "final"]
            assert any(decision["details"].get("reason") == "no_deliverable_candidate"
                       for decision in snapshot["decisions"])
            with app.state.database.sessions() as session:
                assert session.get(Run, root["run_id"]).state == "step_limit"
    asyncio.run(scenario())
