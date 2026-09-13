"""Unknown transport continuation over the real HTTP worker/API, without inference sockets."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from mathagent.persistence.models import Attempt, Head
from mathagent.persistence.runtime_models import ProviderRequest
from mathagent.providers.remote import ProviderConfig, ProviderFailure, RemoteProvider
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_autonomous_agent import action, exercise, project_and_run, result
from test_autonomous_agent import app as app


@pytest.fixture(autouse=True)
def synthetic_config(monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")


async def setup_run(api, *, budget=8, **options):
    project, _ = await project_and_run(api, autonomous=False)
    await api.write(f"/projects/{project['project_id']}/runtime-settings", {
        "request_budget": budget, "allow_real_api": True, "allowed_providers": ["deepseek"]}, method="PUT")
    run = await api.write("/runs", {
        "branch_id": project["branch_id"], "goal_object_id": project["object_id"],
        "autonomous": True, "provider": "deepseek", "request_budget": budget,
        "unknown_recovery": "once", **options})
    return project, run


@pytest.mark.parametrize("policy,budget,repeated,expected", [
    ("once", 3, False, 2), ("once", 3, True, 2),
    ("once", 1, False, 1), ("stop", 3, False, 1),
])
def test_wire_unknown_retains_occupancy_and_never_executes_partial_output(app, policy, budget, repeated, expected):
    async def scenario(api, client):
        project, run = await setup_run(api, budget=budget, unknown_recovery=policy)
        captured = []

        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                fragment = json.dumps(result("MUST_NOT_EXECUTE", [action("write_draft", kind="claim", body="MUST_NOT_EXECUTE")])["result"])
                yield ("data: " + json.dumps({"id": "unknown-id", "choices": [{"delta": {"content": fragment}}]}) + "\n\n").encode()
                raise httpx.ReadError("synthetic interruption")

        def transport(request):
            payload = json.loads(request.content)
            captured.append(payload)
            if len(captured) == 1 or repeated:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream())
            task = json.loads(payload["messages"][1]["content"])
            assert task["request_budget_status"]["remaining"] == budget - 1
            assert all(scope["unknown"] == 1 for scope in task["request_budget_status"]["scopes"])
            assert "MUST_NOT_EXECUTE" not in json.dumps(payload)
            return httpx.Response(200, json={"usage": {"total_tokens": 10}, "choices": [{
                "message": {"content": json.dumps(result("Valid result.", next_action="finish")["result"])}, "finish_reason": "stop"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as wire:
            config = ProviderConfig("deepseek", "synthetic-key", "synthetic-model", "https://mock.invalid", True)
            await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: RemoteProvider(config, wire)).run(once=True)
        assert len(captured) == expected
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["occupied"] == expected
        assert ledger["unknown"] == (2 if repeated else 1)
        assert ledger["spent"] == (1 if expected == 2 and not repeated else 0)
        calls = (await api.get(f"/runs/{run['run_id']}/calls"))["calls"]
        assert all(call["result"] is None for call in calls if not call["complete"])
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == ledger["spent"]
        assert all(not step["actions"] for step in steps)
        snapshot = await api.get(f"/projects/{project['project_id']}/snapshot")
        current = next(r for r in snapshot["runs"] if r["id"] == run["run_id"])
        assert current["state"] == ("completed" if ledger["spent"] else "reconciliation_required")
    exercise(app, scenario)


def test_unknown_output_usage_keeps_the_full_pre_dispatch_reservation(app):
    async def scenario(api, client):
        _, run = await setup_run(
            api, budget=3, unknown_recovery="stop", cumulative_output_token_budget=300
        )

        class Unknown:
            async def generate(self, task):
                assert task["max_output_tokens"] == 300
                raise ProviderFailure("transport_read_error", outcome="unknown")

        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Unknown()).run(once=True)
        tokens = (await api.get(f"/runs/{run['run_id']}/budget"))["output_token_budget"]
        assert tokens["reported_output_tokens"] == 0
        assert tokens["reserved_output_tokens"] == tokens["occupied_output_tokens"] == 300
        assert tokens["remaining_output_tokens"] == 0
        assert tokens["unknown_outcome_requests"] == tokens["unknown_usage_requests"] == 1
        # Simulate a pre-0007 unknown request.  It had no reservation column
        # value, so enabling a limit later must use the conservative fallback.
        with app.state.database.sessions.begin() as session:
            session.scalars(select(ProviderRequest).where(ProviderRequest.run_id == run["run_id"])).one().output_token_reservation = 0
        migrated = (await api.get(f"/runs/{run['run_id']}/budget"))["output_token_budget"]
        assert migrated["reserved_output_tokens"] == migrated["occupied_output_tokens"] == 65_536
    exercise(app, scenario)


@pytest.mark.parametrize("fence", ["pause", "cancel", "branch_pause", "stale", "expired", "permission", "local_protocol"])
def test_unknown_retry_respects_controls_inputs_leases_and_error_scope(app, fence):
    async def scenario(api, client):
        project, run = await setup_run(api)
        calls = []

        class Script:
            async def generate(self, task):
                calls.append(task)
                assert len(calls) == 1
                if fence in {"pause", "cancel"}:
                    await api.write(f"/runs/{run['run_id']}/interventions", {"action": fence})
                elif fence == "branch_pause":
                    await api.write(f"/branches/{project['branch_id']}/interventions", {"action": "pause"})
                elif fence == "permission":
                    await api.write(f"/projects/{project['project_id']}/runtime-settings", {
                        "request_budget": 8, "allow_real_api": False, "allowed_providers": ["deepseek"]}, method="PUT")
                elif fence in {"stale", "expired"}:
                    with app.state.database.sessions.begin() as session:
                        if fence == "expired":
                            session.get(Attempt, task["attempt_id"]).lease_until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                        else:
                            session.delete(session.get(Head, (project["branch_id"], project["object_id"])))
                raise ProviderFailure("transport_local_protocol_error" if fence == "local_protocol" else "transport_read_error", outcome="unknown")

        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script()).run(once=True)
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["unknown"] == ledger["occupied"] == len(calls) == 1
        with app.state.database.sessions() as session:
            assert not session.get(Attempt, calls[0]["attempt_id"]).checkpoint.get("continued_unknown_request_ids")
    exercise(app, scenario)


def test_allowance_survives_later_steps_reconciliation_options_update_and_restart(app):
    async def scenario(api, client):
        project, run = await setup_run(api)
        calls = []

        class Script:
            async def generate(self, task):
                calls.append(task)
                if len(calls) == 2:
                    return result("A valid intermediate step.")
                raise ProviderFailure("transport_read_error", outcome="unknown")

        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script()).run(once=True)
        assert len(calls) == 3 and calls[0]["attempt_id"] == calls[1]["attempt_id"] != calls[2]["attempt_id"]
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["unknown"] == 2 and ledger["spent"] == 1
        for request in ledger["requests"]:
            if request["state"] == "unknown":
                await api.write(f"/requests/{request['request_id']}/reconcile", {"outcome": "spent", "reason": "Synthetic acceptance confirmation."})
        options = await api.get(f"/runs/{run['run_id']}/options")
        options.pop("run_id")
        await api.write(f"/runs/{run['run_id']}/options", options, method="PUT")
        await api.write(f"/runs/{run['run_id']}/resume")
        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script()).run(once=True)
        assert len(calls) == 4
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["unknown"] == 1 and ledger["spent"] == 3 and ledger["occupied"] == 4
    exercise(app, scenario)


def test_concurrent_children_share_one_transactional_allowance(app):
    async def scenario(api, client):
        project, run = await setup_run(api)
        seen, arrived = {}, asyncio.Event()

        class Script:
            async def generate(self, task):
                name = task["instruction"]
                seen[name] = seen.get(name, 0) + 1
                if task["run_id"] == run["run_id"]:
                    return result("Delegate two tasks.", [action("spawn_task", goal_object_id=task["goal_object_id"],
                        instruction=name, request_budget=2) for name in ("child-a", "child-b")], next_action="wait")
                if seen[name] == 1:
                    if "child-a" in seen and "child-b" in seen:
                        arrived.set()
                    await asyncio.wait_for(arrived.wait(), 10)
                    raise ProviderFailure("transport_read_error", outcome="unknown")
                return result("Synthetic child output.", next_action="finish")

        await HTTPWorker(client, providers=["deepseek"], concurrency=2, provider_factory=lambda _: Script()).run(once=True)
        assert sorted(seen.values()) == [1, 1, 2]
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["unknown"] == 2 and ledger["spent"] == 2 and ledger["occupied"] == 4
        with app.state.database.sessions() as session:
            from sqlalchemy import select
            assert sum(bool(a.checkpoint.get("continued_unknown_request_ids")) for a in session.scalars(select(Attempt))) == 1
    exercise(app, scenario)


@pytest.mark.parametrize("after_authorization", ["pause", "expired", "late_response", "budget_lowered"])
def test_retry_authorization_is_not_permission_to_bypass_next_dispatch_boundary(app, after_authorization):
    async def scenario(api, client):
        project, run = await setup_run(api)
        task = await api.write(f"/runs/{run['run_id']}/claim", worker=True)
        execution = {"token": task["token"]}
        prefix = f"/attempts/{task['attempt_id']}"
        request = await api.write(prefix + "/requests", execution, worker=True)
        request_path = f"/requests/{request['request_id']}"
        await api.write(request_path + "/observation", {**execution, "observation": {
            "call_config": {}, "raw_text": "Partial visible response", "complete": False}}, worker=True)
        await api.write(request_path + "/start", execution, worker=True)
        settlement = await api.write(request_path + "/settle", {**execution, "outcome": "unknown",
            "reason": "transport_read_error", "retry_unknown": True}, worker=True)
        assert settlement["unknown_retry_allowed"] and settlement["state"] == "unknown"
        if after_authorization == "pause":
            await api.write(f"/runs/{run['run_id']}/interventions", {"action": "pause"})
            assert not (await api.write(prefix + "/requests", execution, worker=True))["continue"]
        elif after_authorization == "budget_lowered":
            await api.write(f"/projects/{project['project_id']}/runtime-settings", {
                "request_budget": 1, "allow_real_api": True, "allowed_providers": ["deepseek"]}, method="PUT")
            response = await api.write(prefix + "/requests", execution, worker=True)
            assert response["continue"] is False and response["state"] == "budget_exhausted"
        elif after_authorization == "expired":
            with app.state.database.sessions.begin() as session:
                session.get(Attempt, task["attempt_id"]).lease_until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            await api.write(prefix + "/requests", execution, worker=True, expect=409)
            claimed = await api.write("/worker/claim-next", {"providers": ["deepseek"]}, worker=True)
            assert claimed["task"] is None
        else:
            await api.write(request_path + "/settle", {**execution, "outcome": "spent"}, worker=True, expect=409)
            await api.write(request_path + "/observation", {**execution, "observation": {
                "call_config": {}, "raw_text": "Late result", "complete": True},
                "result": result("Late result", next_action="finish")["result"]}, worker=True, expect=409)
            await api.write(prefix + "/steps", {**execution, "request_id": request["request_id"],
                "result": result("Late result", next_action="finish")["result"]}, worker=True, expect=409)
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["unknown"] == ledger["occupied"] == 1
        assert not (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
    exercise(app, scenario)


def test_saved_retry_response_is_quarantined_after_crash_without_losing_unknown_cost(app):
    async def scenario(api, client):
        project, run = await setup_run(api)
        task = await api.write(f"/runs/{run['run_id']}/claim", worker=True)
        execution = {"token": task["token"]}
        prefix = f"/attempts/{task['attempt_id']}"
        for index in range(2):
            request = await api.write(prefix + "/requests", execution, worker=True)
            path = f"/requests/{request['request_id']}"
            await api.write(path + "/start", execution, worker=True)
            output = result("Durable retry output.", [action("write_draft", kind="claim", body="OLD_ACTION_MUST_NOT_RUN")])["result"]
            await api.write(path + "/observation", {**execution, "observation": {
                "call_config": {}, "raw_text": json.dumps(output) if index else "", "complete": bool(index)},
                **({"result": output} if index else {})}, worker=True)
            if index == 0:
                settled = await api.write(path + "/settle", {**execution, "outcome": "unknown",
                    "reason": "transport_read_error", "retry_unknown": True}, worker=True)
                assert settled["unknown_retry_allowed"]
        with app.state.database.sessions.begin() as session:
            session.get(Attempt, task["attempt_id"]).lease_until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        claimed = await api.write("/worker/claim-next", {"providers": ["deepseek"]}, worker=True)
        assert claimed["task"] is None
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["unknown"] == ledger["spent"] == 1 and ledger["occupied"] == 2
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == 1 and steps[0]["receipt"]["quarantined"] and not steps[0]["actions"]
        assert steps[0]["body"] == "Durable retry output."
    exercise(app, scenario)


def test_child_can_disable_inherited_recovery_during_inflight_request(app):
    async def scenario(api, client):
        _, run = await setup_run(api)
        seen = []

        class Script:
            async def generate(self, task):
                seen.append(task["run_id"])
                if task["run_id"] == run["run_id"]:
                    return result("Delegate.", [action("spawn_task", goal_object_id=task["goal_object_id"],
                        instruction="child", request_budget=3)], next_action="wait")
                options = await api.get(f"/runs/{task['run_id']}/options")
                options.pop("run_id")
                await api.write(f"/runs/{task['run_id']}/options", {**options, "unknown_recovery": "stop"}, method="PUT")
                raise ProviderFailure("transport_read_error", outcome="unknown")

        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script()).run(once=True)
        assert len(seen) == 2
        ledger = await api.get(f"/runs/{run['run_id']}/budget")
        assert ledger["unknown"] == 1 and ledger["spent"] == 1
    exercise(app, scenario)
