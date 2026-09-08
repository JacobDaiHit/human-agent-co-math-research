"""Real HTTP, SQLite/WAL, archive, and fresh-process recovery acceptance."""

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app
from mathagent.persistence.models import Attempt, Run
from mathagent.persistence.runtime_models import ProviderRequest
from mathagent.tools.hermitian import ORIGINAL_QUESTION, exact_calculation
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
AUTH = {"Authorization": "Bearer test-human"}


def post(client, path, payload=None, *, worker=False, key=None):
    response = client.post(
        path,
        json=payload,
        headers={
            "Authorization": "Bearer test-worker" if worker else "Bearer test-human",
            "Idempotency-Key": key or str(uuid4()),
        },
    )
    assert response.status_code in {200, 201, 202}, response.text
    return response.json()


def snapshot(client, project, branch=None):
    response = client.get(
        f"/projects/{project['project_id']}/snapshot",
        params={"branch_id": branch or project["branch_id"]},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def archive(client, export):
    response = client.get(export["download_url"], headers=AUTH)
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert set(bundle.namelist()) == {"research.md", "manifest.json"}
        manifest = json.loads(bundle.read("manifest.json"))
        draft = bundle.read("research.md")
        assert manifest["files"]["research.md"]["sha256"] == hashlib.sha256(draft).hexdigest()
        assert manifest["files"]["research.md"]["size"] == len(draft)
    return response.content, manifest, draft.decode()


def make_client(path):
    return TestClient(create_app(path, token="test-human", worker_token="test-worker"))


def test_exact_hermitian_example_preserves_failed_goal_and_separate_new_question(tmp_path):
    computation = exact_calculation()
    assert computation["characteristic_polynomial"] == {
        "lambda^2": "1",
        "lambda*t": "0",
        "t^2": "-1",
    }
    assert computation["one_sided_derivatives_at_zero"] == {
        "left": ["1", "-1"],
        "right": ["-1", "1"],
    }
    assert computation["analytic_branches"][0]["Av"] == ["1", "1"]
    assert computation["analytic_branches"][1]["Av"] == ["-1", "1"]
    with make_client(tmp_path / "example.db") as client:
        example = post(client, "/examples/hermitian", {}, key="hermitian-once")
        assert post(client, "/examples/hermitian", {}, key="hermitian-once") == example
        assert example["real_model_calls"] == 0
        main = snapshot(client, example)
        analytic = snapshot(client, example, example["analytic_branch_id"])
        main_objects = {obj["id"]: obj for obj in main["objects"]}
        assert main_objects[example["object_id"]]["revision"]["body"] == ORIGINAL_QUESTION
        names = example["objects"]
        assert names["analytic_question"]["object_id"] not in main_objects
        assert names["analytic_question"]["object_id"] in {obj["id"] for obj in analytic["objects"]}
        assert names["general_question"]["object_id"] in {obj["id"] for obj in analytic["objects"]}
        assert main_objects[names["conjecture"]["object_id"]]["adoption_state"] == "disputed"
        assert main_objects[names["failed_argument"]["object_id"]]["adoption_state"] == "withdrawn"
        actual = main_objects[names["exact_artifact"]["object_id"]]["revision"]["payload"]
        assert actual["execution"] == "actual_local_exact_calculation"
        assert actual["simulated"] is False and actual["model_discovery"] is False
        assert all(obj["revision"]["payload"]["pre_authored"] for obj in main["objects"])
        assert any(rel["kind"] == "refutes" for rel in main["relations"])
        assert main["reviews"][0]["coverage"] == "partial"
        assert main["reviews"][0]["author"] == "fixed_hermitian_exact_tool"
        assert main["runs"] == []


def test_export_keeps_old_dependencies_scope_annotations_and_exact_bytes_across_restart(tmp_path):
    path = tmp_path / "export.db"
    with make_client(path) as client:
        example = post(client, "/examples/hermitian", {})
        names = example["objects"]
        sorting = names["sorting"]
        post(
            client,
            "/annotations",
            {
                "branch_id": example["branch_id"],
                "revision_id": sorting["revision_id"],
                "body": "局部异议：不能混用两套标签。",
            },
        )
        revised = post(
            client,
            f"/objects/{sorting['object_id']}/revisions",
            {
                "branch_id": example["branch_id"],
                "expected_revision_id": sorting["revision_id"],
                "body": "排序定义的新版本；旧论证必须保留其原始定义。",
            },
        )
        export = post(
            client,
            "/exports",
            {
                "project_id": example["project_id"],
                "branch_id": example["branch_id"],
                "object_ids": [names["counterexample"]["object_id"]],
            },
            key="fixed-export",
        )
        assert "bundle" not in export
        expected_bytes, manifest, draft = archive(client, export)
        ids = {rev["id"] for rev in manifest["revisions"]}
        assert {
            sorting["revision_id"],
            revised["revision_id"],
            names["counterexample_argument"]["revision_id"],
        } <= ids
        assert names["exact_artifact"]["revision_id"] in ids
        assert names["conjecture"]["revision_id"] in ids
        assert manifest["snapshot_seq"] == export["snapshot_seq"]
        assert manifest["snapshot"]["annotations"][0]["body"] == "局部异议：不能混用两套标签。"
        assert manifest["reviews"][0]["coverage"] == "partial"
        assert (
            manifest["reviews"][0]["dependency_snapshot"][sorting["object_id"]]
            == sorting["revision_id"]
        )
        assert all(plan["dependencies"] for plan in manifest["proof_plans"])
        assert "研究工作稿" in draft and "局部异议" in draft and "不是数学真值" in draft
        assert ORIGINAL_QUESTION in draft
        post(
            client,
            f"/objects/{sorting['object_id']}/revisions",
            {
                "branch_id": example["branch_id"],
                "expected_revision_id": revised["revision_id"],
                "body": "此文字在导出之后提交，旧导出不应出现。",
            },
        )
        assert archive(client, export)[0] == expected_bytes
        assert (
            post(
                client,
                "/exports",
                {
                    "project_id": example["project_id"],
                    "branch_id": example["branch_id"],
                    "object_ids": [names["counterexample"]["object_id"]],
                },
                key="fixed-export",
            )
            == export
        )
    with make_client(path) as reopened:
        assert archive(reopened, export)[0] == expected_bytes


def test_export_auth_branch_validation_and_command_idempotency(tmp_path):
    with make_client(tmp_path / "auth.db") as client:
        example = post(client, "/examples/hermitian", {})
        data = {"project_id": example["project_id"], "branch_id": example["branch_id"]}
        assert (
            client.post("/exports", json=data, headers={"Idempotency-Key": "no-auth"}).status_code
            == 401
        )
        export = post(client, "/exports", data, key="export-key")
        assert client.get(export["download_url"]).status_code == 401
        assert (
            client.get(
                export["download_url"], headers={"Authorization": "Bearer test-worker"}
            ).status_code
            == 401
        )
        headers = {**AUTH, "Idempotency-Key": "export-key"}
        assert (
            client.post(
                "/exports",
                json={**data, "branch_id": example["analytic_branch_id"]},
                headers=headers,
            ).status_code
            == 409
        )
        other = post(client, "/projects", {"title": "Other", "body": "other"})
        assert (
            client.post(
                "/exports",
                json={**data, "branch_id": other["branch_id"]},
                headers={**AUTH, "Idempotency-Key": str(uuid4())},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/exports",
                json={**data, "object_ids": [other["object_id"]]},
                headers={**AUTH, "Idempotency-Key": str(uuid4())},
            ).status_code
            == 422
        )
        assert client.get("/exports/missing/download", headers=AUTH).status_code == 404


def cli(script, *args, success=True):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / script), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return result


