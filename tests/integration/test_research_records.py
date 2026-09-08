"""Structured records and branch presentation over authenticated local HTTP."""

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app
from mathagent.application.research_records import ResearchRecordsService
from mathagent.persistence.models import Revision
from mathagent.persistence.research_models import ResearchRecordReference
from sqlalchemy import select

TOKEN = "research-records-human-test"
WORKER_TOKEN = "research-records-worker-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def request(client, path, payload, *, method="POST", key=None, auth=AUTH):
    return client.request(
        method,
        path,
        json=payload,
        headers={**auth, "Idempotency-Key": key or str(uuid4())},
    )


def write(client, path, payload, *, status=201, **kwargs):
    response = request(client, path, payload, **kwargs)
    assert response.status_code == status, response.text
    return response.json()


def get(client, path, **params):
    response = client.get(path, params=params, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def project(client):
    return write(client, "/projects", {"title": "Record tests", "body": "Original question"})


def obj(client, branch_id, body, kind="claim"):
    return write(client, "/objects", {"branch_id": branch_id, "kind": kind, "body": body})


def failure(branch_id, target_id, **changes):
    return {
        "branch_id": branch_id,
        "target_revision_id": target_id,
        "outcome": "unresolved",
        "body": "No proof was completed under the stated assumptions.",
        "assumption_revision_ids": [],
        "evidence_revision_ids": [],
        "scope": "Only the submitted target and the declared assumptions.",
        "retry_conditions": ["Try a different construction."],
        "evidence_explanation": "",
        **changes,
    }


def source(branch_id, **changes):
    return {
        "branch_id": branch_id,
        "title": "A supplied mathematical reference",
        "authors": ["A. Researcher"],
        "url_or_identifier": "https://example.invalid/math-reference",
        "locator": "Theorem 2, page 4",
        "accessed_on": "2026-09-08",
        "body": "A supplied excerpt; no remote retrieval occurs.",
        "verification": "unverified",
        **changes,
    }


def snapshot(client, p):
    return get(client, f"/projects/{p['project_id']}/snapshot", branch_id=p["branch_id"])


@pytest.fixture
def api(tmp_path):
    with TestClient(
        create_app(tmp_path / "records.db", token=TOKEN, worker_token=WORKER_TOKEN)
    ) as client:
        yield client


def test_failure_binds_historical_target_assumptions_evidence_and_replay(api):
    p = project(api)
    target = obj(api, p["branch_id"], "Target version one")
    assumption = obj(api, p["branch_id"], "Assume Q", "context")
    evidence = obj(api, p["branch_id"], "The exact failed equality", "artifact")
    saved = write(
        api,
        "/failures",
        failure(
            p["branch_id"],
            target["revision_id"],
            outcome="argument_error",
            assumption_revision_ids=[assumption["revision_id"]],
            evidence_revision_ids=[evidence["revision_id"]],
        ),
        key="fixed-failure",
    )
    assert (
        write(
            api,
            "/failures",
            failure(
                p["branch_id"],
                target["revision_id"],
                outcome="argument_error",
                assumption_revision_ids=[assumption["revision_id"]],
                evidence_revision_ids=[evidence["revision_id"]],
            ),
            key="fixed-failure",
        )
        == saved
    )
    write(
        api,
        f"/objects/{target['object_id']}/revisions",
        {
            "branch_id": p["branch_id"],
            "expected_revision_id": target["revision_id"],
            "body": "Target version two",
        },
    )
    with api.app.state.database.sessions() as session:
        revision = session.get(Revision, saved["revision_id"])
        assert revision.payload["target_revision_id"] == target["revision_id"]
        assert revision.author == "human"
        references = session.scalars(
            select(ResearchRecordReference).where(
                ResearchRecordReference.record_revision_id == revision.id
            )
        ).all()
        assert {(reference.role, reference.target_revision_id) for reference in references} == {
            ("target", target["revision_id"]),
            ("assumption", assumption["revision_id"]),
            ("evidence", evidence["revision_id"]),
        }
    current = snapshot(api, p)
    assert len([item for item in current["objects"] if item["id"] == saved["object_id"]]) == 1
    assert (
        next(item for item in current["objects"] if item["id"] == target["object_id"])[
            "adoption_state"
        ]
        == "draft"
    )  # The failure record cannot refute or adopt its target by itself.


@pytest.mark.parametrize("outcome", ["argument_error", "method_obstruction", "refuted"])
def test_mathematical_failure_needs_evidence_or_explicit_explanation(api, outcome):
    p = project(api)
    payload = failure(p["branch_id"], p["revision_id"], outcome=outcome)
    before = snapshot(api, p)
    assert request(api, "/failures", payload).status_code == 422
    assert snapshot(api, p) == before
    saved = write(
        api,
        "/failures",
        {**payload, "evidence_explanation": "A supplied argument identifies the failed step."},
    )
    item = next(item for item in snapshot(api, p)["objects"] if item["id"] == saved["object_id"])
    assert item["revision"]["payload"]["outcome"] == outcome
    assert item["adoption_state"] == "draft"


def test_failure_rejects_cross_project_other_branch_and_wrong_assumptions(api):
    p, foreign = project(api), project(api)
    sibling = write(api, "/branches", {"source_branch_id": p["branch_id"], "name": "sibling"})
    sibling_only = obj(api, sibling["branch_id"], "Only on sibling")
    sibling_revision = write(
        api,
        f"/objects/{p['object_id']}/revisions",
        {
            "branch_id": sibling["branch_id"],
            "expected_revision_id": p["revision_id"],
            "body": "New sibling target",
        },
    )
    artifact = obj(api, p["branch_id"], "An artifact is not an assumption", "artifact")
    for changes in (
        {"target_revision_id": foreign["revision_id"]},
        {"target_revision_id": sibling_only["revision_id"]},
        {"target_revision_id": sibling_revision["revision_id"]},
        {"assumption_revision_ids": [artifact["revision_id"]]},
        {"evidence_revision_ids": [foreign["revision_id"]]},
    ):
        before = snapshot(api, p)
        response = request(api, "/failures", failure(p["branch_id"], p["revision_id"], **changes))
        assert response.status_code == 422, response.text
        assert snapshot(api, p) == before


def test_sources_preserve_metadata_across_new_versions_and_reject_schema_removal(api):
    p = project(api)
    saved = write(api, "/sources", source(p["branch_id"]))
    original = next(
        item for item in snapshot(api, p)["objects"] if item["id"] == saved["object_id"]
    )
    metadata = original["revision"]["payload"]
    changed = write(
        api,
        f"/objects/{saved['object_id']}/revisions",
        {
            "branch_id": p["branch_id"],
            "expected_revision_id": saved["revision_id"],
            "body": "Corrected excerpt",
            "payload": {**metadata, "locator": "Theorem 3, page 5"},
        },
    )
    history = get(api, f"/objects/{saved['object_id']}/revisions")["revisions"]
    assert (
        next(item for item in history if item["id"] == saved["revision_id"])["payload"] == metadata
    )
    assert (
        next(item for item in history if item["id"] == changed["revision_id"])["payload"]["locator"]
        == "Theorem 3, page 5"
    )
    for invalid in ({}, {**metadata, "accessed_on": "not-a-date"}):
        response = request(
            api,
            f"/objects/{saved['object_id']}/revisions",
            {
                "branch_id": p["branch_id"],
                "expected_revision_id": changed["revision_id"],
                "body": "Attempt to remove structured provenance",
                "payload": invalid,
            },
        )
        assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"url_or_identifier": "javascript:alert(1)"},
        {"url_or_identifier": "https://user:password@example.invalid/paper"},
        {"locator": " "},
        {"title": " "},
        {"authors": [" "]},
        {"accessed_on": "2026-02-30"},
        {"verification": "formally_verified"},
        {"author": "worker:deepseek"},
    ],
)
def test_source_validation_and_author_provenance_cannot_be_bypassed(api, changes):
    p = project(api)
    response = request(api, "/sources", source(p["branch_id"], **changes))
    assert response.status_code == 422, response.text
    assert len(snapshot(api, p)["objects"]) == 1


