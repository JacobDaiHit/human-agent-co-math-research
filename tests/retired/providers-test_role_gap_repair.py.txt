"""Research gap-role violations receive one safe, bounded repair attempt."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app
from mathagent.providers.protocol import repair_feedback, result_schema, validate_result
from mathagent.providers.remote import ProviderConfig, RemoteProvider
from mathagent.runtime.worker import HTTPWorker
from pydantic import ValidationError


def test_research_gap_schema_validation_and_feedback_are_role_specific():
    research_schema = result_schema("research", autonomous=True)
    review_schema = result_schema("review")
    gaps = research_schema["properties"]["structured_gaps"]
    assert gaps["maxItems"] == 0
    assert "body/findings" in gaps["description"]
    assert "report_gap" in gaps["description"]
    assert review_schema["properties"]["structured_gaps"]["maxItems"] > 0

    raw = json.dumps({
        "mode": "research", "body": "Keep this candidate argument. do-not-echo",
        "findings": ["The final implication needs a proof."], "verdict": None,
        "structured_gaps": [{"kind": "missing_argument", "anchor": "final-step",
                             "detail": "The implication needs a proof."}],
        "next_action": "finish",
    })
    with pytest.raises(ValidationError, match="Only reviews can report structured_gaps"):
        validate_result(json.loads(raw), mode="research", read_set={}, autonomous=True)
    feedback = repair_feedback(raw, {"mode": "research", "read_set": {}, "autonomous": True})
    assert feedback[0]["loc"] == [] and feedback[0]["type"] == "value_error"
    assert "Only reviews can report structured_gaps" in feedback[0]["msg"]
    assert "input_value" not in json.dumps(feedback)
    assert "do-not-echo" not in json.dumps(feedback)


def test_remote_research_gap_is_repaired_before_actions_and_charges_both_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")
    app = create_app(tmp_path / "role-gap-repair.sqlite3", token="human", worker_token="worker")
    app.state.database.migrate()
    preserved_body = "The candidate argument remains useful, but its final implication needs proof."
    invalid = {
        "mode": "research", "body": preserved_body,
        "findings": ["The final implication needs a proof."], "verdict": None,
        "structured_gaps": [{"kind": "missing_argument", "anchor": "final-step",
                             "detail": "The implication needs a proof."}],
        "actions": [{"type": "write_draft", "arguments": {"kind": "claim", "body": "Must not run."}}],
        "next_action": "finish",
    }
    corrected = {**invalid, "structured_gaps": [], "actions": []}

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

            project = await write("/projects", {"title": "Role gap repair", "body": "Synthetic target."})
            await write(f"/projects/{project['project_id']}/runtime-settings", {
                "request_budget": 2, "allow_real_api": True, "allowed_providers": ["deepseek"]}, "PUT")
            run = await write("/runs", {"branch_id": project["branch_id"], "goal_object_id": project["object_id"],
                "provider": "deepseek", "autonomous": True, "request_budget": 2})
            seen = []

            async def wire(request):
                payload = json.loads(request.content)
                task = json.loads(payload["messages"][1]["content"])
                seen.append(task)
                if len(seen) == 1:
                    assert task["repair_output"] is None and task["repair_feedback"] is None
                    response = invalid
                else:
                    assert payload["thinking"] == {"type": "disabled"}
                    assert task["repair_output"] == json.dumps(invalid, ensure_ascii=False)
                    assert task["repair_feedback"]
                    assert task["repair_feedback"][0]["loc"] == []
                    assert "Only reviews can report structured_gaps" in task["repair_feedback"][0]["msg"]
                    assert "input_value" not in json.dumps(task["repair_feedback"])
                    assert (await read(f"/runs/{run['run_id']}/steps"))["steps"] == []
                    response = corrected
                return httpx.Response(200, json={"id": f"synthetic-{len(seen)}", "usage": {"total_tokens": 1},
                    "choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}, "finish_reason": "stop"}]})

            async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as wire_client:
                config = ProviderConfig("deepseek", "synthetic-key", "synthetic-model", "https://mock.invalid", True)
                await HTTPWorker(api, providers=["deepseek"],
                    provider_factory=lambda _: RemoteProvider(config, wire_client)).run(once=True)
            assert len(seen) == 2
            calls = (await read(f"/runs/{run['run_id']}/calls"))["calls"]
            assert calls[0]["result"] is None and calls[1]["result"]["body"] == preserved_body
            assert (await read(f"/runs/{run['run_id']}/steps"))["steps"][0]["actions"] == []
            budget = await read(f"/runs/{run['run_id']}/budget")
            assert budget["spent"] == 2 and budget["unknown"] == 0

    try:
        asyncio.run(scenario())
    finally:
        app.state.database.close()
