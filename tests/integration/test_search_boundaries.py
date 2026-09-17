"""Regression fences for bounded-search controller boundaries."""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest
from mathagent.application.errors import DomainError
from mathagent.persistence.agent_models import AgentRun
from mathagent.persistence.models import Revision, Run
from mathagent.persistence.runtime_models import ProviderRequest
from mathagent.persistence.search_models import SearchMemory, SearchRoute
from mathagent.providers.protocol import AgentAction
from mathagent.runtime.service import Runtime
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_bounded_search import Solver, setup, state
from test_bounded_search import app as bounded_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    yield from bounded_app.__wrapped__(tmp_path, monkeypatch)


def test_controlled_task_exposes_no_legacy_spawn_or_project_search(app):
    async def scenario():
        client, _, _, _ = await setup(app)
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"])
        async with aclosing(client):
            await worker.run(once=True)
            schemas = solver.tasks[0]["operation_schemas"]
            assert not {"spawn_task", "request_review", "create_branch", "discuss", "search_project"} & set(schemas)

    asyncio.run(scenario())


def test_paused_root_cannot_dispatch_queued_route_children(app):
    async def scenario():
        client, _, _, root = await setup(app)
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"])
        async with aclosing(client):
            before = len(solver.tasks)
            paused = await client.post(f"/runs/{root['run_id']}/interventions", json={"action": "pause"}, headers={
                "Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
            assert paused.is_success, paused.text
            for _ in range(3):
                await worker.run(once=True)
            snapshot = await state(client, root["run_id"])
            assert len(solver.tasks) == before
            assert snapshot["session"]["phase"] == "analysis"
            assert all(work["state"] != "running" for work in snapshot["works"])

    asyncio.run(scenario())


def test_legacy_spawn_and_same_project_other_root_read_are_rejected(app):
    async def scenario():
        client, write, project, root = await setup(app)
        other = await write("/runs", {"branch_id": project["branch_id"], "goal_object_id": project["object_id"],
            "provider": "deepseek", "autonomous": True, "request_budget": 12, "max_output_tokens": 4096,
            "cumulative_output_token_budget": 32768, "solver_controller": "bounded_search_v1"})
        async with aclosing(client):
            with app.state.database.sessions.begin() as session:
                runtime = Runtime(app.state.service)
                runtime.claim(session, {"run_id": other["run_id"]})
                other_run = session.get(Run, other["run_id"])
                runtime.agent.execute_action(session, other_run, AgentAction(type="propose_routes", arguments={"routes": [{
                    "title": "private", "core_reduction": "private reduction", "key_lemmas": ["private lemma"],
                    "assumptions": [], "subgoal": "private subgoal", "cheap_check": "private check"}]}))
                private = session.scalar(select(SearchRoute).where(SearchRoute.root_run_id == other["run_id"]))
                runtime.claim(session, {"run_id": root["run_id"]})
                run = session.get(Run, root["run_id"])
                with pytest.raises(DomainError, match="当前派发合同"):
                    runtime.agent.execute_action(session, run, AgentAction(type="spawn_task", arguments={
                        "goal_object_id": project["object_id"], "instruction": "bypass controller"}))
                with pytest.raises(DomainError, match="可用材料"):
                    runtime.agent.execute_action(session, run, AgentAction(type="read_object", arguments={
                        "object_id": session.get(Revision, private.card_revision_id).object_id,
                        "revision_id": private.card_revision_id}))

    asyncio.run(scenario())


def test_original_goal_change_stops_before_any_paid_request(app):
    async def scenario():
        client, _, project, root = await setup(app)
        async with aclosing(client):
            with app.state.database.sessions.begin() as session:
                runtime = Runtime(app.state.service)
                _, task = runtime.claim(session, {"run_id": root["run_id"]})
                app.state.service.revise_object(session, {"branch_id": project["branch_id"],
                    "object_id": project["object_id"], "expected_revision_id": project["revision_id"],
                    "body": "A materially changed problem."})
                _, reservation = runtime.reserve_request(session, {"attempt_id": task["attempt_id"],
                    "token": task["token"], "requested_output_tokens": 512})
                assert reservation["continue"] is False
                assert not session.scalars(select(ProviderRequest).where(
                    ProviderRequest.attempt_id == task["attempt_id"])).all()

    asyncio.run(scenario())


def test_unknown_retry_keeps_original_charge_in_explore_and_final_reserved(app):
    async def scenario():
        client, _, _, root = await setup(app, config={"budget_policy": "fixed"})
        async with aclosing(client):
            with app.state.database.sessions.begin() as session:
                runtime = Runtime(app.state.service)
                config = session.get(AgentRun, root["run_id"])
                config.options = {**config.options, "unknown_recovery": "once"}
                _, task = runtime.claim(session, {"run_id": root["run_id"]})
                _, reserved = runtime.reserve_request(session, {"attempt_id": task["attempt_id"],
                    "token": task["token"], "requested_output_tokens": 512})
                runtime.agent.observe(session, {"request_id": reserved["request_id"], "token": task["token"],
                    "observation": {"raw_text": "", "complete": False, "call_config": {"provider": "deepseek"}}})
                runtime.start_request(session, {"request_id": reserved["request_id"], "token": task["token"]})
                _, settled = runtime.settle_request(session, {"request_id": reserved["request_id"],
                    "token": task["token"], "outcome": "unknown", "retry_unknown": True,
                    "reason": "transport_outcome_unknown"})
                search = runtime.search.session_for(session, root["run_id"])
                budget = runtime.search.budget(session, search)
                assert settled["unknown_retry_allowed"] is True
                assert budget["explore"]["requests"] == 1
                assert budget["final"]["remaining_requests"] == search.config["final_requests"]

    asyncio.run(scenario())


def test_late_route_completion_after_root_pause_is_quarantined(app):
    async def scenario():
        client, _, _, root = await setup(app)
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], concurrency=1)
        async with aclosing(client):
            claimed = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                headers={"Idempotency-Key": str(uuid4())})
            assert claimed.is_success and claimed.json()["task"]
            await worker.execute(claimed.json()["task"])
            child = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                headers={"Idempotency-Key": str(uuid4())})
            task = child.json()["task"]
            assert task and task["run_id"] != root["run_id"]
            reserved = await client.post(f"/attempts/{task['attempt_id']}/requests", json={
                "token": task["token"], "requested_output_tokens": task["max_output_tokens"]}, headers={"Idempotency-Key": str(uuid4())})
            request_id = reserved.json()["request_id"]
            await client.post(f"/requests/{request_id}/observation", json={"token": task["token"],
                "observation": {"raw_text": "", "complete": False, "call_config": {"provider": "deepseek"}}},
                headers={"Idempotency-Key": str(uuid4())})
            await client.post(f"/requests/{request_id}/start", json={"token": task["token"]},
                headers={"Idempotency-Key": str(uuid4())})
            paused = await client.post(f"/runs/{root['run_id']}/interventions", json={"action": "pause"},
                headers={"Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
            assert paused.is_success
            result = {"mode": task["mode"], "body": "late route output", "findings": ["late"],
                "cited_revision_ids": [], "verdict": None, "scope": None, "actions": [],
                "structured_gaps": [], "next_action": "finish"}
            await client.post(f"/requests/{request_id}/observation", json={"token": task["token"],
                "observation": {"raw_text": "late", "complete": True, "call_config": {"provider": "deepseek"}},
                "result": result}, headers={"Idempotency-Key": str(uuid4())})
            await client.post(f"/requests/{request_id}/settle", json={"token": task["token"], "outcome": "spent"},
                headers={"Idempotency-Key": str(uuid4())})
            completed = await client.post(f"/attempts/{task['attempt_id']}/steps", json={"token": task["token"],
                "request_id": request_id, "result": result}, headers={"Idempotency-Key": str(uuid4())})
            assert completed.is_success, completed.text
            assert completed.json()["quarantined"] is True

    asyncio.run(scenario())


def test_two_route_reservations_are_idempotent_and_leave_final_pool(app):
    async def scenario():
        client, _, _, root = await setup(app, config={"budget_policy": "fixed"})
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], concurrency=1)
        async with aclosing(client):
            analysis = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                headers={"Idempotency-Key": str(uuid4())})
            await worker.execute(analysis.json()["task"])
            tasks = []
            for _ in range(2):
                claimed = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                    headers={"Idempotency-Key": str(uuid4())})
                assert claimed.json()["task"]
                tasks.append(claimed.json()["task"])
            key = str(uuid4())
            first = await client.post(f"/attempts/{tasks[0]['attempt_id']}/requests", json={
                "token": tasks[0]["token"], "requested_output_tokens": 256}, headers={"Idempotency-Key": key})
            replay = await client.post(f"/attempts/{tasks[0]['attempt_id']}/requests", json={
                "token": tasks[0]["token"], "requested_output_tokens": 256}, headers={"Idempotency-Key": key})
            second = await client.post(f"/attempts/{tasks[1]['attempt_id']}/requests", json={
                "token": tasks[1]["token"], "requested_output_tokens": 256}, headers={"Idempotency-Key": str(uuid4())})
            assert first.json()["request_id"] == replay.json()["request_id"]
            assert second.is_success
            snapshot = await state(client, root["run_id"])
            # Analysis has already used one explore request; the duplicate
            # reservation did not add a fourth charge.
            assert snapshot["budget"]["explore"]["requests"] == 3
            assert snapshot["budget"]["final"]["remaining_requests"] == snapshot["session"]["config"]["final_requests"]

    asyncio.run(scenario())


