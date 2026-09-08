"""Bounded content-addressed JSON artifacts; no caller-selected filesystem paths.

Files precede database references. A rolled-back transaction can leave an orphan,
but a committed reference is never created before the durable file is available.
This module uses only the standard library so offline backup tools share checks.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

MAX_ARTIFACT_BYTES = 512 * 1024
MAX_ARTIFACT_FILES = 4096
MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class ArtifactError(ValueError):
    """Diagnostics contain fixed codes, never file contents or supplied paths."""


def json_bytes(value):
    try:
        data = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ArtifactError("artifact_invalid_json") from None
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("artifact_size_limit")
    return data


def validate_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ArtifactError("artifact_invalid_metadata")
    digest = metadata.get("sha256")
    size = metadata.get("size")
    if (
        not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
        or type(size) is not int
        or not 0 < size <= MAX_ARTIFACT_BYTES
        or metadata.get("relative_path") != f"artifacts/{digest}.json"
        or metadata.get("media_type") != "application/json"
    ):
        raise ArtifactError("artifact_invalid_metadata")
    return {key: metadata[key] for key in ("sha256", "relative_path", "size", "media_type")}


def validate_content(metadata, data):
    metadata = validate_metadata(metadata)
    if len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
        raise ArtifactError("artifact_integrity_mismatch")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise ArtifactError("artifact_invalid_json") from None
    if not isinstance(value, dict) or json_bytes(value) != data:
        raise ArtifactError("artifact_noncanonical_json")
    return data


def collect_references(payloads):
    """Read only the reserved top-level artifact_files field on revision payloads."""
    found = {}
    for payload in payloads:
        entries = payload.get("artifact_files", []) if isinstance(payload, dict) else []
        if not isinstance(entries, list) or len(entries) > MAX_ARTIFACT_FILES:
            raise ArtifactError("artifact_invalid_metadata")
        for entry in entries:
            metadata = validate_metadata(entry)
            name = metadata["relative_path"]
            if name in found and found[name] != metadata:
                raise ArtifactError("artifact_conflicting_metadata")
            found[name] = metadata
        if (
            len(found) > MAX_ARTIFACT_FILES
            or sum(item["size"] for item in found.values()) > MAX_TOTAL_ARTIFACT_BYTES
        ):
            raise ArtifactError("artifact_collection_limit")
    return dict(sorted(found.items()))


def database_references(connection):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revisions'"
    ).fetchone()
    if not exists:
        return {}
    try:
        return collect_references(
            json.loads(row[0]) for row in connection.execute("SELECT payload FROM revisions")
        )
    except (ValueError, TypeError, RecursionError):
        raise ArtifactError("artifact_invalid_database_metadata") from None


def _not_link(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ArtifactError("artifact_unsafe_storage")
    return info


class ArtifactStore:
    def __init__(self, database):
        self.directory = Path(database).resolve().parent / "artifacts"

    def _directory(self, *, create=False):
        if create:
            self.directory.mkdir(exist_ok=True)
        if not stat.S_ISDIR(_not_link(self.directory).st_mode):
            raise ArtifactError("artifact_unsafe_storage")

    def read(self, metadata):
        metadata = validate_metadata(metadata)
        try:
            self._directory()
            path = self.directory / (metadata["sha256"] + ".json")
            info = _not_link(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size != metadata["size"]:
                raise ArtifactError("artifact_integrity_mismatch")
            with path.open("rb") as stream:
                data = stream.read(MAX_ARTIFACT_BYTES + 1)
            return validate_content(metadata, data)
        except OSError:
            raise ArtifactError("artifact_unavailable") from None

    def write_json(self, value):
        data = json_bytes(value)
        digest = hashlib.sha256(data).hexdigest()
        metadata = {
            "sha256": digest,
            "relative_path": f"artifacts/{digest}.json",
            "size": len(data),
            "media_type": "application/json",
        }
        temporary = None
        try:
            self._directory(create=True)
            path = self.directory / (digest + ".json")
            if path.exists() or path.is_symlink():
                self.read(metadata)
                return metadata
            with tempfile.NamedTemporaryFile(
                prefix=".pending-", dir=self.directory, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            # Unix requires a directory sync for durable rename; Windows has no
            # portable directory fsync. File contents have been synced on both.
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self.read(metadata)
            return metadata
        except OSError:
            raise ArtifactError("artifact_write_failed") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # An unreferenced temporary file is safe after failure.
