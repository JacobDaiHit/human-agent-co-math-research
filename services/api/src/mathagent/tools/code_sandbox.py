"""A fail-closed, local Docker sandbox for short Python calculations.

The Docker image is selected only from trusted process configuration.  Solver input
never controls an image, a path, a mount, or the Docker command line.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

TOOL_VERSION = "docker-python-sandbox-v1"
MAX_CODE_BYTES = 64 * 1024
MAX_HOST_DIAGNOSTIC_BYTES = 32 * 1024
_DIGEST = re.compile(r"(?:^|@)sha256:[0-9a-f]{64}$")


class CodeSandbox:
    """Run bounded Python only in a pre-existing, content-addressed Docker image."""

    def __init__(self, database_path, image_id=None):
        self.database_path = Path(database_path).resolve()
        self.image_id = image_id if image_id is not None else os.getenv("MATHAGENT_SANDBOX_IMAGE")
        self.jobs_path = self.database_path.parent / "sandbox-jobs"

    def status(self):
        """Return readiness without pulling an image or starting a container."""
        if not self.image_id:
            return {"ready": False, "reason": "image_not_configured", "image_id": None}
        if not _DIGEST.search(self.image_id):
            return {"ready": False, "reason": "image_not_immutable", "image_id": self.image_id}
        try:
            inspected = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", self.image_id],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env=_docker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"ready": False, "reason": "docker_unavailable", "image_id": self.image_id}
        if inspected.returncode:
            return {"ready": False, "reason": "image_not_local", "image_id": self.image_id}
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", inspected.stdout.strip()):
            return {"ready": False, "reason": "image_inspect_invalid", "image_id": self.image_id}
        return {"ready": True, "reason": None, "image_id": self.image_id}

    def execute(self, project_id, attempt_id, code, timeout_seconds=5):
        """Reserve once, run once, and return a durable result or an explicit unknown."""
        if not isinstance(project_id, str) or not project_id or not isinstance(attempt_id, str) or not attempt_id:
            return _failure("invalid_identity")
        if not isinstance(code, str) or len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return _failure("code_too_large")
        timeout = _bounded_timeout(timeout_seconds)
        code_hash = _hash(code.encode("utf-8"))
        provenance = {"tool_version": TOOL_VERSION, "image_id": self.image_id, "timeout_seconds": timeout,
                      "code_hash": code_hash, "limits": {"code_bytes": MAX_CODE_BYTES, "output_bytes": 32768,
                      "timeout_seconds": timeout}}
        job_key = _hash(json.dumps([project_id, attempt_id, provenance], sort_keys=True).encode("utf-8"))
        project_path = self.jobs_path / _hash(project_id.encode("utf-8"))
        journal_path = project_path / (job_key + ".json")
        try:
            for path in (self.jobs_path, project_path):
                _plain(path, directory=True)
            _plain(journal_path)
        except OSError:
            return _failure("journal_unavailable")
        if journal_path.exists() and (journal_path.is_symlink() or _read_json(journal_path) is None):
            return _failure("execution_unknown", code_hash=code_hash)
        existing = _read_json(journal_path)
        if existing is not None:
            return _duplicate(existing, code_hash)

        ready = self.status()
        if not ready["ready"]:
            return _failure(ready["reason"], image_id=ready["image_id"])

        try:
            if self.jobs_path.is_symlink() or project_path.is_symlink():
                return _failure("journal_unavailable")
            self.jobs_path.mkdir(parents=True, exist_ok=True)
            if self.jobs_path.is_symlink():
                return _failure("journal_unavailable")
            project_path.mkdir(exist_ok=True)
            if project_path.is_symlink():
                return _failure("journal_unavailable")
        except OSError:
            return _failure("journal_unavailable")
        # Reserve this execution identity once. Resource bounds belong to each
        # execution and the research budget, not a lifetime project counter.
        slot, newly_reserved = self._reserve_slot(project_path, job_key)
        if slot is None:
            return _failure("journal_unavailable")
        if not newly_reserved:
            return _failure("execution_unknown", code_hash=code_hash)
        record = {
            "version": TOOL_VERSION,
            "project_id": project_id,
            "attempt_id": attempt_id,
            "code": code,
            "code_hash": code_hash,
            "job_key": job_key,
            "provenance": provenance,
            "budget_slot": slot.name,
            "status": "reserved",
            "created_at": time.time(),
        }
        if not _atomic_json(journal_path, record):
            existing = _read_json(journal_path)
            return _duplicate(existing, code_hash) if existing else _failure("execution_unknown")

        result = self._run(code, timeout)
        result["provenance"] = provenance
        record["status"] = "complete"
        record["result"] = result
        record["completed_at"] = time.time()
        if not _replace_json(journal_path, record):
            return _failure("execution_unknown", image_id=self.image_id, code_hash=code_hash)
        return result

    def erase_project(self, project_id):
        """Redact source and output while retaining hashes and consumed budget slots."""
        if not isinstance(project_id, str) or not project_id:
            return {"redacted": 0, "pending": True}
        project_path = self.jobs_path / _hash(project_id.encode("utf-8"))
        redacted = pending = 0
        try:
            _plain(self.jobs_path, directory=True)
            _plain(project_path, directory=True)
        except OSError:
            return {"redacted": 0, "pending": True}
        for path in project_path.glob("*.json") if project_path.exists() else ():
            record = _read_json(path)
            if path.is_symlink() or record is None or record.get("project_id") != project_id:
                pending += 1
                continue
            record.pop("code", None)
            result = record.get("result")
            if isinstance(result, dict):
                for key in ("stdout", "stderr"):
                    result.pop(key, None)
            record["redacted_at"] = time.time()
            if _replace_json(path, record):
                redacted += 1
            else:
                pending += 1
        # Interrupted atomic replacements can retain old source/output text.
        for path in project_path.glob("*.tmp") if project_path.exists() else ():
            try:
                _plain(path)
                path.unlink()
            except OSError:
                pending += 1
        return {"redacted": redacted, "pending": pending}

    def _reserve_slot(self, project_path, job_key):
        slot = project_path / ("execution-" + job_key)
        try:
            fd = os.open(slot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return slot, False
        except OSError:
            return None, False
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(job_key)
            handle.flush()
            os.fsync(handle.fileno())
        return slot, True

    def _run(self, code, timeout):
        command = [
            "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=16m", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
            "--pids-limit", "32", "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--cap-add", "KILL", "--security-opt", "no-new-privileges", "--log-driver", "none", "--user", "0:0", "--workdir", "/work",
            "-e", f"MATHAGENT_TIMEOUT_SECONDS={timeout}", "-i", self.image_id, "python", "/opt/mathagent/runner.py",
        ]
        try:
            completed = subprocess.run(
                command, input=code, capture_output=True, text=True, timeout=timeout + 2,
                check=False, env=_docker_environment(),
            )
        except subprocess.TimeoutExpired:
            return _failure("docker_timeout", image_id=self.image_id)
        except OSError:
            return _failure("docker_unavailable", image_id=self.image_id)
        if completed.returncode:
            return _failure("container_failed", image_id=self.image_id, stderr=_cap(completed.stderr))
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            return _failure("invalid_supervisor_result", image_id=self.image_id, stderr=_cap(completed.stderr))
        if not isinstance(payload, dict):
            return _failure("invalid_supervisor_result", image_id=self.image_id)
        payload.update({"ok": payload.get("reason") is None, "image_id": self.image_id, "tool_version": TOOL_VERSION})
        return payload


def _docker_environment():
    """Do not forward the API environment (which can contain provider credentials)."""
    environment = {"PATH": os.environ.get("PATH", "")}
    if os.name == "nt" and os.environ.get("SystemRoot"):
        environment["SystemRoot"] = os.environ["SystemRoot"]
    return environment


def _duplicate(record, code_hash):
    if not isinstance(record, dict) or record.get("code_hash") != code_hash:
        return _failure("execution_unknown")
    if record.get("status") != "complete":
        return _failure("execution_unknown", code_hash=code_hash)
    result = record.get("result")
    if not isinstance(result, dict):
        return _failure("execution_unknown", code_hash=code_hash)
    if record.get("redacted_at"):
        return _failure("material_erased", code_hash=code_hash)
    return {**result, "cached": True}


def _failure(reason, **extra):
    return {"ok": False, "reason": reason, "tool_version": TOOL_VERSION, **extra}


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _bounded_timeout(value):
    try:
        return max(1, min(10, int(value)))
    except (TypeError, ValueError):
        return 5


def _cap(value):
    return (value or "")[:MAX_HOST_DIAGNOSTIC_BYTES]


def _read_json(path):
    try:
        _plain(path)
        if path.stat().st_size > 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_json(path, value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def _replace_json(path, value):
    temporary = None
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        _plain(path)
        with tempfile.NamedTemporaryFile(prefix=".pending-", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _plain(path, *, directory=False):
    """Reject links and Windows junctions before reading or replacing local journals."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("unsafe_journal_storage")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise OSError("unsafe_journal_storage")