def test_online_backup_scrubs_secrets_and_restore_fences_active_and_unknown_work(tmp_path):
    source = tmp_path / "source" / "mathagent.db"
    source.parent.mkdir()
    tokens = [
        "SESSION-SECRET-" + str(uuid4()),
        "WORKER-SECRET-" + str(uuid4()),
        "API-SECRET-" + str(uuid4()),
    ]
    (source.parent / "session.token").write_text(tokens[0])
    (source.parent / "worker.token").write_text(tokens[1])
    output = tmp_path / "backup.zip"
    destination = tmp_path / "restored"
    with make_client(source) as client:
        example = post(client, "/examples/hermitian", {})
        export = post(
            client,
            "/exports",
            {"project_id": example["project_id"], "branch_id": example["branch_id"]},
        )
        expected_export = archive(client, export)[0]
        post(
            client,
            "/objects",
            {
                "branch_id": example["branch_id"],
                "kind": "artifact",
                "body": "Diagnostic accidentally repeats " + tokens[2],
                "payload": {"nested": {"api_key": tokens[2]}},
            },
        )
        runs = [
            post(
                client,
                "/runs",
                {"branch_id": example["branch_id"], "goal_object_id": example["object_id"]},
            )
            for _ in range(2)
        ]
        attempts = [post(client, f"/runs/{run['run_id']}/claim", worker=True) for run in runs]
        tokens.extend(attempt["token"] for attempt in attempts)
        with client.app.state.database.sessions.begin() as session:
            session.get(Run, runs[1]["run_id"]).provider = "deepseek"
            session.add(
                ProviderRequest(
                    project_id=example["project_id"],
                    run_id=runs[1]["run_id"],
                    attempt_id=attempts[1]["attempt_id"],
                    provider="deepseek",
                    state="dispatched",
                )
            )
        cli("backup.py", "--database", source, "--output", output)
        with client.app.state.database.sessions() as session:
            assert session.get(Attempt, attempts[0]["attempt_id"]).token == attempts[0]["token"]
            assert session.get(Run, runs[0]["run_id"]).state == "running"
        with zipfile.ZipFile(output) as bundle:
            assert set(bundle.namelist()) == {"mathagent.db", "manifest.json"}
            copied = bundle.read("mathagent.db")
            manifest = json.loads(bundle.read("manifest.json"))
            assert all(token.encode() not in copied for token in tokens)
            assert manifest["files"]["mathagent.db"]["sha256"] == hashlib.sha256(copied).hexdigest()
            assert manifest["credentials_included"] is False
        cli("restore.py", "--backup", output, "--destination", destination)
        assert not (destination / "session.token").exists()
        assert not (destination / "worker.token").exists()
    with make_client(destination / "mathagent.db") as restored:
        assert archive(restored, export)[0] == expected_export
        with restored.app.state.database.sessions() as session:
            assert session.get(Run, runs[0]["run_id"]).state == "interrupted"
            assert session.get(Run, runs[1]["run_id"]).state == "reconciliation_required"
            request = session.scalar(select(ProviderRequest))
            assert request.state == "unknown"
            assert all(a.token == "" for a in session.scalars(select(Attempt)))
        blocked = post(restored, f"/runs/{runs[1]['run_id']}/claim", worker=True)
        assert blocked["state"] == "reconciliation_required"
        assert post(restored, f"/runs/{runs[1]['run_id']}/resume")["effect"] == "blocked"
        assert post(restored, f"/runs/{runs[0]['run_id']}/resume")["state"] == "queued"
        resumed = post(restored, f"/runs/{runs[0]['run_id']}/claim", worker=True)
        assert resumed["attempt_number"] == 2
        assert resumed["token"] not in tokens
    cli("backup.py", "--database", source, "--output", output, success=False)
    cli("restore.py", "--backup", output, "--destination", destination, success=False)


