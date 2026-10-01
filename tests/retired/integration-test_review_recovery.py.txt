"""Recovery of one-request review children through the real HTTP worker/API."""

import pytest
from mathagent.providers.remote import ProviderFailure
from mathagent.runtime.worker import HTTPWorker
from test_autonomous_agent import action, exercise, project_and_run, result
from test_autonomous_agent import app as upstream_app

app_fixture = upstream_app


@pytest.fixture(autouse=True)
def synthetic_deepseek(monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-review-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-review-model")


async def reviewed_run(api, budget):
    project, _ = await project_and_run(api, autonomous=False)
    await api.write(f"/projects/{project['project_id']}/runtime-settings", {
        "request_budget": budget, "allow_real_api": True, "allowed_providers": ["deepseek"],
    }, method="PUT")
    run = await api.write("/runs", {
        "branch_id": project["branch_id"], "goal_object_id": project["object_id"],
        "autonomous": True, "provider": "deepseek", "request_budget": budget,
        "completion_policy": "reviewed_answer", "unknown_recovery": "once", "max_steps": 3,
    })
    return project, run


def test_review_unknown_retries_once_then_allows_reviewed_completion(app_fixture):
    async def scenario(api, client):
        project, run = await reviewed_run(api, budget=5)
        review_calls = 0

        class Script:
            async def generate(self, task):
                nonlocal review_calls
                if task["mode"] == "review":
                    review_calls += 1
                    assert task["request_budget"] == 3
                    if review_calls == 1:
                        raise ProviderFailure("transport_read_error", outcome="unknown")
                    assert task["request_budget_status"]["remaining"] == 2
                    return result("The saved candidate is sound.", mode="review", verdict="passed",
                                  next_action="finish")
                if not task["previous_steps"]:
                    return result("Save a candidate.", [action(
                        "write_draft", kind="claim", body=r"The result is $\boxed{2}$.",
                    )], next_action="continue")
                candidate = task["previous_steps"][0]["actions"][0]["result"]
                if len(task["previous_steps"]) == 1:
                    return result("Request review.", [action(
                        "request_review", target_revision_id=candidate["revision_id"],
                    )], next_action="wait")
                assert task["child_results"][0]["state"] == "completed"
                final = result(r"The final answer is $\boxed{2}$.", next_action="finish")
                final["result"]["cited_revision_ids"] = [candidate["revision_id"]]
                return final

        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script()).run(once=True)
        budget = await api.get(f"/runs/{run['run_id']}/budget")
        assert review_calls == 2
        assert budget["occupied"] == 5 and budget["unknown"] == 1 and budget["spent"] == 4
        snapshot = await api.get(f"/projects/{project['project_id']}/snapshot")
        assert next(item for item in snapshot["runs"] if item["id"] == run["run_id"])["state"] == "completed"
    exercise(app_fixture, scenario)


def test_review_format_repair_uses_child_recovery_budget(app_fixture):
    async def scenario(api, client):
        _, run = await reviewed_run(api, budget=5)
        review_calls = 0

        class Script:
            async def generate(self, task):
                nonlocal review_calls
                if task["mode"] == "review":
                    review_calls += 1
                    assert task["request_budget"] == 3
                    if review_calls == 1:
                        raise ProviderFailure("invalid_structured_output", outcome="spent", observation={
                            "raw_text": "malformed review", "complete": True, "finish_reason": "stop",
                        })
                    assert task["repair_output"] == "malformed review"
                    return result("The saved candidate is sound.", mode="review", verdict="passed",
                                  next_action="finish")
                if not task["previous_steps"]:
                    return result("Save a candidate.", [action(
                        "write_draft", kind="claim", body=r"The result is $\boxed{2}$.",
                    )], next_action="continue")
                candidate = task["previous_steps"][0]["actions"][0]["result"]
                if len(task["previous_steps"]) == 1:
                    return result("Request review.", [action(
                        "request_review", target_revision_id=candidate["revision_id"],
                    )], next_action="wait")
                final = result(r"The final answer is $\boxed{2}$.", next_action="finish")
                final["result"]["cited_revision_ids"] = [candidate["revision_id"]]
                return final

        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script()).run(once=True)
        budget = await api.get(f"/runs/{run['run_id']}/budget")
        assert review_calls == 2
        assert budget["occupied"] == budget["spent"] == 5 and budget["unknown"] == 0
    exercise(app_fixture, scenario)


def test_review_unknown_retry_cannot_exceed_root_budget(app_fixture):
    async def scenario(api, client):
        _, run = await reviewed_run(api, budget=3)
        review_calls = 0

        class Script:
            async def generate(self, task):
                nonlocal review_calls
                if task["mode"] == "review":
                    review_calls += 1
                    raise ProviderFailure("transport_read_error", outcome="unknown")
                if not task["previous_steps"]:
                    return result("Save a candidate.", [action(
                        "write_draft", kind="claim", body=r"The result is $\boxed{2}$.",
                    )], next_action="continue")
                candidate = task["previous_steps"][0]["actions"][0]["result"]
                return result("Request review.", [action(
                    "request_review", target_revision_id=candidate["revision_id"],
                )], next_action="wait")

        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script()).run(once=True)
        budget = await api.get(f"/runs/{run['run_id']}/budget")
        assert review_calls == 1
        assert budget["occupied"] == 3 and budget["unknown"] == 1 and budget["spent"] == 2
    exercise(app_fixture, scenario)
