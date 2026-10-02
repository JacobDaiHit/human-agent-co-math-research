"""Local, isolated Python calculations and durable background computations.

The Docker image is selected only from trusted process configuration.  Solver input
never controls an image, a path, a mount, or the Docker command line.
"""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

TOOL_VERSION = "docker-python-sandbox-v2"
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
            if record.get("container") and record.get("status") != "complete":
                stopped = self.cancel(project_id, record["job_key"])
                pending += int(stopped.get("status") != "complete")
                record = _read_json(path) or record
            record.pop("code", None)
            result = record.get("result")
            if isinstance(result, dict):
                for key in ("stdout", "stderr", "code"):
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

    def start(self, project_id, execution_id, code, timeout_seconds=3600, *, research_root_id=None):
        """Create an owned container once. The container outlives API/worker restarts."""
        if not isinstance(code, str) or len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return _failure("code_too_large")
        timeout = max(1, min(604800, int(timeout_seconds)))
        code_hash = _hash(code.encode("utf-8"))
        provenance = {"tool_version": TOOL_VERSION, "image_id": self.image_id,
                      "timeout_seconds": timeout, "code_hash": code_hash, "background": True}
        job_id = _hash(json.dumps([project_id, execution_id, provenance], sort_keys=True).encode())
        path = self._job_path(project_id, job_id)
        if path is None:
            return _failure("journal_unavailable")
        existing = _read_json(path)
        if existing:
            return self.poll(project_id, job_id)
        ready = self.status()
        if not ready["ready"]:
            return _failure(ready["reason"])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _plain(path.parent, directory=True)
        except OSError:
            return _failure("journal_unavailable")
        container = "mathagent-" + job_id
        record = {"project_id": project_id, "execution_id": execution_id, "job_key": job_id,
                  "research_root_id": research_root_id,
                  "code": code, "code_hash": code_hash, "provenance": provenance,
                  "container": container, "status": "creating", "created_at": time.time()}
        if not _atomic_json(path, record):
            return self.poll(project_id, job_id)
        command = ["docker", "create", "--name", container, "--pull", "never", "--network", "none",
            "--read-only", "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=256m",
            "--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "64",
            "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "KILL",
            "--security-opt", "no-new-privileges", "--log-driver", "local", "--log-opt", "max-size=16m",
            "--log-opt", "max-file=1", "--log-opt", "compress=false", "--user", "0:0", "--workdir", "/work",
            "-e", f"MATHAGENT_TIMEOUT_SECONDS={timeout}",
            "-e", "MATHAGENT_OUTPUT_BYTES=1048576",
            self.image_id, "python", "/opt/mathagent/runner.py"]
        try:
            # Docker reads this one generated value while creating the container.
            # No host mount, credentials, repository or writable rootfs is needed.
            # An env file avoids Windows' command-line length limit for source.
            with tempfile.TemporaryDirectory(prefix="mathagent-calculation-") as directory:
                source = Path(directory) / "calculation.env"
                source.write_text("MATHAGENT_SOURCE_BASE64=" + base64.b64encode(code.encode("utf-8")).decode("ascii"), encoding="ascii")
                command[2:2] = ["--env-file", str(source)]
                self._docker(command)
            record["status"] = "starting"
            _replace_json(path, record)
            self._docker(["docker", "start", container])
            record["status"] = "running"
            _replace_json(path, record)
        except (OSError, subprocess.SubprocessError):
            # An ambiguous start is queried, never automatically launched twice.
            return self.poll(project_id, job_id)
        return {"job_id": job_id, "status": "running", "code": code, "provenance": provenance}

    def _job_path(self, project_id, job_id):
        if not isinstance(project_id, str) or not project_id or not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{64}", job_id):
            return None
        path = self.jobs_path / _hash(project_id.encode()) / (job_id + ".json")
        try:
            _plain(self.jobs_path, directory=True)
            _plain(path.parent, directory=True)
            _plain(path)
            return path
        except OSError:
            return None

    @staticmethod
    def _docker(command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=15,
                                check=True, encoding="utf-8", errors="replace", env=_docker_environment())
        return result.stdout

    def poll(self, project_id, job_id):
        path = self._job_path(project_id, job_id)
        record = _read_json(path) if path else None
        if not record or record.get("project_id") != project_id or record.get("container") != "mathagent-" + job_id:
            return _failure("job_not_found", job_id=job_id)
        if record.get("redacted_at"):
            return _failure("material_erased", job_id=job_id)
        if record["status"] == "complete":
            return {"job_id": job_id, "status": "complete", **record["result"], "cached": True}
        try:
            state = json.loads(self._docker(["docker", "inspect", "--format", "{{json .State}}", record["container"]]))
            if state.get("Running"):
                return {"job_id": job_id, "status": "running", "elapsed_seconds": time.time() - record["created_at"]}
            if state.get("Status") == "created":
                # A host interruption during creation/start must not trigger an
                # unrequested rerun. Preserve the source and expose that state.
                return {"job_id": job_id, "status": "not_started", "reason": "start_interrupted",
                        "detail": _cap(state.get("Error"))}
            output = self._docker(["docker", "logs", record["container"]])
            result = json.loads(output)
            result.update(ok=result.get("reason") is None, code=record.get("code"),
                          provenance=record["provenance"])
        except (OSError, subprocess.SubprocessError, ValueError):
            return _failure("execution_unknown", job_id=job_id)
        record.update(status="complete", result=result, completed_at=time.time())
        if not _replace_json(path, record):
            return _failure("journal_unavailable", job_id=job_id)
        try:
            self._docker(["docker", "rm", record["container"]])
        except (OSError, subprocess.SubprocessError):
            pass  # The durable result is already saved; this is cleanup only.
        return {"job_id": job_id, "status": "complete", **result}

    def cancel(self, project_id, job_id):
        path = self._job_path(project_id, job_id)
        record = _read_json(path) if path else None
        if not record or record.get("project_id") != project_id or record.get("container") != "mathagent-" + job_id:
            return _failure("job_not_found", job_id=job_id)
        if record["status"] == "complete":
            return self.poll(project_id, job_id)
        try:
            self._docker(["docker", "stop", "--time", "1", record["container"]])
        except (OSError, subprocess.SubprocessError):
            return _failure("cancellation_unknown", job_id=job_id)
        result = _failure("cancelled", code=record.get("code"), provenance=record["provenance"])
        record.update(status="complete", result=result, completed_at=time.time())
        if not _replace_json(path, record):
            return _failure("journal_unavailable", job_id=job_id)
        try:
            self._docker(["docker", "rm", record["container"]])
        except (OSError, subprocess.SubprocessError):
            pass
        return {"job_id": job_id, "status": "complete", **result}

    def cancel_research(self, project_id, research_root_id):
        """Human Stop applies to this research's background jobs, not other work."""
        directory = self.jobs_path / _hash(project_id.encode())
        results = []
        for path in directory.glob("*.json") if directory.exists() else ():
            record = _read_json(path)
            if (record and record.get("project_id") == project_id
                    and record.get("research_root_id") == research_root_id
                    and record.get("status") != "complete" and record.get("container")):
                results.append(self.cancel(project_id, record["job_key"]))
        return results

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
                check=False, encoding="utf-8", errors="replace", env=_docker_environment(),
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
        return max(1, min(60, int(value)))
    except (TypeError, ValueError):
        return 5


def _cap(value):
    return (value or "")[:MAX_HOST_DIAGNOSTIC_BYTES]


def _read_json(path):
    try:
        _plain(path)
        if path.stat().st_size > 16 * 1024 * 1024:
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