def test_paused_queued_route_resumes_to_its_saved_work_kind(app):
    async def scenario():
        client, _, _, root = await setup(app)
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], concurrency=1)
        async with aclosing(client):
            analysis = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                headers={"Idempotency-Key": str(uuid4())})
            await worker.execute(analysis.json()["task"])
            before = await state(client, root["run_id"])
            route = before["routes"][0]
            pause_url = f"/runs/{root['run_id']}/search/routes/{route['id']}/interventions"
            paused = await client.post(pause_url, json={"action": "pause"}, headers={
                "Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
            assert paused.is_success and paused.json()["state"] == "paused"
            resumed = await client.post(pause_url, json={"action": "resume"}, headers={
                "Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
            assert resumed.is_success and resumed.json()["state"] == "ready"
            claimed = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                headers={"Idempotency-Key": str(uuid4())})
            assert claimed.json()["task"]["search_context"]["route_id"] == route["id"]
            assert claimed.json()["task"]["search_context"]["kind"] == "advance"

    asyncio.run(scenario())


def test_inflight_route_cannot_resume_until_its_request_is_settled(app):
    async def scenario():
        client, _, _, root = await setup(app)
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], concurrency=1)
        async with aclosing(client):
            analysis = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                headers={"Idempotency-Key": str(uuid4())})
            await worker.execute(analysis.json()["task"])
            child = await client.post("/worker/claim-next", json={"providers": ["deepseek"]},
                headers={"Idempotency-Key": str(uuid4())})
            task = child.json()["task"]
            route_id = task["search_context"]["route_id"]
            reserved = await client.post(f"/attempts/{task['attempt_id']}/requests", json={
                "token": task["token"], "requested_output_tokens": task["max_output_tokens"]}, headers={"Idempotency-Key": str(uuid4())})
            await client.post(f"/requests/{reserved.json()['request_id']}/observation", json={"token": task["token"],
                "observation": {"raw_text": "", "complete": False, "call_config": {"provider": "deepseek"}}},
                headers={"Idempotency-Key": str(uuid4())})
            await client.post(f"/requests/{reserved.json()['request_id']}/start", json={"token": task["token"]},
                headers={"Idempotency-Key": str(uuid4())})
            url = f"/runs/{root['run_id']}/search/routes/{route_id}/interventions"
            assert (await client.post(url, json={"action": "pause"}, headers={"Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})).is_success
            resumed = await client.post(url, json={"action": "resume"}, headers={"Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
            assert resumed.status_code == 409
            assert resumed.json()["error"] == "search_route_pause_pending"

    asyncio.run(scenario())


def test_share_memory_action_preserves_conditions_and_target_can_read_it(app):
    async def scenario():
        client, _, _, root = await setup(app, config={"budget_policy": "fixed"})
        solver = Solver()
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], concurrency=1)
        async with aclosing(client):
            analysis = await client.post("/worker/claim-next", json={"providers": ["deepseek"]}, headers={"Idempotency-Key": str(uuid4())})
            await worker.execute(analysis.json()["task"])
            tasks = []
            for _ in range(2):
                response = await client.post("/worker/claim-next", json={"providers": ["deepseek"]}, headers={"Idempotency-Key": str(uuid4())})
                tasks.append(response.json()["task"])
            source, target = tasks
            source_revision = source["search_context"]["route"]["card_revision_id"]
            with app.state.database.sessions.begin() as session:
                runtime = Runtime(app.state.service)
                response = runtime.agent.execute_action(session, session.get(Run, source["run_id"]), AgentAction(type="share_memory", arguments={
                    "revision_id": source_revision, "target_route_id": target["search_context"]["route_id"],
                    "assumptions": ["x is positive"], "reason": "needed by target route"}))
                entry = session.get(SearchMemory, response["memory_id"])
                assert entry.assumptions == ["x is positive"]
                packet = runtime.search.memory.packet(session, root["run_id"], target["search_context"]["route_id"])
                assert source_revision in {item["revision_id"] for item in packet["entries"]}

    asyncio.run(scenario())
