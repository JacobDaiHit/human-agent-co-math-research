"""Actual RemoteProvider -> worker -> API rejects malformed tools, then corrects them."""

import json

import httpx
from mathagent.persistence.models import Adoption, Review, Revision
from mathagent.providers.remote import ProviderConfig, RemoteProvider
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_autonomous_agent import action, exercise, result
from test_autonomous_agent import app as app


def test_remote_worker_corrects_calculator_arguments_using_visible_schema_and_safe_receipt(
    app, monkeypatch
):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")

    async def scenario(api, client):
        project = await api.write(
            "/projects", {"title": "工具契约修复", "body": "检查二项式展开。"}
        )
        await api.write(
            f"/projects/{project['project_id']}/runtime-settings",
            {"request_budget": 2, "allow_real_api": True, "allowed_providers": ["deepseek"]},
            method="PUT",
        )
        run = await api.write(
            "/runs",
            {
                "branch_id": project["branch_id"],
                "goal_object_id": project["object_id"],
                "provider": "deepseek",
                "autonomous": True,
                "request_budget": 2,
            },
        )
        wire_requests = []

        def wire(request):
            payload = json.loads(request.content)
            wire_requests.append(payload)
            prompt = payload["messages"][0]["content"]
            assert '"PolynomialInputs"' in prompt
            assert '"required": ["left", "right", "variables"]' in prompt
            assert (
                "explicit *" in prompt
                and "never ^" in prompt
                and "lhs/rhs are not aliases" in prompt
            )
            task = json.loads(payload["messages"][1]["content"])
            if len(wire_requests) == 1:
                output = result(
                    "请求核对 $(x+y)^2$ 的展开。",
                    [
                        action(
                            "calculate",
                            tool="polynomial_identity",
                            inputs={
                                "lhs": "(x+y)^2",
                                "rhs": "x^2+2xy+y^2",
                                "PRIVATE_FIELD_NAME": "PRIVATE_FIELD_VALUE",
                            },
                        )
                    ],
                )
            else:
                feedback = task["operation_results"][0]
                assert (
                    feedback["status"] == "rejected"
                    and feedback["error"] == "invalid_operation_arguments"
                )
                assert feedback["expected_inputs"] == [
                    "inputs.left",
                    "inputs.right",
                    "inputs.variables",
                ]
                missing = {
                    tuple(item["path"])
                    for item in feedback["validation_errors"]
                    if item["code"] == "missing"
                }
                assert missing == {("inputs", "left"), ("inputs", "right"), ("inputs", "variables")}
                assert "PRIVATE_FIELD" not in json.dumps(feedback)
                assert "**" in feedback["syntax_hint"] and "explicit *" in feedback["syntax_hint"]
                output = result(
                    "按明确字段和算术语法修正工具参数。",
                    [
                        action(
                            "calculate",
                            tool="polynomial_identity",
                            inputs={
                                "left": "(x+y)**2",
                                "right": "x**2+2*x*y+y**2",
                                "variables": ["x", "y"],
                            },
                            target_revision_id=project["revision_id"],
                        )
                    ],
                    next_action="finish",
                )
            return httpx.Response(
                200,
                json={
                    "id": f"synthetic-{len(wire_requests)}",
                    "usage": {"total_tokens": 15},
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(output["result"], ensure_ascii=False)
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as provider_client:
            config = ProviderConfig(
                "deepseek", "synthetic-key", "synthetic-model", "https://mock.invalid", True
            )
            await HTTPWorker(
                client,
                providers=["deepseek"],
                provider_factory=lambda _: RemoteProvider(config, provider_client),
            ).run(once=True)
        assert len(wire_requests) == 2
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == 2
        assert steps[0]["actions"][0]["status"] == "rejected"
        success = steps[1]["actions"][0]
        assert success["status"] == "completed"
        assert success["result"]["exact_result"]["identity"] is True
        with app.state.database.sessions() as session:
            calculations = [
                rev
                for rev in session.scalars(select(Revision))
                if rev.payload.get("artifact_type") == "exact_calculation"
            ]
            assert len(calculations) == 1
            assert calculations[0].payload["input"] == {
                "left": "(x+y)**2",
                "right": "x**2+2*x*y+y**2",
                "variables": ["x", "y"],
            }
            review = session.get(Review, success["result"]["review_id"])
            assert review.kind == "exact_computation" and review.coverage == "partial"
            assert review.target_revision_id == project["revision_id"]
            assert session.scalars(select(Adoption)).all() == []
        budget = await api.get(f"/runs/{run['run_id']}/budget")
        assert budget["spent"] == 2 and budget["unknown"] == 0
        assert len((await api.get(f"/runs/{run['run_id']}/calls"))["calls"]) == 2

    exercise(app, scenario)
