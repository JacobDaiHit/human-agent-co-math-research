"""Actual local file, immutable export, and portable backup consistency checks."""

import base64
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
from mathagent.application.errors import DomainError
from mathagent.application.state import StateService
from mathagent.exports.service import ExportService, export_zip
from mathagent.persistence.artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    ArtifactStore,
    collect_references,
    validate_metadata,
)
from mathagent.persistence.database import Database
from mathagent.persistence.models import CommandReceipt, Revision
from mathagent.tools.exact import execute_calculation
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path):
    database = Database(tmp_path / "source" / "research.db")
    database.migrate()
    state = StateService(database)
    _, project = state.execute(
        "project.create",
        str(uuid4()),
        {"title": "文件闭环", "body": "检查 $1/3+2/5$。"},
        state.create_project,
    )
    yield state, project
    database.close()


def calculate(workspace, *, expression="1/3+2/5", key=None):
    state, project = workspace

    def handler(session, values):
        branch = state.require_branch(session, project["branch_id"])
        result = execute_calculation(state, session, branch, values, author="human", run_id=None)
        return 201, result

    return state.execute(
        "calculation.execute",
        key or str(uuid4()),
        {
            "tool": "rational_arithmetic",
            "inputs": {"expression": expression},
            "target_revision_id": project["revision_id"],
        },
        handler,
    )[1]


def export(workspace):
    state, project = workspace
    return state.execute(
        "export.create",
        str(uuid4()),
        {"project_id": project["project_id"], "branch_id": project["branch_id"]},
        ExportService(state).create,
    )[1]


def script(name, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, "MATHAGENT_LOAD_ENV": "0", "MATHAGENT_ENABLE_REAL_API": "0"},
    )


def test_calculation_files_match_database_and_reuse_idempotent_receipt(workspace):
    state, _ = workspace
    first = calculate(workspace, key="same-command")
    assert calculate(workspace, key="same-command") == first
    (metadata,) = first["artifact_files"]
    data = ArtifactStore(state.db.path).read(metadata)
    content = json.loads(data)
    assert content["exact_result"]["value"] == "11/15"
    assert content["input"] == {"expression": "1/3+2/5"}
    assert content["runtime"]["network"] is False
    assert content["status"] == "ok" and content["stdout"]
    assert "artifact_files" not in content
    assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
    assert metadata["size"] == len(data)
    with state.db.sessions() as session:
        assert session.get(Revision, first["revision_id"]).payload["artifact_files"] == [metadata]
    assert list((state.db.path.parent / "artifacts").iterdir()) == [
        state.db.path.parent / metadata["relative_path"]
    ]


def test_failed_calculation_has_truthful_error_file(workspace):
    state, _ = workspace
    result = calculate(workspace, expression="1/0")
    content = json.loads(ArtifactStore(state.db.path).read(result["artifact_files"][0]))
    assert content["status"] == "error"
    assert content["error"] and content["exact_result"] is None and content["stdout"] == ""


def test_file_write_failure_rolls_back_all_calculation_records(workspace, monkeypatch):
    state, _ = workspace
    with state.db.sessions() as session:
        before = session.scalar(select(func.count()).select_from(Revision))

    def fail(*args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("mathagent.persistence.artifacts.os.replace", fail)
    with pytest.raises(DomainError) as raised:
        calculate(workspace)
    assert raised.value.response["error"] == "artifact_storage_failed"
    with state.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Revision)) == before
    assert list((state.db.path.parent / "artifacts").iterdir()) == []


def test_transaction_failure_can_leave_orphan_but_no_broken_reference(workspace, monkeypatch):
    state, _ = workspace

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(state, "new_object", fail)
    with pytest.raises(RuntimeError):
        calculate(workspace)
    files = list((state.db.path.parent / "artifacts").glob("*.json"))
    assert len(files) == 1
    with state.db.sessions() as session:
        payloads = session.scalars(select(Revision.payload)).all()
        assert collect_references(payloads) == {}
        assert (
            session.scalar(
                select(CommandReceipt).where(CommandReceipt.operation == "calculation.execute")
            )
            is None
        )