def test_worker_uses_same_validated_service_and_cannot_choose_http_author(api):
    p = project(api)
    state = api.app.state.service
    service = ResearchRecordsService(state)
    payload = failure(p["branch_id"], p["revision_id"])
    _, saved = state.execute(
        "worker.failure_test",
        "worker-failure",
        payload,
        lambda session, data: service.create_failure(session, data, author="worker:deepseek"),
    )
    with state.db.sessions() as session:
        assert session.get(Revision, saved["revision_id"]).author == "worker:deepseek"
    response = request(api, "/failures", {**payload, "author": "human"})
    assert response.status_code == 422
    assert (
        request(
            api, "/failures", payload, auth={"Authorization": f"Bearer {WORKER_TOKEN}"}
        ).status_code
        == 401
    )


def test_presentation_cas_concurrency_keeps_mathematics_and_branch_isolation(api):
    p = project(api)
    item = obj(api, p["branch_id"], "Keep the mathematical content")
    sibling = write(api, "/branches", {"source_branch_id": p["branch_id"], "name": "visible"})
    path = f"/branches/{p['branch_id']}/presentation"
    before = snapshot(api, p)
    assert get(api, path)["version"] == 0
    payload = {
        "expected_version": 0,
        "hidden_object_ids": [item["object_id"]],
        "collapsed_object_ids": [p["object_id"]],
        "archived": True,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda _: request(api, path, payload, method="PUT"), range(2))
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    current = get(api, path)
    assert current["version"] == 1 and current["archived"] is True
    after = snapshot(api, p)
    assert after["objects"] == before["objects"]
    assert after["manuscript"] == before["manuscript"]
    assert after["presentation"] == current
    branches = get(api, f"/projects/{p['project_id']}/branches")["branches"]
    assert (
        next(branch for branch in branches if branch["id"] == p["branch_id"])["presentation"]
        == current
    )
    assert get(api, f"/branches/{sibling['branch_id']}/presentation")["archived"] is False
    receipt = write(
        api,
        path,
        {**payload, "expected_version": 1, "archived": False},
        method="PUT",
        status=200,
        key="presentation-unarchive",
    )
    assert (
        write(
            api,
            path,
            {**payload, "expected_version": 1, "archived": False},
            method="PUT",
            status=200,
            key="presentation-unarchive",
        )
        == receipt
    )


