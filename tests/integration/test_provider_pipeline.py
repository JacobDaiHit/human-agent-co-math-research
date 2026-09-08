"""Wire adapters, durable observations, paid repair and ledger integration (no network)."""

import json

import httpx
import pytest
from mathagent.providers.remote import ProviderConfig, RemoteProvider
from mathagent.runtime.worker import HTTPWorker
from test_autonomous_agent import app as app
from test_autonomous_agent import exercise, project_and_run, result


@pytest.mark.parametrize("provider_name", ["deepseek", "glm"])
@pytest.mark.parametrize("budget", [1, 2])
def test_actual_adapter_metadata_is_accepted_and_repair_obeys_budget(app, monkeypatch, provider_name, budget):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv(f"MATHAGENT_{provider_name.upper()}_API_KEY", "synthetic-key")
    monkeypatch.setenv(f"MATHAGENT_{provider_name.upper()}_MODEL", "synthetic-model")

    async def scenario(api, client):
        p, _ = await project_and_run(api, autonomous=False)
        await api.write(f"/projects/{p['project_id']}/runtime-settings", {
            "request_budget": budget, "allow_real_api": True, "allowed_providers": [provider_name]}, method="PUT")
        run = await api.write("/runs", {"branch_id": p["branch_id"], "goal_object_id": p["object_id"],
            "autonomous": True, "provider": provider_name, "request_budget": budget})
        captured = []

        def transport(request):
            payload = json.loads(request.content)
            captured.append(payload)
            user = json.loads(payload["messages"][1]["content"])
            if len(captured) == 1:
                body = "a malformed response with a visible $x^2$ fragment"
                assert user["repair_output"] is None
            else:
                assert "$x^2$" in user["repair_output"]
                body = json.dumps(result("对实数 $x$，$x^2+1>0$。", next_action="finish")["result"], ensure_ascii=False)
            return httpx.Response(200, json={"id": f"synthetic-{len(captured)}", "usage": {"total_tokens": 11},
                "choices": [{"message": {"content": body}, "finish_reason": "stop"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as wire:
            config = ProviderConfig(provider_name, "synthetic-key", "synthetic-model", "https://mock.invalid", True)
            await HTTPWorker(client, providers=[provider_name], provider_factory=lambda _: RemoteProvider(config, wire)).run(once=True)
        assert len(captured) == budget
        calls = (await api.get(f"/runs/{run['run_id']}/calls"))["calls"]
        assert len(calls) == budget
        assert all(c["call_config"]["transport_timeout_seconds"] and c["raw_text"] and c["raw_sha256"] for c in calls)
        assert all(c["usage"] == {"total_tokens": 11} for c in calls)
        assert "synthetic-key" not in json.dumps(calls)
        accounting = await api.get(f"/runs/{run['run_id']}/budget")
        assert accounting["spent"] == budget and accounting["unknown"] == 0
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == budget - 1
        if budget == 2:
            assert calls[0]["call_config"]["prompt_sha256"] != calls[1]["call_config"]["prompt_sha256"]
            assert calls[0]["result"] is None and calls[1]["result"]
    exercise(app, scenario)