def test_export_includes_verified_file_and_uses_frozen_bytes_after_source_loss(workspace):
    state, _ = workspace
    result = calculate(workspace)
    metadata = result["artifact_files"][0]
    created = export(workspace)
    expected = export_zip(created["bundle"])
    with zipfile.ZipFile(io.BytesIO(expected)) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "research.md",
            metadata["relative_path"],
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert "artifact_files" not in manifest  # payload is in its own ZIP entry
        assert manifest["files"][metadata["relative_path"]] == {
            "sha256": metadata["sha256"],
            "size": metadata["size"],
        }
        assert archive.read(metadata["relative_path"]) == ArtifactStore(state.db.path).read(
            metadata
        )
        assert r"\frac" in archive.read("research.md").decode()
    (state.db.path.parent / metadata["relative_path"]).unlink()
    assert export_zip(ExportService(state).get(created["export_id"])) == expected
    with pytest.raises(DomainError) as raised:
        export(workspace)
    assert raised.value.response["error"] == "artifact_export_failed"


def test_corrupt_source_and_frozen_export_never_report_success(workspace):
    state, _ = workspace
    result = calculate(workspace)
    metadata = result["artifact_files"][0]
    created = export(workspace)
    (state.db.path.parent / metadata["relative_path"]).write_bytes(b"corrupted")
    with pytest.raises(DomainError, match="产物文件"):
        export(workspace)
    created["bundle"]["artifact_files"][metadata["relative_path"]]["content_base64"] = (
        base64.b64encode(b"corrupted").decode()
    )
    with pytest.raises(DomainError):
        export_zip(created["bundle"])


def test_backup_restores_artifacts_and_identical_export_in_new_directory(workspace, tmp_path):
    state, _ = workspace
    result = calculate(workspace)
    metadata = result["artifact_files"][0]
    created = export(workspace)
    expected = export_zip(created["bundle"])
    backup = tmp_path / "portable.zip"
    outcome = script("backup.py", "--database", state.db.path, "--output", backup)
    assert outcome.returncode == 0, outcome.stderr
    with zipfile.ZipFile(backup) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert set(archive.namelist()) == {
            "mathagent.db",
            "manifest.json",
            metadata["relative_path"],
        }
        assert manifest["files"][metadata["relative_path"]]["sha256"] == metadata["sha256"]
    destination = tmp_path / "restored"
    outcome = script("restore.py", "--backup", backup, "--destination", destination)
    assert outcome.returncode == 0, outcome.stderr
    assert ArtifactStore(destination / "mathagent.db").read(metadata) == (
        ArtifactStore(state.db.path).read(metadata)
    )
    report = json.loads((destination / "restore-report.json").read_text())
    assert report["artifact_files_restored"] == 1 and report["workers_started"] is False
    restored = Database(destination / "mathagent.db")
    try:
        assert (
            export_zip(ExportService(StateService(restored)).get(created["export_id"])) == expected
        )
    finally:
        restored.close()


@pytest.mark.parametrize("change", ["missing", "corrupt"])
def test_backup_refuses_missing_or_corrupted_referenced_file(workspace, tmp_path, change):
    state, _ = workspace
    metadata = calculate(workspace)["artifact_files"][0]
    path = state.db.path.parent / metadata["relative_path"]
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    output = tmp_path / "bad-backup.zip"
    outcome = script("backup.py", "--database", state.db.path, "--output", output)
    assert outcome.returncode != 0 and not output.exists()


