"""API regressions discovered while reviewing branch and revision consistency."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app


@pytest.fixture
def commands(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "review.db", token="human-review-test", worker_token="worker-review-test"
        )
    ) as client:
        headers = {"Authorization": "Bearer human-review-test"}

        def post(path, payload, expected=201):
            response = client.post(
                path, json=payload, headers={**headers, "Idempotency-Key": str(uuid4())}
            )
            assert response.status_code == expected, response.text
            return response.json()

        project = post("/projects", {"title": "Review regression", "body": "Original goal"})

        def snapshot(branch_id=None):
            response = client.get(
                f"/projects/{project['project_id']}/snapshot",
                params={"branch_id": branch_id or project["branch_id"]},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            return response.json()

        yield post, snapshot, project


@pytest.mark.parametrize("role", [[], {}, None, "axiom", 42])
def test_invalid_context_role_is_rejected_atomically_on_create_and_revise(commands, role):
    post, snapshot, project = commands
    branch = project["branch_id"]
    context = post(
        "/objects",
        {
            "branch_id": branch,
            "kind": "context",
            "body": "Definition X",
            "payload": {"role": "definition"},
        },
    )
    before = snapshot()
    post(
        "/objects",
        {
            "branch_id": branch,
            "kind": "context",
            "body": "Invalid context",
            "payload": {"role": role},
        },
        expected=422,
    )
    post(
        f"/objects/{context['object_id']}/revisions",
        {
            "branch_id": branch,
            "expected_revision_id": context["revision_id"],
            "body": "Invalid revision",
            "payload": {"role": role},
        },
        expected=422,
    )
    after = snapshot()
    assert after["objects"] == before["objects"]
    assert after["project"]["event_seq"] == before["project"]["event_seq"]


def test_body_only_revision_preserves_context_role_and_source_metadata(commands):
    post, snapshot, project = commands
    payload = {"role": "definition", "source": "Definition 2.1", "symbols": ["X"]}
    context = post(
        "/objects",
        {
            "branch_id": project["branch_id"],
            "kind": "context",
            "body": "Definition X",
            "payload": payload,
        },
    )
    revision = post(
        f"/objects/{context['object_id']}/revisions",
        {
            "branch_id": project["branch_id"],
            "expected_revision_id": context["revision_id"],
            "body": "Definition X, with corrected punctuation.",
        },
    )
    current = next(item for item in snapshot()["objects"] if item["id"] == context["object_id"])
    assert current["revision"]["id"] == revision["revision_id"]
    assert current["revision"]["payload"] == payload
    post(
        f"/objects/{context['object_id']}/revisions",
        {
            "branch_id": project["branch_id"],
            "expected_revision_id": revision["revision_id"],
            "body": "Explicitly reset metadata.",
            "payload": {},
        },
    )
    current = next(item for item in snapshot()["objects"] if item["id"] == context["object_id"])
    assert current["revision"]["payload"] == {}


def test_branch_snapshot_excludes_other_branch_only_claims_and_reviews(commands):
    post, snapshot, project = commands
    main = project["branch_id"]
    shared = post("/objects", {"branch_id": main, "kind": "claim", "body": "Shared claim"})
    shared_review = post(
        "/reviews",
        {
            "branch_id": main,
            "target_revision_id": shared["revision_id"],
            "kind": "human_review",
            "verdict": "inconclusive",
            "scope": "Original shared statement",
            "findings": ["Needs a proof plan."],
        },
    )
    side = post("/branches", {"source_branch_id": main, "name": "side"})["branch_id"]
    private = post("/objects", {"branch_id": main, "kind": "claim", "body": "Only created on main"})
    private_review = post(
        "/reviews",
        {
            "branch_id": main,
            "target_revision_id": private["revision_id"],
            "kind": "human_review",
            "verdict": "inconclusive",
            "scope": "Main branch candidate",
            "findings": ["Requires a new approach."],
        },
    )
    post(
        f"/objects/{shared['object_id']}/revisions",
        {
            "branch_id": side,
            "expected_revision_id": shared["revision_id"],
            "body": "Shared claim, revised on side",
        },
    )
    result = snapshot(side)
    assert private["object_id"] not in {item["id"] for item in result["objects"]}
    assert private["revision_id"] not in result["support"]["claims"]
    review_ids = {item["id"] for item in result["reviews"]}
    assert private_review["review_id"] not in review_ids
    assert shared_review["review_id"] in review_ids
    assert shared["revision_id"] in result["support"]["claims"]
