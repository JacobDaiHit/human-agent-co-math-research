"""Reviewed answers must bind the final body to current, visible review evidence."""

import pytest
from mathagent.api.app import create_app
from mathagent.persistence.agent_models import AgentRun
from mathagent.persistence.models import Run
from mathagent.providers.remote import ProviderFailure
from mathagent.runtime.service import Runtime
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_autonomous_agent import action, exercise, project_and_run, result


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-completion-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")
    application = create_app(tmp_path / "completion.sqlite3", token="human", worker_token="worker")
    application.state.database.migrate()
    yield application
    application.state.database.close()


@pytest.mark.parametrize("body,scope,box_issue", [
    (r"An unsupported final answer $\boxed{2}$.", None, False),
    ("A draft without its final answer.", r"A tentative answer $\boxed{2}$.", True),
    (r"Either $\boxed{2}$ or $\boxed{3}$.", None, True),
])
def test_reviewed_answer_rejects_zero_review_and_nonfinal_boxes_with_feedback(app, body, scope, box_issue):
    seen = []

    class Script:
        async def generate(self, task):
            seen.append(task)
            if task["previous_steps"]:
                feedback = task["previous_steps"][-1]["actions"][-1]
                assert feedback["type"] == "finish" and feedback["status"] == "rejected"
                assert feedback["error"] == "completion_requirements_unmet"
            output = result(body, next_action="finish")
            output["result"]["scope"] = scope
            return output

    async def scenario(api, client):
        _, run = await project_and_run(api, completion_policy="reviewed_answer", max_steps=2)
        await HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0).run(once=True)
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == len(seen) == 2
        assert steps[0]["state"] == "queued" and steps[-1]["state"] == "step_limit"
        for step in steps:
            issues = step["actions"][-1]["issues"]
            assert "cite_current_passed_review_candidate_with_same_boxed_answer_required" in issues
            assert ("exactly_one_final_boxed_answer_in_body_required" in issues) is box_issue

    exercise(app, scenario)


def test_ordinary_draft_completion_remains_compatible_without_box_or_review(app):
    class Script:
        async def generate(self, task):
            assert task["completion_policy"] == "draft"
            return result("An explicitly unresolved ordinary research draft.", next_action="finish")

    async def scenario(api, client):
        _, run = await project_and_run(api)
        await HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0).run(once=True)
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == 1 and steps[0]["state"] == "completed"
        assert not steps[0]["actions"]

    exercise(app, scenario)


def test_legacy_completion_endpoint_cannot_bypass_reviewed_answer_checks(app):
    async def scenario(api, client):
        _, run = await project_and_run(api, completion_policy="reviewed_answer")
        task = (await api.write("/worker/claim-next", {"providers": ["fake"]}, worker=True))["task"]
        response = await api.write(f"/attempts/{task['attempt_id']}/complete", {
            "token": task["token"], "body": r"Unreviewed $\boxed{2}$.",
        }, worker=True, expect=422)
        assert response["error"] == "autonomous_step_required"
        assert (await api.get(f"/runs/{run['run_id']}/steps"))["steps"] == []

    exercise(app, scenario)


