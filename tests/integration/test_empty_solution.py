"""Synthetic controller coverage for empty and unresolved solution sets."""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import httpx
import pytest
from mathagent.persistence.models import Run
from mathagent.runtime.worker import HTTPWorker
from test_bounded_search import app as bounded_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    yield from bounded_app.__wrapped__(tmp_path, monkeypatch)


def op(kind, **arguments):
    return {"type": kind, "arguments": arguments}


async def setup_solution_problem(app, problem, config):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000",
        headers={"Authorization": "Bearer worker"},
    )

    async def write(path, payload, method="POST", worker=False):
        response = await client.request(method, path, json=payload, headers={
            "Authorization": "Bearer worker" if worker else "Bearer human",
            "Idempotency-Key": str(uuid4()),
        })
        assert response.is_success, response.text
        return response.json()

    project = await write("/projects", {"title": "Empty-set controller fixture", "body": problem})
    await write(f"/projects/{project['project_id']}/runtime-settings", {
        "allow_real_api": True, "allowed_providers": ["fake", "deepseek"], "request_budget": 100,
    }, "PUT")
    root = await write("/runs", {
        "branch_id": project["branch_id"], "goal_object_id": project["object_id"],
        "provider": "deepseek", "autonomous": True, "request_budget": 12,
        "max_output_tokens": 4096, "cumulative_output_token_budget": 32768,
        "solver_controller": "bounded_search_v1", "search_config": config,
        "completion_policy": "reviewed_answer",
    })
    return client, root


async def search_state(client, root_id):
    response = await client.get(f"/runs/{root_id}/search", headers={"Authorization": "Bearer human"})
    assert response.is_success, response.text
    return response.json()


async def run_until_terminated(worker, client, root_id, limit=20):
    for _ in range(limit):
        await worker.run(once=True)
        snapshot = await search_state(client, root_id)
        if snapshot["session"]["phase"] == "terminated":
            return snapshot
    return await search_state(client, root_id)


