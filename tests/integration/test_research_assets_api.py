"""HTTP contract tests for human-controlled research assets."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app

TOKEN = "assets-human"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "assets.sqlite3", token=TOKEN, worker_token="worker")
    app.state.database.migrate()
    with TestClient(app) as value:
        yield value
    app.state.database.close()


def post(client, path, payload, expected=200):
    response = client.post(path, json=payload, headers={**AUTH, "Idempotency-Key": str(uuid4())})
    assert response.status_code == expected, response.text
    return response.json()


def get(client, path, **params):
    response = client.get(path, params=params, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_article_api_pins_sources_and_reports_current_version_change(client):
    project = post(client, "/projects", {"title": "Article", "body": "Original problem."}, 201)
    claim = post(client, "/objects", {"branch_id": project["branch_id"], "kind": "claim", "body": "Claim v1."}, 201)
    article = post(client, "/articles", {"branch_id": project["branch_id"], "title": "Draft", "sections": [{
        "heading": "Argument", "revision_ids": [claim["revision_id"]], "transition": "Therefore.",
    }]}, 201)
    assert any(issue["code"] == "source_unreviewed" for issue in article["issues"])
    post(client, f"/objects/{claim['object_id']}/revisions", {"branch_id": project["branch_id"],
        "expected_revision_id": claim["revision_id"], "body": "Claim v2."}, 201)
    checked = get(client, f"/articles/{article['revision_id']}/check", branch_id=project["branch_id"])
    assert any(issue["code"] == "source_version_changed" for issue in checked["current_issues"])


def test_merge_api_requires_resolution_and_rejects_stale_preview(client):
    project = post(client, "/projects", {"title": "Merge", "body": "Problem"}, 201)
    claim = post(client, "/objects", {"branch_id": project["branch_id"], "kind": "claim", "body": "v1"}, 201)
    source = post(client, "/branches", {"source_branch_id": project["branch_id"], "name": "source"}, 201)
    target = post(client, "/branches", {"source_branch_id": project["branch_id"], "name": "target"}, 201)
    post(client, f"/objects/{claim['object_id']}/revisions", {"branch_id": source["branch_id"],
        "expected_revision_id": claim["revision_id"], "body": "source"}, 201)
    preview = get(client, f"/branches/{target['branch_id']}/merge-preview", source_branch_id=source["branch_id"])
    missing = client.post(f"/branches/{target['branch_id']}/merge", json={"source_branch_id": source["branch_id"],
        "preview_token": preview["preview_token"], "resolutions": []}, headers={**AUTH, "Idempotency-Key": str(uuid4())})
    assert missing.status_code == 422
    post(client, f"/objects/{claim['object_id']}/revisions", {"branch_id": target["branch_id"],
        "expected_revision_id": claim["revision_id"], "body": "target"}, 201)
    stale = client.post(f"/branches/{target['branch_id']}/merge", json={"source_branch_id": source["branch_id"],
        "preview_token": preview["preview_token"], "resolutions": [{"object_id": claim["object_id"],
        "choice": "source", "reason": "Review source."}]}, headers={**AUTH, "Idempotency-Key": str(uuid4())})
    assert stale.status_code == 409


def test_permanent_delete_api_requires_typed_confirmation_and_is_idempotent(client):
    project = post(client, "/projects", {"title": "Erase", "body": "Problem"}, 201)
    material = post(client, "/objects", {"branch_id": project["branch_id"], "kind": "claim", "body": "Secret proof"}, 201)
    old = post(client, f"/objects/{material['object_id']}/revisions", {"branch_id": project["branch_id"],
        "expected_revision_id": material["revision_id"], "body": "Secret proof v2"}, 201)
    article = post(client, "/articles", {"branch_id": project["branch_id"], "title": "Derived", "sections": [{
        "heading": "Secret", "revision_ids": [old["revision_id"]], "transition": ""}],
    }, 201)
    export = post(client, "/exports", {"project_id": project["project_id"], "branch_id": project["branch_id"]}, 201)
    other = post(client, "/projects", {"title": "Other", "body": "Other secret"}, 201)
    preview = get(client, f"/objects/{material['object_id']}/deletion-preview")
    denied = client.post(f"/objects/{material['object_id']}/permanent-delete", json={"preview_token": preview["preview_token"],
        "confirmation": "delete"}, headers={**AUTH, "Idempotency-Key": str(uuid4())})
    assert denied.status_code == 422
    key = str(uuid4())
    payload = {"preview_token": preview["preview_token"], "confirmation": "永久删除"}
    first = client.post(f"/objects/{material['object_id']}/permanent-delete", json=payload,
        headers={**AUTH, "Idempotency-Key": key})
    assert first.status_code == 200 and first.json()["deleted"]
    replay = client.post(f"/objects/{material['object_id']}/permanent-delete", json=payload,
        headers={**AUTH, "Idempotency-Key": key})
    assert replay.status_code == 200
    snapshot = get(client, f"/projects/{project['project_id']}/snapshot", branch_id=project["branch_id"])
    assert "Secret proof" not in str(snapshot) and "Derived" not in str(snapshot)
    revisions = get(client, f"/objects/{material['object_id']}/revisions")["revisions"]
    assert all(item["body"] == "此材料已永久删除；其证据不可取得。" for item in revisions)
    restore = client.post(f"/objects/{material['object_id']}/revisions", json={
        "branch_id": project["branch_id"], "expected_revision_id": material["revision_id"], "body": "Restored secret"},
        headers={**AUTH, "Idempotency-Key": str(uuid4())})
    assert restore.status_code == 410
    download = client.get(export["download_url"], headers=AUTH)
    assert download.status_code in {404, 410}
    other_snapshot = get(client, f"/projects/{other['project_id']}/snapshot", branch_id=other["branch_id"])
    assert "Other secret" in str(other_snapshot)
    article_revisions = get(client, f"/objects/{article['object_id']}/revisions")["revisions"]
    assert all(item["body"] == "此材料已永久删除；其证据不可取得。" for item in article_revisions)


def test_deletion_preserves_unknown_cost_and_rejects_late_completion(client):
    project = post(client, "/projects", {"title": "Runtime erase", "body": "Problem"}, 201)
    run = post(client, "/runs", {"branch_id": project["branch_id"], "goal_object_id": project["object_id"],
        "provider": "fake", "request_budget": 3}, 201)
    worker_headers = {"Authorization": "Bearer worker", "Idempotency-Key": str(uuid4())}
    claim = client.post(f"/runs/{run['run_id']}/claim", headers=worker_headers)
    assert claim.status_code == 201
    task = claim.json()
    execution = {"token": task["token"]}
    reserve = client.post(f"/attempts/{task['attempt_id']}/requests", json=execution,
        headers={"Authorization": "Bearer worker", "Idempotency-Key": str(uuid4())})
    request_id = reserve.json()["request_id"]
    started = client.post(f"/requests/{request_id}/start", json=execution,
        headers={"Authorization": "Bearer worker", "Idempotency-Key": str(uuid4())})
    assert started.status_code == 200
    settled = client.post(f"/requests/{request_id}/settle", json={**execution, "outcome": "unknown",
        "reason": "transport_read_error"}, headers={"Authorization": "Bearer worker", "Idempotency-Key": str(uuid4())})
    assert settled.status_code == 200
    preview = get(client, f"/objects/{project['object_id']}/deletion-preview")
    deleted = client.post(f"/objects/{project['object_id']}/permanent-delete", json={
        "preview_token": preview["preview_token"], "confirmation": "永久删除",
    }, headers={**AUTH, "Idempotency-Key": str(uuid4())})
    assert deleted.status_code == 200
    budget = get(client, f"/projects/{project['project_id']}/budget")
    assert budget["unknown"] == budget["occupied"] == 1
    late = client.post(f"/attempts/{task['attempt_id']}/complete", json={**execution, "body": "Late result"},
        headers={"Authorization": "Bearer worker", "Idempotency-Key": str(uuid4())})
    assert late.status_code == 410


def test_delete_cleans_only_unshared_artifact_files_and_retries_safely(client):
    from mathagent.persistence.artifacts import ArtifactStore

    store = ArtifactStore(client.app.state.database.path)
    private = store.write_json({"private": "local computation output"})
    shared = store.write_json({"shared": "used by another project"})
    project = post(client, "/projects", {"title": "Artifacts", "body": "Original."}, 201)
    other = post(client, "/projects", {"title": "Other artifacts", "body": "Original."}, 201)
    source = post(client, "/objects", {"branch_id": project["branch_id"], "kind": "artifact",
        "body": "Local files.", "payload": {"artifact_files": [private, shared]}}, 201)
    post(client, "/objects", {"branch_id": other["branch_id"], "kind": "artifact",
        "body": "Shared file.", "payload": {"artifact_files": [shared]}}, 201)
    preview = get(client, f"/objects/{source['object_id']}/deletion-preview")
    headers = {**AUTH, "Idempotency-Key": str(uuid4())}
    payload = {"preview_token": preview["preview_token"], "confirmation": "永久删除"}
    for _ in range(2):
        response = client.post(f"/objects/{source['object_id']}/permanent-delete", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["storage_cleanup_pending"] is False
    assert not (store.directory / (private["sha256"] + ".json")).exists()
    assert store.read(shared)
