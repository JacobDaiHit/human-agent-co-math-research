"""Verify and restore a portable archive into a new empty directory.

Usage: python scripts/restore.py --backup backup.zip --destination restored-data
Start the application explicitly with restored-data/mathagent.db afterwards.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api" / "src"))
from mathagent.persistence.artifacts import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_FILES,
    MAX_TOTAL_ARTIFACT_BYTES,
    ArtifactStore,
    database_references,
    validate_content,
    validate_metadata,
)

FORMAT = "mathagent.sqlite-backup"
MAX_DATABASE_BYTES = 1024 * 1024 * 1024


def interrupt_restored_work(connection):
    """Fence historical leases and never replay an uncertain external side effect."""
    timestamp = datetime.now(UTC).isoformat()
    connection.row_factory = sqlite3.Row
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    uncertain = set()
    if "provider_requests" in tables:
        connection.execute(
            "UPDATE provider_requests SET state='released', reason='backup_restore_before_dispatch' "
            "WHERE state='reserved'"
        )
        connection.execute(
            "UPDATE provider_requests SET state='released', reason='backup_restore_fake_dispatch' "
            "WHERE state='dispatched' AND provider='fake'"
        )
        connection.execute(
            "UPDATE provider_requests SET state='unknown', reason='backup_restore_during_dispatch' "
            "WHERE state='dispatched'"
        )
        uncertain = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT run_id FROM provider_requests WHERE state='unknown'"
            )
        }
    active = {"queued", "running", "waiting_children", "pause_requested", "cancel_requested", "steer_requested"}
    changed = []
    for run in connection.execute("SELECT * FROM runs").fetchall():
        if run["id"] in uncertain:
            state = "reconciliation_required"
        elif run["state"] in active:
            state = {"pause_requested": "paused", "cancel_requested": "cancelled"}.get(
                run["state"], "interrupted"
            )
        else:
            continue
        connection.execute(
            "UPDATE runs SET state=?,control_epoch=control_epoch+1 WHERE id=?", (state, run["id"])
        )
        for attempt in connection.execute(
            "SELECT * FROM attempts WHERE run_id=?", (run["id"],)
        ).fetchall():
            if attempt["state"] not in {"running", "reconciliation_required"} and not (
                run["id"] in uncertain and attempt["id"] == run["current_attempt_id"]
            ):
                continue
            checkpoint = json.loads(attempt["checkpoint"])
            if run["id"] in uncertain:
                checkpoint.setdefault(
                    "reconciliation_resume_state",
                    "cancelled" if run["state"] in {"cancel_requested", "cancelled"} else "paused",
                )
            checkpoint.update(
                {
                    "end_reason": "backup_restore",
                    "restored_at": timestamp,
                    "reconciled": True,
                    "automatic_redispatch": False,
                }
            )
            connection.execute(
                "UPDATE attempts SET state=?,token='',lease_until=?,checkpoint=? WHERE id=?",
                (
                    "reconciliation_required" if run["id"] in uncertain else "interrupted",
                    timestamp,
                    json.dumps(checkpoint, ensure_ascii=False),
                    attempt["id"],
                ),
            )
        branch = connection.execute(
            "SELECT project_id FROM branches WHERE id=?", (run["branch_id"],)
        ).fetchone()
        connection.execute(
            "UPDATE branches SET control_epoch=control_epoch+1 WHERE id=?", (run["branch_id"],)
        )
        connection.execute("UPDATE projects SET event_seq=event_seq+1 WHERE id=?", (branch[0],))
        seq = connection.execute(
            "SELECT event_seq FROM projects WHERE id=?", (branch[0],)
        ).fetchone()[0]
        payload = {
            "run_id": run["id"],
            "previous_state": run["state"],
            "state": state,
            "reason": "backup_restore",
            "automatic_redispatch": False,
        }
        connection.execute(
            "INSERT INTO events (project_id,seq,id,branch_id,type,payload,author,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                branch[0],
                seq,
                str(uuid4()),
                run["branch_id"],
                "run.restored_interrupted",
                json.dumps(payload),
                "system",
                timestamp,
            ),
        )
        changed.append(payload)
    # All old capabilities are invalid even for attempts that were already terminal.
    connection.execute("UPDATE attempts SET token=''")
    # Re-enabling paid providers is an explicit choice in the new environment.
    if "runtime_settings" in tables:
        connection.execute("UPDATE runtime_settings SET allow_real_api=0")
    connection.commit()
    connection.execute("VACUUM")
    return changed


def restore_database(backup, destination):
    backup, destination = Path(backup).resolve(), Path(destination).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Restore destination must be a new empty directory.")
    with zipfile.ZipFile(backup) as archive:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        if (
            len(entries) != len(names)
            or not {"manifest.json", "mathagent.db"} <= names
            or len(entries) > MAX_ARTIFACT_FILES + 2
        ):
            raise ValueError("Archive contains unexpected, duplicate, or unsafe paths.")
        for entry in entries:
            if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive directories and symbolic links are not allowed.")
            limit = (
                MAX_DATABASE_BYTES
                if entry.filename == "mathagent.db"
                else 1024 * 1024
                if entry.filename == "manifest.json"
                else MAX_ARTIFACT_BYTES
            )
            if entry.file_size > limit:
                raise ValueError("Archive entry exceeds the restore size limit.")
        manifest = json.loads(archive.read("manifest.json"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != FORMAT
            or manifest.get("format_version") != 1
            or manifest.get("credentials_included") is not False
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != names - {"manifest.json"}
        ):
            raise ValueError("Unsupported or invalid backup manifest.")
        data = archive.read("mathagent.db")
        artifact_files = {}
        if (
            sum(item.file_size for item in entries if item.filename.startswith("artifacts/"))
            > MAX_TOTAL_ARTIFACT_BYTES
        ):
            raise ValueError("Artifact collection exceeds the restore size limit.")
        for name in sorted(names - {"mathagent.db", "manifest.json"}):
            metadata = validate_metadata(
                {**manifest["files"][name], "relative_path": name, "media_type": "application/json"}
            )
            artifact_files[name] = validate_content(metadata, archive.read(name))
    expected = manifest["files"]["mathagent.db"]
    if (
        not isinstance(expected, dict)
        or type(expected.get("size")) is not int
        or len(data) != expected.get("size")
        or hashlib.sha256(data).hexdigest() != expected.get("sha256")
    ):
        raise ValueError("Backup hash or size mismatch; nothing was restored.")
    with tempfile.TemporaryDirectory(prefix="mathagent-restore-") as staging:
        staged = Path(staging) / "mathagent.db"
        staged.write_bytes(data)
        with closing(sqlite3.connect(staged)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup database integrity check failed.")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Backup database contains broken foreign keys.")
            references = database_references(connection)
            if set(references) != set(artifact_files):
                raise ValueError("Backup artifact reference set is incomplete or unexpected.")
            for name, metadata in references.items():
                validate_content(metadata, artifact_files[name])
            changed = interrupt_restored_work(connection)
        report = {
            "source_database_sha256": expected["sha256"],
            "restored_at": datetime.now(UTC).isoformat(),
            "runs": changed,
            "credentials_restored": False,
            "workers_started": False,
            "artifact_files_restored": len(artifact_files),
        }
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise ValueError("Restore destination must still be empty.")
        # Publish files first: even an interrupted restore cannot expose a DB
        # containing references to files that have not been written yet.
        store = ArtifactStore(destination / "mathagent.db")
        for name, content in artifact_files.items():
            saved = store.write_json(json.loads(content))
            if saved != references[name]:
                raise ValueError("Restored artifact metadata mismatch.")
        with (destination / "mathagent.db").open("xb") as stream:
            stream.write(staged.read_bytes())
        with (destination / "restore-report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = restore_database(args.backup, args.destination)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, zipfile.BadZipFile) as error:
        parser.exit(1, f"Restore failed: {error}\n")
    print(
        json.dumps(
            {
                "database": str(args.destination.resolve() / "mathagent.db"),
                "interrupted_runs": len(report["runs"]),
                "workers_started": False,
            }
        )
    )


if __name__ == "__main__":
    main()