@pytest.mark.parametrize("change", ["missing", "hash", "db_reference", "traversal", "symlink"])
def test_restore_rejects_incomplete_tampered_or_unsafe_artifacts(workspace, tmp_path, change):
    state, _ = workspace
    metadata = calculate(workspace)["artifact_files"][0]
    backup = tmp_path / "source.zip"
    outcome = script("backup.py", "--database", state.db.path, "--output", backup)
    assert outcome.returncode == 0, outcome.stderr
    with zipfile.ZipFile(backup) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents["manifest.json"])
    path = metadata["relative_path"]
    if change == "missing":
        contents.pop(path)
        manifest["files"].pop(path)
    elif change == "hash":
        contents[path] = b"corrupt"
    elif change == "db_reference":
        replacement = b'{\n  "replacement": true\n}\n'
        digest = hashlib.sha256(replacement).hexdigest()
        contents.pop(path)
        manifest["files"].pop(path)
        path = f"artifacts/{digest}.json"
        contents[path] = replacement
        manifest["files"][path] = {"sha256": digest, "size": len(replacement)}
    elif change == "traversal":
        replacement_path = "../" + path
        contents[replacement_path] = contents.pop(path)
        manifest["files"][replacement_path] = manifest["files"].pop(path)
    contents["manifest.json"] = json.dumps(manifest).encode()
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(damaged, "w") as archive:
        for name, content in contents.items():
            info = zipfile.ZipInfo(name)
            if change == "symlink" and name == path:
                info.external_attr = 0o120777 << 16
            archive.writestr(info, content)
    destination = tmp_path / "refused"
    outcome = script("restore.py", "--backup", damaged, "--destination", destination)
    assert outcome.returncode != 0
    assert not destination.exists()


@pytest.mark.parametrize(
    "path",
    ["../outside.json", "artifacts/../../secret", "C:/secret", "artifacts/" + "a" * 64 + ".JSON"],
)
def test_metadata_does_not_allow_user_selected_paths(path):
    with pytest.raises(ArtifactError):
        validate_metadata(
            {"sha256": "a" * 64, "relative_path": path, "size": 1, "media_type": "application/json"}
        )


def test_content_addressed_store_reuses_and_does_not_overwrite_corruption(tmp_path):
    store = ArtifactStore(tmp_path / "database.db")
    metadata = store.write_json({"input": "1/3", "exact_result": "1/3"})
    path = tmp_path / metadata["relative_path"]
    modified = path.stat().st_mtime_ns
    assert store.write_json({"input": "1/3", "exact_result": "1/3"}) == metadata
    assert path.stat().st_mtime_ns == modified
    path.write_bytes(b"corrupted")
    with pytest.raises(ArtifactError):
        store.write_json({"input": "1/3", "exact_result": "1/3"})
    assert path.read_bytes() == b"corrupted"


def test_artifact_size_limit_is_enforced_before_any_write(tmp_path):
    store = ArtifactStore(tmp_path / "database.db")
    with pytest.raises(ArtifactError, match="artifact_size_limit"):
        store.write_json({"body": "x" * MAX_ARTIFACT_BYTES})
    assert not store.directory.exists()


def test_orphan_files_are_not_archived(workspace, tmp_path):
    state, _ = workspace
    orphan = ArtifactStore(state.db.path).write_json({"unreferenced": True})
    backup = tmp_path / "no-orphans.zip"
    outcome = script("backup.py", "--database", state.db.path, "--output", backup)
    assert outcome.returncode == 0, outcome.stderr
    with zipfile.ZipFile(backup) as archive:
        assert orphan["relative_path"] not in archive.namelist()


@pytest.mark.parametrize("credential_kind", ["field", "known_token"])
def test_backup_rejects_credential_bearing_files_without_rewriting_hashes(
    workspace, tmp_path, credential_kind
):
    state, project = workspace
    secret = "synthetic-secret-for-artifact-test"
    content = {"api_key": secret} if credential_kind == "field" else {"diagnostic": secret}
    if credential_kind == "known_token":
        (state.db.path.parent / "worker.token").write_text(secret, encoding="utf-8")
    metadata = ArtifactStore(state.db.path).write_json(content)

    def handler(session, values):
        branch = state.require_branch(session, project["branch_id"])
        state.new_object(
            session, branch, "artifact", "合成测试产物。", {"artifact_files": [metadata]}, "human"
        )
        return 201, {}

    state.execute("artifact.test", str(uuid4()), {}, handler)
    output = tmp_path / "must-not-leak.zip"
    outcome = script("backup.py", "--database", state.db.path, "--output", output)
    assert outcome.returncode != 0 and not output.exists()
    assert secret not in outcome.stderr
    if credential_kind == "field":
        with pytest.raises(DomainError):
            export(workspace)
