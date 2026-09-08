"""Offline acceptance for the independent worker, durable budget, and real-provider gate."""

import asyncio
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app
from mathagent.persistence.models import Attempt, Review, Run
from mathagent.providers.remote import ProviderFailure
from mathagent.runtime.service import Runtime
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_http_transport import http_service as http_service


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "worker.sqlite3", token="human", worker_token="worker")
    app.state.database.migrate()
    yield app
    app.state.database.close()


class API:
    def __init__(self, client):
        self.client = client
        self.project = None

    async def post(self, path, payload=None, *, human=False, expected=None, key=None):
        response = await self.client.post(
            path,
            json=payload,
            headers={
                "Authorization": "Bearer human" if human else "Bearer worker",
                "Idempotency-Key": key or str(uuid4()),
            },
        )
        assert response.status_code == expected if expected else response.is_success, response.text
        return response.json()

    async def get(self, path):
        response = await self.client.get(path, headers={"Authorization": "Bearer human"})
        assert response.is_success, response.text
        return response.json()

    async def settings(self, **overrides):
        response = await self.client.put(
            f"/projects/{self.project['project_id']}/runtime-settings",
            json={
                "request_budget": 100,
                "allow_real_api": False,
                "allowed_providers": ["fake"],
                **overrides,
            },
            headers={"Authorization": "Bearer human", "Idempotency-Key": str(uuid4())},
        )
        assert response.is_success, response.text
        return response.json()

    async def run(self, **overrides):
        return await self.post(
            "/runs",
            {
                "branch_id": self.project["branch_id"],
                "goal_object_id": self.project["object_id"],
                **overrides,
            },
            human=True,
        )

    async def claim(self, run):
        return await self.post(f"/runs/{run['run_id']}/claim")

    async def reserve(self, task):
        return await self.post(f"/attempts/{task['attempt_id']}/requests", {"token": task["token"]})

    async def start(self, request, task):
        return await self.post(f"/requests/{request['request_id']}/start", {"token": task["token"]})

    async def budget(self):
        return await self.get(f"/projects/{self.project['project_id']}/budget")


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer worker"},
        ) as client:
            api = API(client)
            api.project = await api.post(
                "/projects", {"title": "Offline worker", "body": "Prove x=x"}, human=True
            )
            await scenario(api)

    asyncio.run(run())


def real_config(monkeypatch):
    # Only synthetic local test values. Adapters in these tests are injected mocks.
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-test-model")


def test_gate_and_worker_permissions(app, monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "0")

    async def scenario(api):
        status = await api.get("/providers/status")
        assert status["real_api_gate"] is False
        await api.post(
            "/runs",
            {
                "branch_id": api.project["branch_id"],
                "goal_object_id": api.project["object_id"],
                "provider": "deepseek",
            },
            human=True,
            expected=403,
        )
        await api.settings(allow_real_api=True, allowed_providers=["fake", "deepseek"])
        await api.post(
            "/runs",
            {
                "branch_id": api.project["branch_id"],
                "goal_object_id": api.project["object_id"],
                "provider": "deepseek",
            },
            human=True,
            expected=403,
        )
        await api.post("/worker/claim-next", {"providers": ["fake"]}, human=True, expected=401)
        denied = await api.client.get(f"/projects/{api.project['project_id']}/budget")
        assert denied.status_code == 401

    exercise(app, scenario)


def test_parallel_worker_applies_cancel_and_steer_at_boundaries(app):
    async def scenario(api):
        first, second = await api.run(), await api.run(mode="review")
        worker = HTTPWorker(api.client, fake_delay_seconds=0.3)
        work = asyncio.create_task(worker.run(once=True))
        async with asyncio.timeout(5):
            while (await api.budget())["dispatched"] != 2:
                await asyncio.sleep(0.01)
        await api.post(
            f"/runs/{first['run_id']}/interventions",
            {"action": "steer", "instruction": "Find a counterexample instead"},
            human=True,
        )
        await api.post(f"/runs/{second['run_id']}/interventions", {"action": "cancel"}, human=True)
        await asyncio.wait_for(work, timeout=10)
        with app.state.database.sessions() as session:
            assert session.get(Run, first["run_id"]).state == "completed"
            assert session.get(Run, second["run_id"]).state == "cancelled"
            attempts = session.scalars(
                select(Attempt).where(Attempt.run_id == first["run_id"]).order_by(Attempt.number)
            ).all()
            assert [a.state for a in attempts] == ["quarantined", "completed"]
            assert attempts[1].checkpoint["instruction"] == "Find a counterexample instead"
            assert session.scalars(select(Review)).all() == []
        budget = await api.budget()
        assert budget["spent"] == 3 and budget["unknown"] == 0

    exercise(app, scenario)


