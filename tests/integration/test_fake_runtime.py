"""HTTP lifecycle acceptance with real SQLite persistence and explicit lease faults."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app
from mathagent.persistence.models import Attempt, Head, Revision, Run
from sqlalchemy import select


class RuntimeAPI:
    def __init__(self, client):
        self.client = client
        self.project = self.post("/projects", {"title": "Runtime acceptance", "body": "原始目标"})

    def request(self, path, payload=None, *, worker=False, key=None):
        return self.client.post(
            path,
            json=payload,
            headers={
                "Authorization": "Bearer worker" if worker else "Bearer human",
                "Idempotency-Key": key or str(uuid4()),
            },
        )

    def post(self, path, payload=None, *, worker=False, key=None):
        response = self.request(path, payload, worker=worker, key=key)
        assert response.status_code in {200, 201, 202}, response.text
        return response.json()

    def run(self, project=None, instruction="初始指令"):
        project = project or self.project
        return self.post(
            "/runs",
            {
                "branch_id": project["branch_id"],
                "goal_object_id": project["object_id"],
                "instruction": instruction,
            },
        )

    def claim(self, run):
        return self.post(f"/runs/{run['run_id']}/claim", worker=True)

    def complete(self, attempt, *, key=None, body=None):
        return self.post(
            f"/attempts/{attempt['attempt_id']}/complete",
            {
                "token": attempt["token"],
                "body": body or attempt["scripted_output"],
            },
            worker=True,
            key=key,
        )

    def intervention(self, run, action, instruction=""):
        return self.post(
            f"/runs/{run['run_id']}/interventions",
            {
                "action": action,
                "instruction": instruction,
            },
        )

    def snapshot(self, branch_id=None):
        response = self.client.get(
            f"/projects/{self.project['project_id']}/snapshot",
            params={"branch_id": branch_id or self.project["branch_id"]},
            headers={"Authorization": "Bearer human"},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def expire(self, attempt):
        with self.client.app.state.database.sessions.begin() as session:
            row = session.get(Attempt, attempt["attempt_id"])
            row.lease_until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()


@pytest.fixture
def runtime_api(tmp_path):
    app = create_app(tmp_path / "runtime.db", token="human", worker_token="worker")
    with TestClient(app) as client:
        yield RuntimeAPI(client)


def test_worker_credentials_and_execution_token_are_both_required(runtime_api):
    api = runtime_api
    run = api.run()
    assert api.request(f"/runs/{run['run_id']}/claim").status_code == 401
    attempt = api.claim(run)
    denied = api.request(
        f"/attempts/{attempt['attempt_id']}/complete",
        {
            "token": "wrong-token",
            "body": "not accepted",
        },
        worker=True,
    )
    assert denied.status_code == 409
    assert denied.json()["error"] == "invalid_execution_token"
    completed = api.complete(attempt)
    assert completed["state"] == "completed"
    assert not completed["quarantined"]
    snapshot = api.snapshot()
    artifact = next(o for o in snapshot["objects"] if o["kind"] == "artifact")
    assert artifact["revision"]["payload"]["simulated"] is True
    assert artifact["revision"]["payload"]["evidence_kind"] == "simulation"
    assert artifact["adoption_state"] == "draft"
    assert completed["output_revision_id"] in {b["revision_id"] for b in snapshot["manuscript"]}
    assert "token" not in snapshot["runs"][0]["attempts"][0]


def test_editing_input_isolates_old_output_and_resume_reads_new_version(runtime_api):
    api = runtime_api
    run = api.run()
    old = api.claim(run)
    revision = api.post(
        f"/objects/{api.project['object_id']}/revisions",
        {
            "branch_id": api.project["branch_id"],
            "expected_revision_id": api.project["revision_id"],
            "body": "人工修改的新目标",
        },
    )
    completed = api.complete(old)
    assert completed["state"] == "interrupted"
    assert completed["quarantined"]
    assert "input_revision_changed" in completed["reasons"]
    assert completed["output_branch_id"] != api.project["branch_id"]
    main = api.snapshot()
    assert completed["output_revision_id"] not in {o["revision"]["id"] for o in main["objects"]}
    candidate = api.snapshot(completed["output_branch_id"])
    goal = next(o for o in candidate["objects"] if o["id"] == api.project["object_id"])
    assert goal["revision"]["id"] == api.project["revision_id"]
    assert completed["output_revision_id"] in {b["revision_id"] for b in candidate["manuscript"]}
    api.post(f"/runs/{run['run_id']}/resume")
    fresh = api.claim(run)
    assert fresh["attempt_number"] == 2
    assert fresh["token"] != old["token"]
    assert fresh["read_set"][api.project["object_id"]] == revision["revision_id"]
    assert "人工修改的新目标" in fresh["scripted_output"]
    assert not api.complete(fresh)["quarantined"]


def test_unrelated_additions_do_not_invalidate_existing_input_revisions(runtime_api):
    api = runtime_api
    attempt = api.claim(api.run())
    api.post(
        "/objects",
        {
            "branch_id": api.project["branch_id"],
            "kind": "context",
            "body": "无关的新对象",
        },
    )
    assert not api.complete(attempt)["quarantined"]


@pytest.mark.parametrize(
    "action,requested,effective",
    [
        ("pause", "pause_requested", "paused"),
        ("cancel", "cancel_requested", "cancelled"),
    ],
)
def test_stop_receipt_reports_requested_then_effective_boundary(
    runtime_api, action, requested, effective
):
    api = runtime_api
    run = api.run()
    attempt = api.claim(run)
    receipt = api.intervention(run, action)
    assert receipt["state"] == requested
    assert receipt["effect"] == "pending"
    assert receipt["in_flight"] is True
    assert receipt["effective_at"] == "attempt_boundary"
    assert api.request(f"/runs/{run['run_id']}/claim", worker=True).status_code == 409
    assert api.request(f"/runs/{run['run_id']}/resume").status_code == 409
    completed = api.complete(attempt)
    assert completed["state"] == effective
    assert completed["quarantined"]
    if action == "pause":
        assert api.post(f"/runs/{run['run_id']}/resume")["state"] == "queued"
        resumed = api.claim(run)
        assert resumed["attempt_number"] == 2
        assert resumed["attempt_id"] != attempt["attempt_id"]
    else:
        assert api.request(f"/runs/{run['run_id']}/resume").status_code == 409


def test_steering_retains_attempt_inputs_and_applies_on_next_claim(runtime_api):
    api = runtime_api
    run = api.run()
    old = api.claim(run)
    receipt = api.intervention(run, "steer", "改为寻找反例")
    assert receipt["state"] == "steer_requested"
    assert receipt["effect"] == "pending"
    with api.client.app.state.database.sessions() as session:
        assert session.get(Attempt, old["attempt_id"]).checkpoint["instruction"] == "初始指令"
    completed = api.complete(old)
    assert completed["state"] == "queued"
    assert completed["quarantined"]
    new = api.claim(run)
    assert new["control_epoch"] > old["control_epoch"]
    assert "改为寻找反例" in new["scripted_output"]
    assert not api.complete(new)["quarantined"]


def test_completion_retries_save_exactly_one_output_even_with_new_command_key(runtime_api):
    api = runtime_api
    attempt = api.claim(api.run())
    key = str(uuid4())
    first = api.complete(attempt, key=key)
    assert api.complete(attempt, key=key) == first
    assert api.complete(attempt) == first
    changed = api.request(
        f"/attempts/{attempt['attempt_id']}/complete",
        {
            "token": attempt["token"],
            "body": "A different completion must not overwrite the output.",
        },
        worker=True,
    )
    assert changed.status_code == 409
    with api.client.app.state.database.sessions() as session:
        artifacts = [r for r in session.scalars(select(Revision)) if r.author == "fake"]
        assert len(artifacts) == 1
        assert artifacts[0].id == first["output_revision_id"]


def test_global_concurrency_limit_keeps_pending_stops_in_occupied_slots(runtime_api):
    api = runtime_api
    first_run = api.run()
    first = api.claim(first_run)
    second_project = api.post("/projects", {"title": "Another project", "body": "Another goal"})
    second = api.claim(api.run(second_project))
    additional = [api.claim(api.run()) for _ in range(2)]
    third_run = api.run()
    full = api.request(f"/runs/{third_run['run_id']}/claim", worker=True)
    assert full.status_code == 409
    assert full.json()["error"] == "concurrency_limit"
    api.intervention(first_run, "pause")
    assert api.request(f"/runs/{third_run['run_id']}/claim", worker=True).status_code == 409
    api.complete(first)
    third = api.claim(third_run)
    assert third["state"] == "running"
    api.complete(second)
    for attempt in additional:
        api.complete(attempt)


def test_expired_fake_attempt_recovers_without_old_worker_overwriting_new_attempt(runtime_api):
    api = runtime_api
    run = api.run()
    old = api.claim(run)
    api.expire(old)
    new = api.claim(run)
    assert new["attempt_number"] == 2
    assert new["token"] != old["token"]
    late = api.complete(old)
    assert late["quarantined"]
    assert late["state"] == "running"
    assert {"lease_expired", "superseded_attempt"}.issubset(late["reasons"])
    with api.client.app.state.database.sessions() as session:
        assert session.get(Run, run["run_id"]).current_attempt_id == new["attempt_id"]
        assert session.get(Attempt, old["attempt_id"]).checkpoint["reconciled"] is True
        assert (
            late["output_revision_id"]
            not in session.scalars(
                select(Head.revision_id).where(Head.branch_id == api.project["branch_id"])
            ).all()
        )
    assert not api.complete(new)["quarantined"]


@pytest.mark.parametrize("action,effective", [("pause", "paused"), ("cancel", "cancelled")])
def test_late_result_cannot_undo_effective_stop_after_lease_reconciliation(
    runtime_api, action, effective
):
    api = runtime_api
    run = api.run()
    old = api.claim(run)
    api.intervention(run, action)
    api.expire(old)
    reconciled = api.claim(run)
    assert reconciled["state"] == effective
    late = api.complete(old)
    assert late["state"] == effective
    assert late["quarantined"]


def test_steering_paused_run_never_implicitly_resumes_it(runtime_api):
    api = runtime_api
    run = api.run()
    assert api.intervention(run, "pause")["state"] == "paused"
    assert api.intervention(run, "steer", "下次执行的新指令")["state"] == "paused"
    assert api.request(f"/runs/{run['run_id']}/claim", worker=True).status_code == 409
    api.post(f"/runs/{run['run_id']}/resume")
    assert "下次执行的新指令" in api.claim(run)["scripted_output"]
