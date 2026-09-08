"""Human workspace contracts exercised over HTTP with transactional SQLite storage."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app

TOKEN = "workspace-human-test"
WORKER_TOKEN = "workspace-worker-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@dataclass
class WorkspaceAPI:
    client: TestClient
    project_id: str
    branch_id: str

    def request(self, path, payload, *, key=None, method="POST"):
        return self.client.request(
            method,
            path,
            json=payload,
            headers={
                **AUTH,
                "Idempotency-Key": key or str(uuid4()),
            },
        )

    def write(self, path, payload, *, status=201, **options):
        response = self.request(path, payload, **options)
        assert response.status_code == status, response.text
        return response.json()

    def get(self, path, **params):
        response = self.client.get(path, params=params, headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    def snapshot(self, branch_id=None):
        return self.get(
            f"/projects/{self.project_id}/snapshot", branch_id=branch_id or self.branch_id
        )

    def object(self, body="Original lemma", kind="claim"):
        return self.write("/objects", {"branch_id": self.branch_id, "kind": kind, "body": body})

    def revise(self, obj, body, *, status=201, branch_id=None):
        return self.write(
            f"/objects/{obj['object_id']}/revisions",
            {
                "branch_id": branch_id or self.branch_id,
                "expected_revision_id": obj["revision_id"],
                "body": body,
            },
            status=status,
        )

    def text(self, body="Initial manuscript text"):
        return self.write(
            "/manuscript/blocks",
            {
                "branch_id": self.branch_id,
                "kind": "text",
                "body": body,
            },
        )["block_id"]


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "workspace.sqlite3"


@pytest.fixture
def api(database_path):
    with TestClient(create_app(database_path, token=TOKEN, worker_token=WORKER_TOKEN)) as client:
        created = client.post(
            "/projects",
            json={"title": "Workspace", "body": "Research goal"},
            headers={**AUTH, "Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 201, created.text
        yield WorkspaceAPI(
            client, **{key: created.json()[key] for key in ("project_id", "branch_id")}
        )


def head(snapshot, object_id):
    return next(obj["revision"] for obj in snapshot["objects"] if obj["id"] == object_id)


def test_layout_concurrent_cas_replay_and_branch_isolation(api):
    lemma = api.object()
    other = api.write("/branches", {"source_branch_id": api.branch_id, "name": "Alternate"})
    path = f"/branches/{api.branch_id}/layout"
    assert api.get(path)["version"] == 0
    before = api.snapshot()
    keys = [str(uuid4()), str(uuid4())]
    payloads = [
        {
            "expected_version": 0,
            "positions": {
                lemma["object_id"]: {"x": x, "y": 25},
            },
        }
        for x in (80, 160)
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(
            workers.map(
                lambda index: api.request(
                    path,
                    payloads[index],
                    key=keys[index],
                    method="PUT",
                ),
                range(2),
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 200)
    layout = responses[winner].json()
    assert layout["version"] == 1
    assert api.get(path) == layout
    replay = api.request(path, payloads[winner], key=keys[winner], method="PUT")
    assert replay.status_code == 200 and replay.json() == layout
    after = api.snapshot()
    for field in ("objects", "proof_plans", "reviews", "support", "manuscript"):
        assert after[field] == before[field]
    assert after["project"]["event_seq"] == before["project"]["event_seq"] + 1
    assert api.snapshot(other["branch_id"])["layout"]["version"] == 0
    branches = api.get(f"/projects/{api.project_id}/branches")["branches"]
    assert {branch["name"] for branch in branches} == {"main", "Alternate"}


def test_layout_and_annotations_reject_foreign_objects_and_invalid_anchors(api):
    local = api.object("A continuous function on a compact set")
    foreign = api.write("/projects", {"title": "Other", "body": "Private goal"})
    remote = WorkspaceAPI(api.client, foreign["project_id"], foreign["branch_id"]).object()
    before = api.snapshot()
    api.write(
        f"/branches/{api.branch_id}/layout",
        {
            "expected_version": 0,
            "positions": {remote["object_id"]: {"x": 2, "y": 3}},
        },
        method="PUT",
        status=422,
    )
    for revision_id, quote in ((remote["revision_id"], None), (local["revision_id"], "absent")):
        api.write(
            "/annotations",
            {
                "branch_id": api.branch_id,
                "revision_id": revision_id,
                "body": "Check this claim",
                "anchor_quote": quote,
            },
            status=422,
        )
    assert api.snapshot() == before
    annotation = api.write(
        "/annotations",
        {
            "branch_id": api.branch_id,
            "revision_id": local["revision_id"],
            "body": "Does compactness apply here?",
            "anchor_quote": "compact set",
        },
    )
    api.revise(local, "A corrected formulation")
    assert api.snapshot()["annotations"] == [annotation]
    assert annotation["revision_id"] == local["revision_id"]
    assert api.get(f"/branches/{api.branch_id}/annotations", revision_id=local["revision_id"])[
        "annotations"
    ] == [annotation]
    assert annotation["author"] == "human"


def test_free_block_cas_history_prevents_concurrent_and_aba_lost_updates(api):
    block_id = api.text("A")
    path = f"/manuscript/blocks/{block_id}/revisions"
    before = api.snapshot()
    payloads = [
        {"branch_id": api.branch_id, "expected_body": "A", "expected_version": 0, "body": body}
        for body in ("B", "C")
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda payload: api.request(path, payload), payloads))
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response.json() for response in responses if response.status_code == 201)
    assert winner["version"] == 1
    api.write(
        path,
        {
            "branch_id": api.branch_id,
            "expected_body": winner["body"],
            "expected_version": 1,
            "body": "A",
        },
    )
    stale = api.write(path, payloads[0], status=409)
    assert stale["current_body"] == "A" and stale["current_version"] == 2
    history = api.get(path)
    assert history["block"]["version"] == 2
    assert [(r["previous_body"], r["body"]) for r in history["revisions"]] == [
        ("A", winner["body"]),
        (winner["body"], "A"),
    ]
    after = api.snapshot()
    assert after["objects"] == before["objects"]
    assert after["block_history"] == history["revisions"]
    assert next(block for block in after["manuscript"] if block["id"] == block_id)["version"] == 2


def test_block_update_is_branch_scoped_and_reference_edits_require_object_revision(api):
    block_id = api.text()
    other = api.write("/branches", {"source_branch_id": api.branch_id, "name": "Other"})
    before = api.snapshot()
    payload = {
        "branch_id": other["branch_id"],
        "expected_body": "Initial manuscript text",
        "expected_version": 0,
        "body": "Wrong branch edit",
    }
    api.write(f"/manuscript/blocks/{block_id}/revisions", payload, status=422)
    reference = next(block for block in before["manuscript"] if block["kind"] == "reference")
    api.write(
        f"/manuscript/blocks/{reference['id']}/revisions",
        {
            **payload,
            "branch_id": api.branch_id,
            "expected_body": reference["body"],
        },
        status=422,
    )
    assert api.snapshot() == before


@pytest.mark.parametrize("choose_candidate", [False, True])
def test_conflict_resolution_preserves_parents_proof_structure_and_old_reviews(
    api, choose_candidate
):
    premise = api.object("Premise")
    conclusion = api.object("Conclusion")
    plan = api.write(
        "/proof-plans",
        {
            "branch_id": api.branch_id,
            "conclusion_revision_id": conclusion["revision_id"],
            "premise_revision_ids": [premise["revision_id"]],
            "body": "Original proof",
        },
    )
    current = api.revise(plan, "Current proof")
    api.write(
        "/reviews",
        {
            "branch_id": api.branch_id,
            "target_revision_id": current["revision_id"],
            "kind": "human_review",
            "verdict": "passed",
            "coverage": "whole_plan",
            "scope": "Whole proof",
            "findings": ["Explicit human review"],
        },
    )
    conflict = api.revise(plan, "Competing proof", status=409)
    key = str(uuid4())
    payload = {
        "branch_id": api.branch_id,
        "expected_current_revision_id": current["revision_id"],
        "selected_revision_id": conflict["candidate_revision_id"]
        if choose_candidate
        else current["revision_id"],
    }
    path = f"/conflicts/{conflict['conflict_id']}/resolve"
    merged = api.write(path, payload, key=key)
    assert api.write(path, payload, key=key) == merged
    assert set(merged["parent_revision_ids"]) == {
        current["revision_id"],
        conflict["candidate_revision_id"],
    }
    snapshot = api.snapshot()
    selected = head(snapshot, plan["object_id"])
    assert selected["id"] == merged["revision_id"]
    assert selected["body"] == ("Competing proof" if choose_candidate else "Current proof")
    selected_plan = next(p for p in snapshot["proof_plans"] if p["revision_id"] == selected["id"])
    assert selected_plan["premise_revision_ids"] == [premise["revision_id"]]
    assert selected_plan["conclusion_revision_id"] == conclusion["revision_id"]
    assert not any(review["target_revision_id"] == selected["id"] for review in snapshot["reviews"])
    assert merged["updated_block_ids"]
    assert all(
        block["revision_id"] == selected["id"]
        for block in snapshot["manuscript"]
        if block["id"] in merged["updated_block_ids"]
    )
    assert snapshot["conflicts"][0]["resolution"]["revision_id"] == selected["id"]
    history = api.get(f"/objects/{plan['object_id']}/revisions")["revisions"]
    assert set(next(r for r in history if r["id"] == selected["id"])["parent_revision_ids"]) == {
        current["revision_id"],
        conflict["candidate_revision_id"],
    }
    assert len(history) == 4
    assert api.write(path, payload, status=409)["error"] == "conflict_already_resolved"


def test_conflict_resolution_rechecks_head_and_serializes_competing_choices(api):
    lemma = api.object()
    current = api.revise(lemma, "Current")
    conflict = api.revise(lemma, "Candidate", status=409)
    latest = api.revise(current, "Newer current")
    path = f"/conflicts/{conflict['conflict_id']}/resolve"
    stale = {
        "branch_id": api.branch_id,
        "expected_current_revision_id": current["revision_id"],
        "selected_revision_id": conflict["candidate_revision_id"],
    }
    before = api.snapshot()
    assert api.write(path, stale, status=409)["error"] == "stale_conflict_resolution"
    assert api.snapshot() == before
    payloads = [
        {
            **stale,
            "expected_current_revision_id": latest["revision_id"],
            "selected_revision_id": selected,
        }
        for selected in (
            latest["revision_id"],
            conflict["candidate_revision_id"],
        )
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda payload: api.request(path, payload), payloads))
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response.json() for response in responses if response.status_code == 201)
    assert head(api.snapshot(), lemma["object_id"])["id"] == winner["revision_id"]
    assert latest["revision_id"] in winner["parent_revision_ids"]
    assert len(api.get(f"/objects/{lemma['object_id']}/revisions")["revisions"]) == 5


def test_draft_proof_reports_recheck_after_lemma_edit_and_conflict_merge(api):
    lemma = api.object("Lemma v1")
    conclusion = api.object("Target conclusion")
    plan = api.write(
        "/proof-plans",
        {
            "branch_id": api.branch_id,
            "conclusion_revision_id": conclusion["revision_id"],
            "premise_revision_ids": [lemma["revision_id"]],
            "body": "Unreviewed draft using v1",
        },
    )
    revised = api.revise(lemma, "Lemma v2")
    snapshot = api.snapshot()
    assert plan["revision_id"] in revised["affected_plan_revision_ids"]
    assert snapshot["support"]["plans"][plan["revision_id"]]["status"] == "needs_recheck"
    assert (
        next(obj for obj in snapshot["objects"] if obj["id"] == plan["object_id"])["adoption_state"]
        == "draft"
    )
    newer_plan = api.write(
        "/proof-plans",
        {
            "branch_id": api.branch_id,
            "conclusion_revision_id": conclusion["revision_id"],
            "premise_revision_ids": [revised["revision_id"]],
            "body": "Draft using v2",
        },
    )
    conflict = api.revise(lemma, "Candidate correction", status=409)
    foreign = api.write("/projects", {"title": "Other project", "body": "Unrelated goal"})
    path = f"/conflicts/{conflict['conflict_id']}/resolve"
    payload = {
        "branch_id": api.branch_id,
        "expected_current_revision_id": revised["revision_id"],
        "selected_revision_id": conflict["candidate_revision_id"],
    }
    before = api.snapshot()
    api.write(path, {**payload, "branch_id": foreign["branch_id"]}, status=422)
    assert api.snapshot() == before
    merged = api.write(path, payload)
    assert newer_plan["revision_id"] in merged["affected_plan_revision_ids"]
    assert (
        api.snapshot()["support"]["plans"][newer_plan["revision_id"]]["status"] == "needs_recheck"
    )


def test_search_reports_version_status_branch_and_candidates_without_cross_project_leaks(api):
    original = api.object("Needle original")
    api.write(
        "/adoptions",
        {
            "branch_id": api.branch_id,
            "revision_id": original["revision_id"],
            "state": "adopted",
            "reason": "Explicit adoption",
        },
    )
    other = api.write("/branches", {"source_branch_id": api.branch_id, "name": "Alternate"})
    alternate = api.revise(original, "Needle alternate", branch_id=other["branch_id"])
    current = api.revise(original, "Needle current")
    conflict = api.revise(original, "Needle candidate", status=409)
    block_id = api.text("Needle free paragraph")
    path = f"/projects/{api.project_id}/search"
    search = api.get(path, q="nEeDlE", branch_id=api.branch_id)
    revisions = {row["revision_id"]: row for row in search["results"]}
    assert alternate["revision_id"] not in revisions
    assert revisions[original["revision_id"]]["status"] == "adopted"
    assert revisions[original["revision_id"]]["revision_state"] == "historical"
    assert revisions[current["revision_id"]]["is_current"] is True
    assert (
        revisions[current["revision_id"]]["version"] > revisions[original["revision_id"]]["version"]
    )
    assert revisions[conflict["candidate_revision_id"]]["revision_state"] == "conflict_candidate"
    assert revisions[conflict["candidate_revision_id"]]["conflict_id"] == conflict["conflict_id"]
    assert revisions[None]["block_id"] == block_id
    assert all(row["branch_id"] == api.branch_id for row in search["results"])
    whole_project = api.get(path, q="Needle")
    assert any(
        row["revision_id"] == alternate["revision_id"] and row["branch_name"] == "Alternate"
        for row in whole_project["results"]
    )
    limited = api.get(path, q="Needle", limit=1)
    assert limited["truncated"] is True and len(limited["results"]) == 1
    assert limited["total"] == whole_project["total"]
    foreign = api.write("/projects", {"title": "Foreign", "body": "Needle private material"})
    assert api.get(path, q="private")["total"] == 0
    rejected = api.client.get(
        path, params={"q": "Needle", "branch_id": foreign["branch_id"]}, headers=AUTH
    )
    assert rejected.status_code == 422
    assert api.client.get(path, params={"q": "   "}, headers=AUTH).status_code == 422


def test_workspace_queries_and_mutations_require_human_auth(api):
    block_id = api.text()
    paths = [
        f"/projects/{api.project_id}/branches",
        f"/branches/{api.branch_id}/layout",
        f"/branches/{api.branch_id}/annotations",
        f"/manuscript/blocks/{block_id}/revisions",
        f"/projects/{api.project_id}/search?q=goal",
    ]
    for path in paths:
        assert api.client.get(path).status_code == 401
        assert (
            api.client.get(path, headers={"Authorization": f"Bearer {WORKER_TOKEN}"}).status_code
            == 401
        )
        assert (
            api.client.get(path, headers={**AUTH, "Origin": "https://foreign.example"}).status_code
            == 403
        )
    layout_path = f"/branches/{api.branch_id}/layout"
    assert (
        api.client.put(
            layout_path, json={"expected_version": 0, "positions": {}}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        api.client.put(
            layout_path,
            json={"expected_version": 0, "positions": {}},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}", "Idempotency-Key": str(uuid4())},
        ).status_code
        == 401
    )


def test_workspace_snapshot_and_edit_receipts_survive_database_reopen(database_path):
    with TestClient(create_app(database_path, token=TOKEN, worker_token=WORKER_TOKEN)) as client:
        api = WorkspaceAPI(client, "", "")
        project = api.write("/projects", {"title": "Persistent", "body": "Preserve this goal"})
        api.project_id, api.branch_id = project["project_id"], project["branch_id"]
        lemma = api.object()
        current = api.revise(lemma, "Current")
        conflict = api.revise(lemma, "Candidate", status=409)
        api.write(
            f"/conflicts/{conflict['conflict_id']}/resolve",
            {
                "branch_id": api.branch_id,
                "expected_current_revision_id": current["revision_id"],
                "selected_revision_id": conflict["candidate_revision_id"],
            },
        )
        api.write(
            f"/branches/{api.branch_id}/layout",
            {"expected_version": 0, "positions": {lemma["object_id"]: {"x": 80, "y": 120}}},
            method="PUT",
            status=200,
        )
        api.write(
            "/annotations",
            {
                "branch_id": api.branch_id,
                "revision_id": lemma["revision_id"],
                "body": "Check the original",
                "anchor_quote": "Original",
            },
        )
        block_id = api.text("Original text")
        edit = {
            "branch_id": api.branch_id,
            "expected_body": "Original text",
            "expected_version": 0,
            "body": "Preserved edit",
        }
        key = str(uuid4())
        path = f"/manuscript/blocks/{block_id}/revisions"
        receipt = api.write(path, edit, key=key)
        snapshot = api.snapshot()
    with TestClient(create_app(database_path, token=TOKEN, worker_token=WORKER_TOKEN)) as client:
        reopened = WorkspaceAPI(client, api.project_id, api.branch_id)
        assert reopened.snapshot() == snapshot
        assert reopened.write(path, edit, key=key) == receipt
        assert len(reopened.get(path)["revisions"]) == 1