def test_presentation_rejects_foreign_objects_and_requires_human_auth(api):
    p, foreign = project(api), project(api)
    path = f"/branches/{p['branch_id']}/presentation"
    payload = {
        "expected_version": 0,
        "hidden_object_ids": [foreign["object_id"]],
        "collapsed_object_ids": [],
        "archived": False,
    }
    assert request(api, path, payload, method="PUT").status_code == 422
    assert get(api, path)["version"] == 0
    assert request(api, path, payload, method="PUT", auth={}).status_code == 401
    assert api.get(path).status_code == 401


def test_record_export_keeps_exact_references_and_source_locator(api):
    p = project(api)
    cited = write(api, "/sources", source(p["branch_id"]))
    assumption = obj(api, p["branch_id"], "Finite-dimensional setting", "context")
    target = obj(api, p["branch_id"], "The exact local target")
    note = write(
        api,
        "/failures",
        failure(
            p["branch_id"],
            target["revision_id"],
            outcome="method_obstruction",
            evidence_revision_ids=[cited["revision_id"]],
            assumption_revision_ids=[assumption["revision_id"]],
        ),
    )
    export = write(
        api,
        "/exports",
        {
            "project_id": p["project_id"],
            "branch_id": p["branch_id"],
            "object_ids": [note["object_id"]],
        },
    )
    response = api.get(export["download_url"], headers=AUTH)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    revisions = {item["id"]: item for item in manifest["revisions"]}
    assert {
        note["revision_id"],
        target["revision_id"],
        assumption["revision_id"],
        cited["revision_id"],
    } <= set(revisions)
    assert revisions[cited["revision_id"]]["payload"]["locator"] == "Theorem 2, page 4"


