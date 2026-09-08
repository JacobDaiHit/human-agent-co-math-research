"""Explicit reasoning settings survive HTTP options, frozen calls, and export."""

import json

import httpx
from mathagent.exports.service import ExportService
from mathagent.providers.remote import DeepSeekProvider, ProviderConfig
from mathagent.runtime.worker import HTTPWorker
from test_autonomous_agent import app as app
from test_autonomous_agent import exercise, project_and_run


def test_reasoning_options_are_exported_separately_from_later_run_changes(app):
    async def scenario(api, client):
        project, run = await project_and_run(
            api, autonomous=False, thinking_mode="enabled", reasoning_effort="max",
            max_output_tokens=65536, request_timeout_seconds=600,
        )
        path = f"/runs/{run['run_id']}/options"
        before = await api.get(path)
        assert before["thinking_mode"] == "enabled" and before["reasoning_effort"] == "max"

        def transport(request):
            payload = json.loads(request.content)
            assert payload["thinking"] == {"type": "enabled"}
            assert payload["reasoning_effort"] == "max" and payload["max_tokens"] == 65536
            return httpx.Response(200, json={"choices": [{
                "message": {"content": json.dumps({
                    "mode": "research", "body": "Synthetic final answer.",
                    "findings": ["Synthetic transport only."], "cited_revision_ids": [],
                    "next_action": "finish",
                })}, "finish_reason": "stop",
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as provider_client:
            provider = DeepSeekProvider(ProviderConfig(
                "deepseek", "synthetic-secret", "deepseek-v4-flash", "https://mock.invalid", True,
            ), provider_client)
            await HTTPWorker(client, provider_factory=lambda _: provider, fake_delay_seconds=0).run(once=True)

        updated = {key: value for key, value in before.items() if key != "run_id"}
        updated["reasoning_effort"] = "high"
        await api.write(path, updated, method="PUT")
        assert (await api.get(path))["reasoning_effort"] == "high"
        calls = (await api.get(f"/runs/{run['run_id']}/calls"))["calls"]
        assert len(calls) == 1
        assert calls[0]["call_config"]["parameters"]["reasoning_effort"] == "max"
        created = await api.write("/exports", {
            "project_id": project["project_id"], "branch_id": project["branch_id"],
            "object_ids": [project["object_id"]],
        })
        bundle = ExportService(app.state.service).get(created["export_id"])
        exported = next(row for row in bundle["snapshot"]["runs"] if row["id"] == run["run_id"])
        assert exported["agent"]["options"]["reasoning_effort"] == "high"
        config = exported["provider_calls"][0]["call_config"]
        assert config["parameters"]["reasoning_effort"] == "max"
        assert config["parameters"]["thinking"] == {"type": "enabled"}
        assert config["parameters"]["max_tokens"] == 65536
        assert config["model"] == "deepseek-v4-flash"
        assert "synthetic-secret" not in json.dumps(bundle)

    exercise(app, scenario)
