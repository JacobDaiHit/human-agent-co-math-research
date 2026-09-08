"""Portable SQLite online backup; credentials and token files are never archived.

Usage: python scripts/backup.py --database data/mathagent.db --output backup.zip
Requires only Python's standard library. The source database is read-only.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api" / "src"))
from mathagent.persistence.artifacts import (  # noqa: E402
    ArtifactStore,
    database_references,
)

FORMAT = "mathagent.sqlite-backup"
VERSION = 1
SECRET_KEYS = {
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "worker_token",
    "session_token",
    "password",
    "secret",
    "client_secret",
}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def sensitive(name):
    normalized = name.lower().replace("-", "_")
    return normalized in SECRET_KEYS or normalized.endswith(("_api_key", "_access_token"))


def scrub_json(value, secrets_found):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if sensitive(key):
                if isinstance(item, str) and item:
                    secrets_found.add(item)
            else:
                result[key] = scrub_json(item, secrets_found)
        return result
    if isinstance(value, list):
        return [scrub_json(item, secrets_found) for item in value]
    return value


def sanitize_database(connection, local_secrets=(), *, collected_secrets=None):
    """Sanitize portable copy, then vacuum to remove secrets from free pages too."""
    connection.execute("PRAGMA secure_delete=ON")
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    secrets_found = {item for item in local_secrets if item}
    text_cells = []
    for table in tables:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quote(table)})")]
        for row in connection.execute(f"SELECT rowid,* FROM {quote(table)}").fetchall():
            rowid, values = row[0], row[1:]
            for column, value in zip(columns, values, strict=True):
                if not isinstance(value, str):
                    continue
                replacement = value
                if sensitive(column):
                    if value:
                        secrets_found.add(value)
                    replacement = ""
                else:
                    try:
                        decoded = json.loads(value)
                    except (ValueError, TypeError):
                        pass
                    else:
                        if isinstance(decoded, (dict, list)):
                            replacement = json.dumps(
                                scrub_json(decoded, secrets_found), ensure_ascii=False
                            )
                text_cells.append((table, column, rowid, replacement))
    # Collected credential strings may also appear in diagnostics or old receipts.
    # Do a second pass after all credential-bearing columns/metadata were examined.
    ordered_secrets = sorted(secrets_found, key=len, reverse=True)
    for table, column, rowid, value in text_cells:
        for secret in ordered_secrets:
            value = value.replace(secret, "[REDACTED]")
        connection.execute(
            f"UPDATE {quote(table)} SET {quote(column)}=? WHERE rowid=?", (value, rowid)
        )
    connection.commit()
    connection.execute("VACUUM")
    if collected_secrets is not None:
        collected_secrets.update(secrets_found)
    return len(secrets_found)


def backup_database(database, output):
    database, output = Path(database).resolve(), Path(output).resolve()
    if not database.is_file():
        raise ValueError("Source database does not exist.")
    if output.exists():
        raise ValueError("Backup output already exists; choose a new filename.")
    local_secrets = []
    for name in ("session.token", "worker.token"):
        path = database.parent / name
        if path.is_file():
            local_secrets.append(path.read_text(encoding="utf-8").strip())
    with tempfile.TemporaryDirectory(prefix="mathagent-backup-") as staging:
        copy_path = Path(staging) / "mathagent.db"
        with closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15)
        ) as source:
            with closing(sqlite3.connect(copy_path)) as target:
                source.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                schema = [
                    row[0] for row in target.execute("SELECT version_num FROM alembic_version")
                ]
                collected_secrets = set()
                sanitize_database(target, local_secrets, collected_secrets=collected_secrets)
                references = database_references(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup database integrity check failed.")
                if target.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Backup database contains broken foreign keys.")
        artifact_files = {}
        store = ArtifactStore(database)
        for name, metadata in references.items():
            content = store.read(metadata)
            decoded = json.loads(content)
            # A portable copy must not silently change an immutable file/hash.
            # Refuse a credential-bearing file rather than leak or relabel it.
            if scrub_json(decoded, set()) != decoded or any(
                secret in content.decode("utf-8") for secret in collected_secrets
            ):
                raise ValueError("Artifact contains credential material; backup was not created.")
            artifact_files[name] = content
        data = copy_path.read_bytes()
        manifest = {
            "format": FORMAT,
            "format_version": VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "schema_revisions": schema,
            "files": {
                "mathagent.db": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
                **{
                    name: {"sha256": item["sha256"], "size": item["size"]}
                    for name, item in references.items()
                },
            },
            "method": "sqlite_online_backup",
            "credentials_included": False,
            "credential_policy": "Token files omitted; credential columns and nested secret fields "
            "removed; known credential bytes scrubbed; portable copy vacuumed.",
            "restore_policy": "New empty directory; active work interrupted; uncertain external "
            "requests require human reconciliation; no worker auto-start.",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("mathagent.db", data)
                for name, content in artifact_files.items():
                    archive.writestr(name, content)
                archive.writestr("manifest.json", json_bytes(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = backup_database(args.database, args.output)
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as error:
        parser.exit(1, f"Backup failed: {error}\n")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "sha256": manifest["files"]["mathagent.db"]["sha256"],
            }
        )
    )


if __name__ == "__main__":
    main()