def test_presentation_and_records_survive_database_reopen(tmp_path):
    path = tmp_path / "reopened.db"
    with TestClient(create_app(path, token=TOKEN, worker_token=WORKER_TOKEN)) as client:
        p = project(client)
        write(client, "/sources", source(p["branch_id"]))
        write(
            client,
            f"/branches/{p['branch_id']}/presentation",
            {
                "expected_version": 0,
                "hidden_object_ids": [p["object_id"]],
                "collapsed_object_ids": [],
                "archived": True,
            },
            method="PUT",
            status=200,
        )
        expected = snapshot(client, p)
    with TestClient(create_app(path, token=TOKEN, worker_token=WORKER_TOKEN)) as client:
        assert snapshot(client, p) == expected


def test_conflict_resolution_keeps_failure_reference_bindings_and_export_closure(api):
    p = project(api)
    target = obj(api, p["branch_id"], "An exact target separate from the original question")
    evidence = obj(api, p["branch_id"], "A counterexample calculation", "artifact")
    note = write(
        api,
        "/failures",
        failure(
            p["branch_id"],
            target["revision_id"],
            outcome="refuted",
            evidence_revision_ids=[evidence["revision_id"]],
        ),
    )
    revision_payload = {
        "branch_id": p["branch_id"],
        "expected_revision_id": note["revision_id"],
        "body": "First edited explanation",
    }
    current = write(api, f"/objects/{note['object_id']}/revisions", revision_payload)
    conflict = write(
        api,
        f"/objects/{note['object_id']}/revisions",
        {**revision_payload, "body": "Concurrent explanation retained as candidate"},
        status=409,
    )
    resolved = write(
        api,
        f"/conflicts/{conflict['conflict_id']}/resolve",
        {
            "branch_id": p["branch_id"],
            "expected_current_revision_id": current["revision_id"],
            "selected_revision_id": conflict["candidate_revision_id"],
        },
    )
    with api.app.state.database.sessions() as session:
        references = session.scalars(
            select(ResearchRecordReference).where(
                ResearchRecordReference.record_revision_id == resolved["revision_id"]
            )
        ).all()
        assert {(reference.role, reference.target_revision_id) for reference in references} == {
            ("target", target["revision_id"]),
            ("evidence", evidence["revision_id"]),
        }
    export = write(
        api,
        "/exports",
        {
            "project_id": p["project_id"],
            "branch_id": p["branch_id"],
            "object_ids": [note["object_id"]],
        },
    )
    response = api.get(export["download_url"], headers=AUTH)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert {target["revision_id"], evidence["revision_id"], resolved["revision_id"]} <= {
        revision["id"] for revision in manifest["revisions"]
    }


def test_trusted_agent_author_survives_object_proof_revision_and_branch_commands(api):
    p = project(api)
    state = api.app.state.service
    author = "agent:deepseek"

    def command(operation, payload, handler):
        return state.execute(
            operation,
            str(uuid4()),
            payload,
            lambda session, data: handler(session, data, author=author),
        )[1]

    created = command(
        "agent.object",
        {"branch_id": p["branch_id"], "kind": "claim", "body": "Agent claim"},
        state.create_object,
    )
    revised = command(
        "agent.revise",
        {
            "branch_id": p["branch_id"],
            "object_id": created["object_id"],
            "expected_revision_id": created["revision_id"],
            "body": "Agent revised claim",
        },
        state.revise_object,
    )
    proof = command(
        "agent.proof",
        {
            "branch_id": p["branch_id"],
            "conclusion_revision_id": revised["revision_id"],
            "body": "Candidate inference",
            "premise_revision_ids": [],
            "context_revision_ids": [],
            "assumption_revision_ids": [],
            "gaps": [],
            "rule": "direct",
        },
        state.create_proof,
    )
    command(
        "agent.branch",
        {"source_branch_id": p["branch_id"], "name": "agent-side"},
        state.create_branch,
    )
    with state.db.sessions() as session:
        for revision_id in (created["revision_id"], revised["revision_id"], proof["revision_id"]):
            assert session.get(Revision, revision_id).author == author
    events = get(api, f"/projects/{p['project_id']}/events")["events"]
    assert all(
        event["author"] == author
        for event in events
        if event["type"]
        in {"object.created", "revision.created", "proof.created", "branch.created"}
    )