@pytest.mark.parametrize("failure", ["traversal", "duplicate", "hash"])
def test_invalid_archives_never_write_destination(tmp_path, failure):
    source = tmp_path / "safe.db"
    with make_client(source) as client:
        post(client, "/projects", {"title": "safe", "body": "goal"})
    good = tmp_path / "good.zip"
    cli("backup.py", "--database", source, "--output", good)
    with zipfile.ZipFile(good) as bundle:
        entries = {name: bundle.read(name) for name in bundle.namelist()}
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as bundle:
        if failure == "hash":
            entries["mathagent.db"] += b"corrupt"
        for name, body in entries.items():
            bundle.writestr(name, body)
        if failure == "traversal":
            bundle.writestr("../escaped.txt", b"must not extract")
        if failure == "duplicate":
            with pytest.warns(UserWarning):
                bundle.writestr("mathagent.db", entries["mathagent.db"])
    destination = tmp_path / "must-not-exist"
    cli("restore.py", "--backup", bad, "--destination", destination, success=False)
    assert not destination.exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_restore_keeps_cancel_intent_after_reconciliation_and_releases_fake_dispatch(tmp_path):
    source, backup, restored_path = (
        tmp_path / "source.db",
        tmp_path / "backup.zip",
        tmp_path / "restored",
    )
    with make_client(source) as client:
        project = post(client, "/projects", {"title": "Control recovery", "body": "goal"})
        runs = [
            post(
                client,
                "/runs",
                {"branch_id": project["branch_id"], "goal_object_id": project["object_id"]},
            )
            for _ in range(2)
        ]
        attempts = [post(client, f"/runs/{run['run_id']}/claim", worker=True) for run in runs]
        with client.app.state.database.sessions.begin() as session:
            cancelled = session.get(Run, runs[1]["run_id"])
            cancelled.provider, cancelled.state = "deepseek", "cancel_requested"
            request_ids = []
            for index, provider in enumerate(("fake", "deepseek")):
                row = ProviderRequest(
                    project_id=project["project_id"],
                    run_id=runs[index]["run_id"],
                    attempt_id=attempts[index]["attempt_id"],
                    provider=provider,
                    state="dispatched",
                )
                session.add(row)
                session.flush()
                request_ids.append(row.id)
        cli("backup.py", "--database", source, "--output", backup)
    cli("restore.py", "--backup", backup, "--destination", restored_path)
    with make_client(restored_path / "mathagent.db") as restored:
        with restored.app.state.database.sessions() as session:
            assert session.get(ProviderRequest, request_ids[0]).state == "released"
            assert session.get(Run, runs[0]["run_id"]).state == "interrupted"
            assert session.get(ProviderRequest, request_ids[1]).state == "unknown"
        reconciled = post(
            restored,
            f"/requests/{request_ids[1]}/reconcile",
            {"outcome": "spent", "reason": "Offline fixture: external charge confirmed."},
        )
        assert reconciled["run_state"] == "cancelled"
        assert (
            restored.post(
                f"/runs/{runs[1]['run_id']}/resume",
                headers={**AUTH, "Idempotency-Key": str(uuid4())},
            ).status_code
            == 409
        )