@pytest.mark.parametrize("condition", [
    "passed", "long_candidate", "candidate_truncated", "repeated_same_answer", "issues", "candidate_stale", "dependency_stale", "not_cited", "candidate_no_box", "different_answer", "review_excerpt",
])
def test_finish_requires_current_passed_visible_cited_candidate_and_matching_answer(app, condition, monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-completion-key")
    dependency = {}
    review_targets = []

    class Script:
        async def generate(self, task):
            if task["mode"] == "review":
                assert task["completion_policy"] == "draft" and task["autonomous"] is False
                review_targets.append(task["target_revision_id"])
                target_input = next(i for i in task["inputs"] if i["revision_id"] == task["target_revision_id"])
                if condition == "long_candidate":
                    assert len(target_input["body"]) > 12000 and not target_input["excerpted"]
                    assert target_input["body"].endswith(r"The candidate concludes $\boxed{2}$.")
                if condition == "candidate_truncated":
                    assert target_input["excerpted"]
                review_body = ("Long review details. " * 1200) if condition == "review_excerpt" else "An independent synthetic review of the exact candidate."
                return result(review_body, mode="review",
                    verdict="issues" if condition == "issues" else "passed", next_action="finish")
            history = task["previous_steps"]
            if not history:
                candidate = "A candidate without any final box." if condition == "candidate_no_box" else r"The candidate concludes $\boxed{2}$."
                if condition == "repeated_same_answer":
                    candidate = r"The calculation gives $\boxed{2}$. Thus the answer is $\boxed{2}$."
                if condition == "long_candidate":
                    candidate = "Synthetic derivation details.\n\n" * 600 + candidate
                if condition == "candidate_truncated":
                    candidate = "\\" * 95000 + candidate
                return result("Save the candidate for independent review.", [
                    action("write_draft", kind="claim", body=candidate),
                ], next_action="continue")
            candidate = history[0]["actions"][0]["result"]
            if len(history) == 1:
                return result("Review the saved candidate.", [action("request_review",
                    target_revision_id=candidate["revision_id"])], next_action="wait")
            assert review_targets == [candidate["revision_id"]]
            child = task["child_results"][0]
            assert child["state"] == "completed"
            assert child["output_revision_id"] in task["context_revision_ids"]
            actions = []
            if condition == "candidate_stale":
                actions = [action("revise_object", object_id=candidate["object_id"],
                    expected_revision_id=candidate["revision_id"], body=r"A changed candidate still saying $\boxed{2}$.")]
            if condition == "dependency_stale":
                actions = [action("revise_object", object_id=dependency["object_id"],
                    expected_revision_id=dependency["revision_id"], body="Changed context invalidates the prior review snapshot.")]
            final = r"The final answer is $\boxed{3}$." if condition == "different_answer" else r"The final answer is $\boxed{ 2 }$."
            output = result(final, actions, next_action="finish")
            output["result"]["cited_revision_ids"] = [] if condition == "not_cited" else [candidate["revision_id"]]
            return output

    async def scenario(api, client):
        project = await api.write("/projects", {"title": "Synthetic reviewed answer", "body": "Compute $1+1$."})
        await api.write(f"/projects/{project['project_id']}/runtime-settings", {
            "request_budget": 12, "allow_real_api": True, "allowed_providers": ["deepseek"]}, method="PUT")
        run = await api.write("/runs", {"branch_id": project["branch_id"], "goal_object_id": project["object_id"],
            "provider": "deepseek", "autonomous": True, "request_budget": 12,
            "completion_policy": "reviewed_answer", "max_steps": 3})
        dependency.update(await api.write("/objects", {"branch_id": project["branch_id"],
            "kind": "context", "body": "A context included in the candidate review."}))
        await HTTPWorker(client, providers=["deepseek"], provider_factory=lambda _: Script(), fake_delay_seconds=0).run(once=True)
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert len(steps) == 3
        succeeds = condition in {"passed", "repeated_same_answer", "long_candidate"}
        assert steps[-1]["state"] == ("completed" if succeeds else "step_limit"), steps[-1]["actions"]
        feedback = [entry for entry in steps[-1]["actions"] if entry["type"] == "finish"]
        if succeeds:
            assert feedback == []
        else:
            assert len(feedback) == 1 and feedback[0]["status"] == "rejected"
            assert feedback[0]["error"] == "completion_requirements_unmet"
            if condition == "candidate_truncated":
                assert "complete_review_material_must_be_delivered" in feedback[0]["issues"]
        snapshot = await api.get(f"/projects/{project['project_id']}/snapshot")
        assert len(snapshot["reviews"]) == 1
        if condition == "dependency_stale":
            assert snapshot["reviews"][0]["dependency_snapshot"][dependency["object_id"]] == dependency["revision_id"]
        assert (await api.get(f"/runs/{run['run_id']}/budget"))["spent"] == 4

    exercise(app, scenario)


@pytest.mark.parametrize("project_limit,branch_limit,root_limit,remaining", [
    (8, 6, 5, 3), (3, 6, 5, 1), (8, 3, 5, 1),
])
def test_request_allowance_uses_tightest_shared_scope_and_keeps_unknown_occupied(app,
                    project_limit, branch_limit, root_limit, remaining):
    captured = []

    class Script:
        async def generate(self, task):
            captured.append(task)
            if task["instruction"] == "Synthetic unknown child":
                raise ProviderFailure("synthetic_unknown", outcome="unknown")
            return result("Delegate one synthetic child.", [action("spawn_task",
                goal_object_id=task["goal_object_id"], instruction="Synthetic unknown child", request_budget=3)],
                next_action="wait")

    async def scenario(api, client):
        project, run = await project_and_run(api, request_budget=root_limit)
        await api.write(f"/projects/{project['project_id']}/runtime-settings", {
            "request_budget": project_limit, "allow_real_api": False, "allowed_providers": ["fake"]}, method="PUT")
        await api.write(f"/branches/{project['branch_id']}/runtime-settings", {
            "request_budget": branch_limit}, method="PUT")
        await HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0).run(once=True)
        assert len(captured) == 2
        assert captured[0]["request_budget_status"]["remaining"] == min(project_limit, branch_limit, root_limit)
        assert captured[1]["request_budget_status"]["remaining"] == min(project_limit - 1, branch_limit - 1, root_limit - 1, 3)
        accounting = await api.get(f"/projects/{project['project_id']}/budget")
        assert accounting["spent"] == 1 and accounting["unknown"] == 1 and accounting["occupied"] == 2
        with app.state.database.sessions() as session:
            root = session.get(Run, run["run_id"])
            status = Runtime(app.state.service).agent.request_budget_status(session, root)
            assert status["remaining"] == remaining
            assert status["after_this_request"] == max(0, remaining - 1)
            assert status["snapshot"] == "before_request_reservation"
            assert status["shared_with_descendants"] and status["includes_format_repairs"]
            scopes = {scope["scope"]: scope for scope in status["scopes"]}
            assert scopes["run"]["occupied"] == 1 and scopes["run"]["unknown"] == 0
            assert scopes["subtree"]["occupied"] == 2 and scopes["subtree"]["unknown"] == 1
            child_id = session.scalar(select(AgentRun.run_id).where(AgentRun.parent_run_id == root.id))
            child = session.get(Run, child_id)
            child_status = Runtime(app.state.service).agent.request_budget_status(session, child)
            assert child_status["remaining"] == min(2, remaining)

    exercise(app, scenario)