def test_project_budget_serializes_concurrent_reservations_and_release(app):
    async def scenario(api):
        await api.settings(request_budget=1)
        one, two = await api.claim(await api.run()), await api.claim(await api.run())
        requests = await asyncio.gather(api.reserve(one), api.reserve(two))
        assert sum(r["continue"] for r in requests) == 1
        index = next(i for i, r in enumerate(requests) if r["continue"])
        task = [one, two][index]
        reserved = requests[index]
        assert (await api.budget())["remaining"] == 0
        await api.post(f"/runs/{task['run_id']}/interventions", {"action": "pause"}, human=True)
        boundary = await api.start(reserved, task)
        assert boundary["continue"] is False
        budget = await api.budget()
        assert budget["released"] == 1 and budget["remaining"] == 1

    exercise(app, scenario)


@pytest.mark.parametrize(
    "ledger_state,expected",
    [("reserved", "running"), ("dispatched", "reconciliation_required"), ("spent", "paused")],
)
def test_restart_recovery_distinguishes_unaccepted_unknown_and_spent(
    app, monkeypatch, ledger_state, expected
):
    real_config(monkeypatch)

    async def scenario(api):
        await api.settings(allow_real_api=True, allowed_providers=["fake", "deepseek"])
        run = await api.run(provider="deepseek")
        task = await api.claim(run)
        request = await api.reserve(task)
        if ledger_state != "reserved":
            await api.start(request, task)
        if ledger_state == "spent":
            await api.post(
                f"/requests/{request['request_id']}/settle",
                {"token": task["token"], "outcome": "spent"},
            )
        with app.state.database.sessions.begin() as session:
            session.get(Attempt, task["attempt_id"]).lease_until = (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
        # A new worker's claim-next transaction performs durable lease recovery.
        next_task = await api.post("/worker/claim-next", {"providers": ["deepseek"]})
        with app.state.database.sessions() as session:
            assert session.get(Run, run["run_id"]).state == expected
        budget = await api.budget()
        if ledger_state == "reserved":
            assert budget["released"] == 1
            assert next_task["task"]["attempt_number"] == 2
        elif ledger_state == "spent":
            assert next_task["task"] is None and budget["spent"] == 1
        else:
            assert next_task["task"] is None and budget["unknown"] == 1
            assert (await api.post(f"/runs/{run['run_id']}/resume", human=True))[
                "effect"
            ] == "blocked"
            reconciled = await api.post(
                f"/requests/{request['request_id']}/reconcile",
                {"outcome": "spent", "reason": "Checked vendor receipt manually"},
                human=True,
            )
            assert reconciled["run_state"] == "paused"
            assert (await api.budget())["spent"] == 1
            await api.post(f"/runs/{run['run_id']}/resume", human=True)
            assert (await api.post("/worker/claim-next", {"providers": ["deepseek"]}))["task"]

    exercise(app, scenario)


@pytest.mark.parametrize(
    "outcome,retryable,count,state",
    [
        ("unaccepted", True, 3, "failed"),
        ("unknown", False, 1, "reconciliation_required"),
        ("spent", False, 1, "failed"),
    ],
)
def test_worker_retries_only_confirmed_unaccepted_requests(
    app, monkeypatch, outcome, retryable, count, state
):
    real_config(monkeypatch)
    calls = []

    class MockProvider:
        async def generate(self, task):
            calls.append(task["attempt_id"])
            raise ProviderFailure(
                "synthetic_provider_failure", outcome=outcome, retryable=retryable
            )

    async def scenario(api):
        await api.settings(allow_real_api=True, allowed_providers=["deepseek"])
        run = await api.run(provider="deepseek", request_budget=1)
        await HTTPWorker(
            api.client, providers=["deepseek"], provider_factory=lambda _: MockProvider()
        ).run(once=True)
        with app.state.database.sessions() as session:
            assert session.get(Run, run["run_id"]).state == state
        assert len(calls) == count
        budget = await api.budget()
        assert len(budget["requests"]) == count
        assert budget["occupied"] == (0 if outcome == "unaccepted" else 1)

    exercise(app, scenario)


def test_real_review_records_exact_target_and_historical_dependencies(app, monkeypatch):
    real_config(monkeypatch)
    observed = []

    class MockProvider:
        async def generate(self, task):
            observed.append(task)
            return {
                "result": {
                    "mode": "review",
                    "body": "Candidate review of pinned proof",
                    "verdict": "passed",
                    "scope": "Only algebraic equality; no whole-proof certification",
                    "findings": ["The displayed equality is reflexive"],
                    "cited_revision_ids": task["context_revision_ids"],
                },
                "usage": {"total_tokens": 10},
            }

    async def scenario(api):
        branch = api.project["branch_id"]
        claim = await api.post(
            "/objects", {"branch_id": branch, "kind": "claim", "body": "x=x"}, human=True
        )
        context = await api.post(
            "/objects",
            {
                "branch_id": branch,
                "kind": "context",
                "body": "Old exact definition",
                "payload": {"role": "definition"},
            },
            human=True,
        )
        proof = await api.post(
            "/proof-plans",
            {
                "branch_id": branch,
                "conclusion_revision_id": claim["revision_id"],
                "body": "Equality is reflexive",
                "context_revision_ids": [context["revision_id"]],
            },
            human=True,
        )
        changed = await api.post(
            f"/objects/{context['object_id']}/revisions",
            {
                "branch_id": branch,
                "expected_revision_id": context["revision_id"],
                "body": "New definition",
            },
            human=True,
        )
        await api.settings(allow_real_api=True, allowed_providers=["deepseek"])
        await api.run(goal_object_id=proof["object_id"], provider="deepseek", mode="review")
        await HTTPWorker(
            api.client, providers=["deepseek"], provider_factory=lambda _: MockProvider()
        ).run(once=True)
        task = observed[0]
        assert task["target_revision_id"] == proof["revision_id"]
        assert task["proof_plans"][0]["conclusion_revision_id"] == claim["revision_id"]
        assert {i["revision_id"] for i in task["inputs"]} >= {
            context["revision_id"],
            changed["revision_id"],
        }
        old = next(i for i in task["inputs"] if i["revision_id"] == context["revision_id"])
        assert old["body"] == "Old exact definition"
        with app.state.database.sessions() as session:
            reviews = session.scalars(select(Review)).all()
            assert len(reviews) == 1
            review = reviews[0]
            assert review.kind == "llm_review" and review.author == "deepseek"
            assert review.coverage == "partial"
            assert review.target_revision_id == proof["revision_id"]
            assert review.dependency_snapshot[context["object_id"]] == context["revision_id"]
        snapshot = await api.get(f"/projects/{api.project['project_id']}/snapshot")
        artifact = next(o for o in snapshot["objects"] if o["kind"] == "artifact")
        assert artifact["adoption_state"] == "draft"
        assert artifact["revision"]["payload"]["candidate"] is True
        target_reviews = [
            r for r in snapshot["reviews"] if r["target_revision_id"] == proof["revision_id"]
        ]
        assert target_reviews[0]["coverage"] == "partial"

    exercise(app, scenario)


def test_lost_reservation_response_replays_one_ledger_record(app):
    async def scenario(api):
        task = await api.claim(await api.run())
        original = api.client.post
        lost = False

        async def flaky(path, **kwargs):
            nonlocal lost
            response = await original(path, **kwargs)
            if path.endswith("/requests") and not lost:
                lost = True
                raise httpx.ReadTimeout("Lost response after committed reservation")
            return response

        api.client.post = flaky
        await HTTPWorker(api.client, fake_delay_seconds=0).execute(task)
        budget = await api.budget()
        assert budget["spent"] == 1 and len(budget["requests"]) == 1

    exercise(app, scenario)


def test_interrupted_fake_dispatch_releases_reservation_without_external_reconciliation(app):
    async def scenario(api):
        task = await api.claim(await api.run())
        request = await api.reserve(task)
        await api.start(request, task)
        await api.post(
            f"/attempts/{task['attempt_id']}/complete",
            {"token": task["token"], "body": task["scripted_output"]},
            expected=409,
        )
        failed = await api.post(
            f"/attempts/{task['attempt_id']}/fail",
            {"token": task["token"], "reason": "worker_shutdown"},
        )
        assert failed["state"] == "failed"
        budget = await api.budget()
        assert budget["released"] == 1 and budget["unknown"] == 0 and budget["occupied"] == 0

    exercise(app, scenario)


def test_worker_heartbeat_keeps_short_lease_alive(tmp_path, monkeypatch):
    initialize = Runtime.__init__

    def short_lease(self, service):
        initialize(self, service, lease_seconds=1)

    monkeypatch.setattr(Runtime, "__init__", short_lease)
    app = create_app(tmp_path / "heartbeat.db", token="human", worker_token="worker")
    app.state.database.migrate()

    async def scenario(api):
        run = await api.run()
        await HTTPWorker(api.client, concurrency=1, fake_delay_seconds=1.4).run(once=True)
        with app.state.database.sessions() as session:
            row = session.get(Run, run["run_id"])
            assert row.state == "completed"
            attempt = session.get(Attempt, row.current_attempt_id)
            assert attempt.state == "completed"
            assert attempt.checkpoint["completion"]["quarantined"] is False

    try:
        exercise(app, scenario)
    finally:
        app.state.database.close()


def test_cancelled_real_review_never_changes_target_evidence(app, monkeypatch):
    real_config(monkeypatch)

    async def scenario(api):
        await api.settings(allow_real_api=True, allowed_providers=["deepseek"])
        run = await api.run(provider="deepseek", mode="review")

        class MockProvider:
            async def generate(self, task):
                await api.post(
                    f"/runs/{run['run_id']}/interventions", {"action": "cancel"}, human=True
                )
                return {
                    "result": {
                        "mode": "review",
                        "body": "Cancelled review candidate",
                        "verdict": "issues",
                        "scope": "Only the assigned target",
                        "findings": ["Potential gap"],
                        "cited_revision_ids": [],
                    },
                    "usage": {},
                }

        await HTTPWorker(
            api.client, providers=["deepseek"], provider_factory=lambda _: MockProvider()
        ).run(once=True)
        with app.state.database.sessions() as session:
            row = session.get(Run, run["run_id"])
            assert row.state == "cancelled"
            assert session.get(Attempt, row.current_attempt_id).state == "quarantined"
            assert session.scalars(select(Review)).all() == []
        assert (await api.budget())["spent"] == 1

    exercise(app, scenario)


def test_unknown_cancellation_stays_cancelled_after_reconciliation(app, monkeypatch):
    real_config(monkeypatch)

    async def scenario(api):
        await api.settings(allow_real_api=True, allowed_providers=["deepseek"])
        run = await api.run(provider="deepseek")
        task = await api.claim(run)
        request = await api.reserve(task)
        await api.start(request, task)
        await api.post(f"/runs/{run['run_id']}/interventions", {"action": "cancel"}, human=True)
        await api.post(
            f"/requests/{request['request_id']}/settle",
            {"token": task["token"], "outcome": "unknown"},
        )
        reconciled = await api.post(
            f"/requests/{request['request_id']}/reconcile",
            {"outcome": "unaccepted", "reason": "Provider confirmed request was not accepted"},
            human=True,
        )
        assert reconciled["run_state"] == "cancelled"
        await api.post(f"/runs/{run['run_id']}/resume", human=True, expected=409)

    exercise(app, scenario)


def test_independent_worker_process_executes_two_overlapping_fake_calls(http_service, tmp_path):
    client = http_service
    human = {"Authorization": "Bearer http-transport-test-token"}

    def post(path, payload):
        response = client.post(
            path, json=payload, headers={**human, "Idempotency-Key": str(uuid4())}
        )
        assert response.is_success, response.text
        return response.json()

    project = post("/projects", {"title": "Independent worker", "body": "x=x"})
    for _ in range(2):
        post("/runs", {"branch_id": project["branch_id"], "goal_object_id": project["object_id"]})
    workspace = Path(__file__).resolve().parents[2]
    interpreter = workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    process = subprocess.run(
        [
            str(interpreter),
            "-m",
            "mathagent.runtime.worker",
            "--api-url",
            str(client.base_url),
            "--once",
            "--fake-delay",
            "0.5",
            "--concurrency",
            "2",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "MATHAGENT_ENABLE_REAL_API": "0",
            "MATHAGENT_WORKER_TOKEN": "http-transport-distinct-worker-token",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    snapshot = client.get(f"/projects/{project['project_id']}/snapshot", headers=human).json()
    assert [r["state"] for r in snapshot["runs"]] == ["completed", "completed"]
    events = client.get(f"/projects/{project['project_id']}/events", headers=human).json()["events"]
    dispatched = [e["seq"] for e in events if e["type"] == "request.dispatched"]
    completed = [e["seq"] for e in events if e["type"] == "attempt.completed"]
    assert len(dispatched) == len(completed) == 2
    assert max(dispatched) < min(completed), (
        "Both independent slots must start before either finishes"
    )
