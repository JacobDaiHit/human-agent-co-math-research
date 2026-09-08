"""Missing flow decisions must be repaired before any autonomous operation executes."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app
from mathagent.providers.protocol import PROMPT_VERSION
from mathagent.providers.remote import ProviderConfig, RemoteProvider
from mathagent.runtime.worker import HTTPWorker


@pytest.mark.parametrize("budget,valid_repair,expected_steps", [(1, False, 0), (2, False, 0), (2, True, 1)])
def test_missing_next_action_uses_paid_repair_before_actions_or_completion(tmp_path, monkeypatch,
                                                                         budget, valid_repair, expected_steps):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-decision-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")
    app = create_app(tmp_path / "decision.sqlite3", token="human", worker_token="worker")
    app.state.database.migrate()
    missing = {"mode": "research", "body": "A candidate response still requiring work.",
        "findings": ["Only a synthetic protocol regression."], "verdict": None,
        "actions": [{"type": "write_draft", "arguments": {"kind": "claim", "body": "Deferred action marker."}}]}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000",
                                     headers={"Authorization": "Bearer worker"}) as api:
            async def write(path, payload, method="POST"):
                response = await api.request(method, path, json=payload, headers={
                    "Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
                assert response.is_success, response.text
                return response.json()

            async def read(path):
                response = await api.get(path, headers={"Authorization": "Bearer human"})
                assert response.is_success, response.text
                return response.json()

            project = await write("/projects", {"title": "Flow decision fixture", "body": "Synthetic target."})
            await write(f"/projects/{project['project_id']}/runtime-settings", {
                "request_budget": budget, "allow_real_api": True, "allowed_providers": ["deepseek"]}, "PUT")
            run = await write("/runs", {"branch_id": project["branch_id"], "goal_object_id": project["object_id"],
                "provider": "deepseek", "autonomous": True, "request_budget": budget})
            seen = []

            async def transport(request):
                payload = json.loads(request.content)
                task = json.loads(payload["messages"][1]["content"])
                seen.append(task)
                if len(seen) == 2:
                    assert json.loads(task["repair_output"]) == missing
                    prior_budget = seen[0]["request_budget_status"]
                    repair_budget = task["request_budget_status"]
                    assert prior_budget["remaining"] == budget
                    assert repair_budget["remaining"] == budget - 1
                    assert repair_budget["after_this_request"] == max(0, budget - 2)
                    assert repair_budget["snapshot"] == "adjusted_after_format_failure"
                    assert repair_budget["stale"] is True
                    assert repair_budget["known_consumed_since_snapshot"] == 1
                    assert all(new["occupied"] == old["occupied"] + 1
                               for old, new in zip(prior_budget["scopes"], repair_budget["scopes"], strict=True))
                    assert (await read(f"/runs/{run['run_id']}/steps"))["steps"] == []
                    snapshot = await read(f"/projects/{project['project_id']}/snapshot")
                    assert len(snapshot["objects"]) == 1
                    assert snapshot["runs"][0]["state"] != "completed"
                    first_call = (await read(f"/runs/{run['run_id']}/calls"))["calls"][0]
                    assert first_call["result"] is None and first_call["complete"] is True
                response = {**missing, "next_action": "continue"} if len(seen) == 2 and valid_repair else missing
                return httpx.Response(200, json={"id": f"synthetic-{len(seen)}",
                    "choices": [{"message": {"content": json.dumps(response)}, "finish_reason": "stop"}]})

            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as wire:
                config = ProviderConfig("deepseek", "synthetic-decision-key", "synthetic-model", "https://mock.invalid", True)
                await HTTPWorker(api, providers=["deepseek"], provider_factory=lambda _: RemoteProvider(config, wire)).run(once=True)
            assert len(seen) == budget
            steps = (await read(f"/runs/{run['run_id']}/steps"))["steps"]
            assert len(steps) == expected_steps
            snapshot = await read(f"/projects/{project['project_id']}/snapshot")
            assert snapshot["runs"][0]["state"] != "completed"
            if expected_steps:
                assert len(steps[0]["actions"]) == 1 and steps[0]["actions"][0]["status"] == "completed"
                assert snapshot["runs"][0]["state"] == "budget_exhausted"
            else:
                assert len(snapshot["objects"]) == 1
            calls = (await read(f"/runs/{run['run_id']}/calls"))["calls"]
            assert calls[0]["result"] is None
            assert all(call["call_config"]["prompt_template_version"] == PROMPT_VERSION for call in calls)
            if budget == 2:
                assert (calls[1]["result"] is not None) is valid_repair
            accounting = await read(f"/runs/{run['run_id']}/budget")
            assert accounting["spent"] == budget and accounting["unknown"] == 0

    try:
        asyncio.run(scenario())
    finally:
        app.state.database.close()
