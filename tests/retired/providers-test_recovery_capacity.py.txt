"""Adaptive search recovery respects fresh output capacity snapshots."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app
from mathagent.providers.remote import ProviderConfig, ProviderFailure, RemoteProvider
from mathagent.runtime.worker import HTTPWorker


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-capacity-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")
    application = create_app(tmp_path / "recovery-capacity.sqlite3", token="human", worker_token="worker")
    application.state.database.migrate()
    yield application
    application.state.database.close()


def _route_action():
    return {"type": "propose_routes", "arguments": {"routes": [{
        "title": "合成路线", "core_reduction": "把问题化为局部恒等式。",
        "key_lemmas": ["局部恒等式"], "assumptions": [],
        "subgoal": "证明局部恒等式。", "cheap_check": "代入边界值。",
    }]}}


class _SyntheticProvider:
    def __init__(self, calls, *, length_first):
        self.calls = calls
        self.length_first = length_first

    async def generate(self, task):
        config = ProviderConfig("deepseek", "synthetic-key", "synthetic-model", "https://mock.invalid", True)
        described = RemoteProvider(config).describe(task)
        self.calls.append({
            "max_output_tokens": task["max_output_tokens"],
            "provider_options": dict(task.get("provider_options", {})),
            "parameters": described["parameters"],
        })
        if self.length_first and len(self.calls) == 1:
            raise ProviderFailure("incomplete_output", outcome="spent", observation={
                "raw_text": "截断的合成输出", "finish_reason": "length", "complete": False,
                "usage": {"completion_tokens": task["max_output_tokens"]},
            })
        return {
            "result": {"mode": task["mode"], "body": "合成进展。",
                       "findings": ["仅用于容量验收。"], "verdict": None,
                       "scope": None, "actions": [_route_action()],
                       "next_action": "finish", "cited_revision_ids": []},
            "usage": {"total_tokens": 10}, "provider_request_id": "synthetic",
        }


class _BoundaryWorker(HTTPWorker):
    def __init__(self, *args, boundary_caps, **kwargs):
        super().__init__(*args, **kwargs)
        self.boundary_caps = iter(boundary_caps)
        self.reservations = []
        self.failures = []

    async def _post(self, path, payload):
        response = await super()._post(path, payload)
        if path.endswith("/heartbeat") and payload.get("boundary"):
            response = {**response, "search_output_cap": next(self.boundary_caps),
                        "search_min_output_tokens": 4096}
        elif path.endswith("/requests"):
            self.reservations.append(payload["requested_output_tokens"])
        elif path.endswith("/fail"):
            self.failures.append(payload["reason"])
        return response


async def _run(app, *, max_output_tokens, boundary_caps, length_first=True):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000",
        headers={"Authorization": "Bearer worker"},
    ) as client:
        async def write(path, payload, method="POST"):
            response = await client.request(method, path, json=payload, headers={
                "Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
            assert response.is_success, response.text
            return response.json()

        project = await write("/projects", {"title": "容量恢复", "body": "研究 $x^2+1>0$。"})
        await write(f"/projects/{project['project_id']}/runtime-settings", {
            "allow_real_api": True, "allowed_providers": ["deepseek"], "request_budget": 12,
        }, "PUT")
        run = await write("/runs", {
            "branch_id": project["branch_id"], "goal_object_id": project["object_id"],
            "provider": "deepseek", "autonomous": True, "request_budget": 12,
            "max_output_tokens": max_output_tokens, "cumulative_output_token_budget": 32768,
            "solver_controller": "bounded_search_v1", "search_config": {
                "budget_policy": "adaptive", "analysis_output_tokens": 8192,
            }, "length_recovery": "high",
        })
        calls = []
        provider = _SyntheticProvider(calls, length_first=length_first)
        worker = _BoundaryWorker(client, providers=["deepseek"],
                                 provider_factory=lambda _: provider, boundary_caps=boundary_caps,
                                 fake_delay_seconds=0)
        claimed = await worker._post("/worker/claim-next", {"providers": ["deepseek"]})
        await worker.execute(claimed["task"])
        return run, calls, worker


def test_recovery_refuses_when_fresh_search_capacity_shrinks(app):
    async def scenario():
        _, calls, worker = await _run(app, max_output_tokens=8192, boundary_caps=[8192, 4096])
        assert len(calls) == 1
        assert calls[0]["max_output_tokens"] == worker.reservations[0] == 8192
        assert worker.failures == ["search_recovery_capacity"]

    asyncio.run(scenario())


def test_recovery_uses_fresh_capacity_and_matches_reservation(app):
    async def scenario():
        _, calls, worker = await _run(app, max_output_tokens=8192, boundary_caps=[8192, 8192])
        assert len(calls) == 2
        assert [call["max_output_tokens"] for call in calls] == [8192, 8192]
        assert worker.reservations == [8192, 8192]
        assert all(call["parameters"]["max_tokens"] == 8192 for call in calls)
        assert all(call["parameters"]["thinking"] == {"type": "disabled"} for call in calls)
        assert worker.failures == []

    asyncio.run(scenario())


def test_initial_search_capacity_below_minimum_skips_provider(app):
    async def scenario():
        _, calls, worker = await _run(app, max_output_tokens=8192, boundary_caps=[1024])
        assert calls == []
        assert worker.reservations == []
        assert worker.failures == ["search_useful_capacity_exhausted"]

    asyncio.run(scenario())
