"""Replay the observed research/review mismatch without paid calls or relaxed checks.

Only revision IDs in the saved response are rebound to synthetic fixture records;
its original mathematical text, mode, verdict and JSON structure are unchanged.
"""

import json
from pathlib import Path

import httpx
import pytest
from mathagent.persistence.models import Adoption, Review, Run
from mathagent.providers.protocol import PROMPT_VERSION
from mathagent.providers.remote import ProviderConfig, RemoteProvider
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_autonomous_agent import action, exercise, result
from test_autonomous_agent import app as app


@pytest.mark.parametrize("budget", [4, 5])
def test_actual_completed_json_with_research_verdict_rejects_then_repairs_only_with_budget(
    app, monkeypatch, budget
):
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/research_verdict_mismatch.json").read_text(
            encoding="utf-8"
        )
    )
    original = json.loads(fixture["raw_text"])
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")

    async def scenario(api, client):
        project = await api.write(
            "/projects", {"title": "实际响应协议回放", "body": "检查对称不等式与研究记录。"}
        )
        claim = await api.write(
            "/objects",
            {
                "branch_id": project["branch_id"],
                "kind": "claim",
                "body": r"实数 $a,b,c$ 满足 $(a+b+c)^2\ge3(ab+bc+ca)$，等号当且仅当 $a=b=c$。",
            },
        )
        await api.write(
            f"/projects/{project['project_id']}/runtime-settings",
            {"request_budget": budget, "allow_real_api": True, "allowed_providers": ["deepseek"]},
            method="PUT",
        )
        run = await api.write(
            "/runs",
            {
                "branch_id": project["branch_id"],
                "goal_object_id": claim["object_id"],
                "provider": "deepseek",
                "autonomous": True,
                "request_budget": budget,
                "max_steps": 3,
                "max_children": 1,
                "max_depth": 1,
                "max_review_rounds": 1,
            },
        )
        wire_requests = []
        rebound_raw = None

        def wire(request):
            nonlocal rebound_raw
            payload = json.loads(request.content)
            task = json.loads(payload["messages"][1]["content"])
            schema = json.loads(payload["messages"][0]["content"].rsplit("\n", 1)[1])
            wire_requests.append(task)
            assert schema["properties"]["mode"]["const"] == task["mode"]
            if task["mode"] == "review":
                output = result(
                    "固定版本的局部审查通过；仍需人工判断。",
                    mode="review",
                    verdict="passed",
                    next_action="finish",
                )["result"]
                assert schema["properties"]["verdict"]["type"] == "string"
            elif not task["previous_steps"]:
                output = result(
                    "保存计算与候选论证。",
                    [
                        action(
                            "calculate",
                            tool="polynomial_identity",
                            inputs={
                                "left": "2*(a**2+b**2+c**2-a*b-b*c-c*a)",
                                "right": "(a-b)**2+(b-c)**2+(c-a)**2",
                                "variables": ["a", "b", "c"],
                            },
                        ),
                        action(
                            "propose_proof",
                            conclusion_revision_id=claim["revision_id"],
                            body=r"差式为平方差和的一半，故非负；等号当且仅当 $a=b=c$。",
                        ),
                    ],
                )["result"]
            elif len(task["previous_steps"]) == 1:
                proof = task["previous_steps"][0]["actions"][1]["result"]
                output = result(
                    "请求独立会话审查此版本。",
                    [action("request_review", target_revision_id=proof["revision_id"])],
                    next_action="wait",
                )["result"]
            else:
                assert schema["properties"]["verdict"]["type"] == "null"
                if task["repair_output"] is None:
                    assert len(wire_requests) == 4
                    first = task["previous_steps"][0]["actions"]
                    ids = [
                        claim["revision_id"],
                        first[1]["result"]["revision_id"],
                        first[0]["result"]["revision_id"],
                        task["child_results"][0]["output_revision_id"],
                    ]
                    rebound_raw = fixture["raw_text"]
                    assert len(original["cited_revision_ids"]) == len(ids)
                    for old, new in zip(original["cited_revision_ids"], ids, strict=True):
                        rebound_raw = rebound_raw.replace(old, new)
                    output = json.loads(rebound_raw)
                    assert output["mode"] == "research" and output["verdict"] == "passed"
                else:
                    assert budget == 5 and len(wire_requests) == 5
                    assert task["repair_output"] == rebound_raw
                    output = json.loads(rebound_raw)
                    # This is a new, separately charged provider response, not
                    # adapter-side normalization of the rejected original.
                    output["verdict"] = None
            raw = rebound_raw if len(wire_requests) == 4 else json.dumps(output, ensure_ascii=False)
            return httpx.Response(
                200,
                json={
                    "id": f"fixture-{len(wire_requests)}",
                    "usage": {"total_tokens": 10},
                    "choices": [{"message": {"content": raw}, "finish_reason": "stop"}],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as provider_client:
            config = ProviderConfig(
                "deepseek", "synthetic-key", "synthetic-model", "https://mock.invalid", True
            )
            await HTTPWorker(
                client,
                providers=["deepseek"],
                concurrency=1,
                provider_factory=lambda _: RemoteProvider(config, provider_client),
            ).run(once=True)
        assert len(wire_requests) == budget
        assert sum(task["repair_output"] is not None for task in wire_requests) == budget - 4
        accounting = await api.get(f"/runs/{run['run_id']}/budget")
        assert accounting["spent"] == budget and accounting["unknown"] == 0
        assert len(accounting["requests"]) == budget
        failed = [
            request
            for request in accounting["requests"]
            if request["reason"] == "invalid_structured_output"
        ]
        assert len(failed) == 1
        calls = (await api.get(f"/runs/{run['run_id']}/calls"))["calls"]
        rejected = next(call for call in calls if call["request_id"] == failed[0]["request_id"])
        assert rejected["raw_text"] == rebound_raw and rejected["complete"] is True
        assert rejected["finish_reason"] == "stop" and rejected["result"] is None
        assert rejected["call_config"]["prompt_template_version"] == PROMPT_VERSION
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == budget - 2
        with app.state.database.sessions() as session:
            current = session.get(Run, run["run_id"])
            assert current.state == ("completed" if budget == 5 else "budget_exhausted")
            reviews = session.scalars(select(Review).where(Review.kind == "llm_review")).all()
            assert len(reviews) == 1  # Only the distinct reviewer can issue a verdict.
            assert session.scalars(select(Adoption)).all() == []
        if budget == 5:
            corrected = calls[-1]
            assert corrected["result"]["verdict"] is None
            assert corrected["attempt_id"] == rejected["attempt_id"]
            assert (
                corrected["call_config"]["prompt_sha256"]
                != rejected["call_config"]["prompt_sha256"]
            )

    exercise(app, scenario)