class SolutionSetSolver:
    """Routes solely from the supplied problem text; it never invokes a remote API."""

    def __init__(self, expected_problem):
        self.expected_problem = expected_problem
        self.tasks = []

    async def generate(self, task):
        self.tasks.append(task)
        problem = next(item["body"] for item in task["inputs"] if item["body"] == self.expected_problem)
        assert problem == self.expected_problem
        kind = task["search_context"]["kind"]
        finite_only = "finite-only" in problem
        solvable = r"n^2=1" in problem
        if kind == "analysis":
            reduction = (r"For every $n\in\mathbb Z_{>0}$, solve $n^2=1$." if solvable
                         else r"For every $n\in\mathbb Z_{>0}$, compare $n^2+1$ with $0$.")
            lemma = (r"$n\in\mathbb Z_{>0}\land n^2=1\Rightarrow n=1$" if solvable
                     else r"$n\in\mathbb Z_{>0}\Rightarrow n^2\geq1$")
            subgoal = (r"Classify all $n\in\mathbb Z_{>0}$ satisfying $n^2=1$." if solvable
                       else r"Classify all $n\in\mathbb Z_{>0}$ satisfying $n^2+1=0$.")
            title = "Domain-wide classification" if solvable else "Domain-wide contradiction"
            return self.result(
                "Derive a condition that covers every allowed value.",
                [op("propose_routes", routes=[{
                    "title": title,
                    "core_reduction": reduction,
                    "key_lemmas": [lemma],
                    "assumptions": [],
                    "subgoal": subgoal,
                    "cheap_check": r"Substitute $n=1$.",
                }])], mode=task["mode"],
            )
        if kind == "advance":
            if "unresolved" in problem:
                return self.result(
                    r"The attempted finite search leaves all $n>B$ unresolved.",
                    [op("report_progress", status="stalled")], mode=task["mode"],
                )
            if finite_only:
                return self.result(
                    r"Checking $1\leq n\leq10$ finds no solution, so this incomplete claim proposes $\boxed{\varnothing}$.",
                    [op("report_progress", status="candidate")], mode=task["mode"],
                )
            if solvable:
                return self.result(
                    r"Since $n\in\mathbb Z_{>0}$ and $n^2=1$, $n=1$; hence $\boxed{\{1\}}$.",
                    [op("report_progress", status="candidate")], mode=task["mode"],
                )
            return self.result(
                r"For every $n\in\mathbb Z_{>0}$, $n^2\geq1$, so $n^2+1\geq2>0$. Thus $\boxed{\varnothing}$.",
                [op("report_progress", status="candidate")], mode=task["mode"],
            )
        if kind == "check":
            candidate = next(item for item in task["inputs"] if item["revision_id"] == task["target_revision_id"])
            if finite_only:
                return self.result(
                    r"The search checks only $1\leq n\leq10$, leaving $n>10$ outside its scope.",
                    [], verdict="issues", scope=r"Review the claimed coverage of $\mathbb Z_{>0}$.",
                    gaps=[{
                        "kind": "quantifier", "anchor": r"$1\leq n\leq10$",
                        "detail": r"A finite search does not cover all $n\in\mathbb Z_{>0}$.",
                        "evidence_revision_ids": [],
                    }], mode=task["mode"],
                )
            expected_box = r"\boxed{\{1\}}" if solvable else r"\boxed{\varnothing}"
            assert expected_box in candidate["body"]
            return self.result(
                r"The argument covers the original domain and has no remaining gap.", [], verdict="passed",
                scope=r"Review the candidate over all $n\in\mathbb Z_{>0}$.", mode=task["mode"],
            )
        if kind == "final":
            candidate_id = task["search_context"]["route"]["candidate_revision_id"]
            answer = r"\boxed{\{1\}}" if solvable else r"\boxed{\varnothing}"
            return self.result(r"The reviewed solution set is $" + answer + r"$.", [], cited=[candidate_id], mode=task["mode"])
        raise AssertionError(kind)

    @staticmethod
    def result(body, actions, *, verdict=None, scope=None, gaps=None, cited=None, mode="research"):
        return {"result": {
            "mode": mode, "body": body, "findings": ["Synthetic controller fixture."],
            "verdict": verdict, "scope": scope, "actions": actions, "structured_gaps": gaps or [],
            "next_action": "finish", "cited_revision_ids": cited or [],
        }, "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}


@pytest.mark.parametrize("case,problem,config", [
    ("proved_empty", r"Find all positive integers $n$ satisfying $n^2+1=0$.",
     {"max_routes": 1, "active_routes": 1}),
    ("finite_inconclusive", r"Find all positive integers $n$ satisfying $n^2+1=0$ (finite-only fixture).",
     {"max_routes": 1, "active_routes": 1, "max_repairs": 0}),
    ("unresolved", r"Find all positive integers $n$ satisfying $n^2+1=0$ (unresolved fixture).",
     {"max_routes": 1, "active_routes": 1}),
    ("solvable", r"Find all positive integers $n$ satisfying $n^2=1$.",
     {"max_routes": 1, "active_routes": 1}),
])
def test_solution_set_outcomes_keep_empty_candidates_distinct_from_unresolved(app, case, problem, config):
    async def scenario():
        client, root = await setup_solution_problem(app, problem, config)
        solver = SolutionSetSolver(problem)
        worker = HTTPWorker(client, provider_factory=lambda _: solver, providers=["deepseek"], fake_delay_seconds=0)
        async with aclosing(client):
            snapshot = await run_until_terminated(worker, client, root["run_id"])
            assert snapshot["session"]["phase"] == "terminated", snapshot
            steps = (await client.get(f"/runs/{root['run_id']}/steps", headers={"Authorization": "Bearer human"})).json()["steps"]
            route = snapshot["routes"][0]
            kinds = [task["search_context"]["kind"] for task in solver.tasks]

            if case == "proved_empty":
                assert route["candidate_revision_id"] is not None
                assert route["progress"]["review_passed"] is True
                assert kinds[-1] == "final"
                assert any(r"\boxed{\varnothing}" in item["body"] for item in solver.tasks[-1]["inputs"])
                assert steps[-1]["state"] == "completed"
                assert steps[-1]["receipt"]["completion_checks"]["passed"] is True
            elif case == "finite_inconclusive":
                assert route["candidate_revision_id"] is not None
                assert route["progress"]["review_passed"] is False
                assert snapshot["gaps"][0]["kind"] == "quantifier"
                assert steps[-1]["state"] != "completed"
                assert steps[-1]["receipt"]["completion_checks"]["passed"] is False
            elif case == "unresolved":
                assert route["candidate_revision_id"] is None
                assert "final" not in kinds
                assert not [work for work in snapshot["works"] if work["kind"] == "final"]
                with app.state.database.sessions() as session:
                    assert session.get(Run, root["run_id"]).state == "step_limit"
            else:
                assert route["candidate_revision_id"] is not None
                assert route["progress"]["review_passed"] is True
                assert kinds[-1] == "final"
                assert steps[-1]["state"] == "completed"
                assert steps[-1]["receipt"]["completion_checks"]["passed"] is True

    asyncio.run(scenario())
