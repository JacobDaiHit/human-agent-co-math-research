import hashlib
import json
import subprocess

from mathagent.tools.code_sandbox import CodeSandbox

IMMUTABLE = "example.invalid/mathagent@sha256:" + "a" * 64


def _completed(args, **kwargs):
    if args[:3] == ["docker", "image", "inspect"]:
        return subprocess.CompletedProcess(args, 0, "sha256:" + "b" * 64 + "\n", "")
    if args[1] == "inspect":
        return subprocess.CompletedProcess(args, 0, json.dumps({"Running": False, "Status": "exited"}), "")
    return subprocess.CompletedProcess(
        args, 0, json.dumps({"reason": None, "exit_code": 0, "stdout": "4\n", "stderr": ""}), ""
    )


def test_status_fails_closed_without_immutable_local_image(tmp_path, monkeypatch):
    monkeypatch.delenv("MATHAGENT_SANDBOX_IMAGE", raising=False)
    sandbox = CodeSandbox(tmp_path / "mathagent.db")
    assert sandbox.status() == {"ready": False, "reason": "image_not_configured", "image_id": None}

    sandbox = CodeSandbox(tmp_path / "mathagent.db", "python:3.13")
    assert sandbox.status()["reason"] == "image_not_immutable"

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    assert sandbox.status()["reason"] == "docker_unavailable"


def test_execute_uses_hardened_docker_command_and_cache(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _completed(args, **kwargs)

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", fake_run)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    first = sandbox.execute("project-a", "attempt-1", "print(2 + 2)")
    assert first["ok"] is True
    assert first["stdout"] == "4\n"
    command, kwargs = next(call for call in calls if call[0][1] == "create")
    assert command[:2] == ["docker", "create"]
    for part in ("--pull", "never", "--network", "none", "--read-only", "--memory", "1g", "--cpus", "1", "--pids-limit", "64"):
        assert part in command
    assert "/work:rw,noexec,nosuid,nodev,size=256m" in command
    assert "--env-file" in command
    assert "MATHAGENT_TIMEOUT_SECONDS=3600" in command
    assert "--cap-drop" in command and "ALL" in command
    assert kwargs["env"].keys() <= {"PATH", "SystemRoot"}
    assert "input" not in kwargs

    cached = sandbox.execute("project-a", "attempt-1", "print(2 + 2)")
    assert cached["cached"] is True
    assert len([call for call in calls if call[0][1] == "create"]) == 1


def test_existing_reservation_never_reruns_after_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", _completed)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    result = sandbox.execute("project-a", "attempt-1", "print('x')")
    assert result["ok"]
    journal = next((tmp_path / "sandbox-jobs").rglob("*.json"))
    record = json.loads(journal.read_text())
    record["status"] = "reserved"
    record.pop("result")
    journal.write_text(json.dumps(record))
    def interrupted(args, **kwargs):
        assert args[1] != "create"
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", interrupted)
    duplicate = sandbox.execute("project-a", "attempt-1", "print('x')")
    assert duplicate["reason"] == "execution_unknown"


def test_dedup_binds_timeout_and_damaged_journal_fails_closed(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _completed(args, **kwargs)

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", fake_run)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    assert sandbox.execute("project-a", "attempt-1", "print(1)", timeout_seconds=1)["ok"]
    assert sandbox.execute("project-a", "attempt-1", "print(1)", timeout_seconds=2)["ok"]
    assert len([args for args in calls if args[1] == "create"]) == 2
    journal = next(path for path in (tmp_path / "sandbox-jobs").rglob("*.json")
                   if json.loads(path.read_text())["provenance"]["timeout_seconds"] == 1)
    journal.write_text("{")
    assert sandbox.execute("project-a", "attempt-1", "print(1)", timeout_seconds=1)["reason"] == "execution_unknown"


def test_project_has_no_lifetime_execution_counter_and_redaction_preserves_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", _completed)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    for index in range(400):
        result = sandbox.execute("project-a", f"attempt-{index}", "print('secret')")
        assert result["ok"], result
    assert sandbox.execute("project-a", "attempt-over", "print('secret')")["ok"]
    assert sandbox.erase_project("project-a") == {"redacted": 401, "pending": 0}
    record = json.loads(next((tmp_path / "sandbox-jobs").rglob("*.json")).read_text())
    assert "code" not in record
    assert "code_hash" in record
    assert "stdout" not in record["result"]


def test_background_job_survives_client_restart_and_is_never_started_twice(tmp_path, monkeypatch):
    import base64
    from pathlib import Path

    import mathagent.tools.code_sandbox as storage

    commands = []
    replacements = []
    replace_json = storage._replace_json

    def record_replacement(path, value):
        replacements.append(value["status"])
        return replace_json(path, value)

    monkeypatch.setattr(storage, "_replace_json", record_replacement)
    running = True

    def docker(args, **kwargs):
        commands.append(args)
        assert kwargs["env"].keys() <= {"PATH", "SystemRoot"}
        if args[1:3] == ["image", "inspect"]:
            return _completed(args)
        if args[1] == "create":
            value = Path(args[args.index("--env-file") + 1]).read_text(encoding="ascii")
            assert value.startswith("MATHAGENT_SOURCE_BASE64=")
            assert base64.b64decode(value.split("=", 1)[1]).decode() == "print('LONG_RESULT')"
        if args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, json.dumps({"Running": running, "Status": "running" if running else "exited"}), "")
        if args[1] == "logs":
            return subprocess.CompletedProcess(args, 0, json.dumps({"reason": None, "stdout": "LONG_RESULT", "stderr": ""}), "")
        return subprocess.CompletedProcess(args, 0, "container-id", "")

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", docker)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    job = sandbox.start("project-a", "request:tool", "print('LONG_RESULT')", 3600)
    assert job["status"] == "running" and job["code"] == "print('LONG_RESULT')"
    assert replacements == []
    create = next(command for command in commands if command[1] == "create")
    assert "3600" in create[create.index("-e") + 1]
    assert "--network" in create and "none" in create and "--read-only" in create
    assert "--mount" not in create and "--privileged" not in create
    assert "--memory" in create and "1g" in create
    restarted = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    assert restarted.start("project-a", "request:tool", "print('LONG_RESULT')", 3600)["status"] == "running"
    assert len([command for command in commands if command[1] == "start"]) == 1
    running = False
    result = restarted.poll("project-a", job["job_id"])
    assert result["ok"] and result["stdout"] == "LONG_RESULT" and result["code"] == "print('LONG_RESULT')"
    assert replacements == ["complete"]
    before = len(commands)
    assert restarted.poll("project-a", job["job_id"])["cached"]
    assert len(commands) == before
    assert restarted.poll("other-project", job["job_id"])["reason"] == "job_not_found"


def test_background_cancellation_is_scoped_and_durable(tmp_path, monkeypatch):
    commands = []

    def docker(args, **kwargs):
        commands.append(args)
        return _completed(args) if args[1:3] == ["image", "inspect"] else subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", docker)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    job = sandbox.start("project-a", "calculation", "while True: pass", 3600)
    result = sandbox.cancel("project-a", job["job_id"])
    assert result["status"] == "complete" and result["reason"] == "cancelled"
    assert sandbox.poll("project-a", job["job_id"])["reason"] == "cancelled"
    assert [command[-1] for command in commands if command[1] in {"stop", "rm"}] == ["mathagent-" + job["job_id"]] * 2


def test_permanent_project_erasure_stops_and_redacts_background_work(tmp_path, monkeypatch):
    commands = []

    def docker(args, **kwargs):
        commands.append(args)
        return _completed(args) if args[1:3] == ["image", "inspect"] else subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", docker)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    job = sandbox.start("project-a", "erase-work", "print('private research')", 3600)
    assert sandbox.erase_project("project-a") == {"redacted": 1, "pending": 0}
    assert sandbox.poll("project-a", job["job_id"])["reason"] == "material_erased"
    record = json.loads(next((tmp_path / "sandbox-jobs").rglob("*.json")).read_text())
    assert record["status"] == "complete" and "code" not in record and "code" not in record["result"]
    assert [command[-1] for command in commands if command[1] in {"stop", "rm"}] == ["mathagent-" + job["job_id"]] * 2


def test_readiness_distinguishes_missing_image_from_unavailable_engine(tmp_path, monkeypatch):
    def missing_image(args, **kwargs):
        return subprocess.CompletedProcess(args, 0 if args[1] == "version" else 1,
            "29.4.0" if args[1] == "version" else "", "")

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", missing_image)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    assert sandbox.status()["reason"] == "image_not_local"
    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "engine unavailable"))
    assert sandbox.status()["reason"] == "docker_unavailable"


def test_quick_wait_does_not_shorten_execution_or_start_a_second_job(tmp_path, monkeypatch):
    commands = []

    def docker(args, **kwargs):
        commands.append(args)
        if args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, json.dumps({"Running": True}), "")
        return _completed(args, **kwargs)

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", docker)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    result = sandbox.execute("project-a", "calculation", "while True: pass", wait_seconds=0)
    assert result["status"] == "running"
    record = json.loads(next((tmp_path / "sandbox-jobs").rglob("*.json")).read_text())
    assert record["provenance"]["timeout_seconds"] == 3600
    again = sandbox.execute("project-a", "calculation", "while True: pass", wait_seconds=0)
    assert again["job_id"] == result["job_id"]
    assert len([command for command in commands if command[1] == "create"]) == 1


def test_old_synchronous_receipt_is_read_without_launching_new_container(tmp_path, monkeypatch):
    code = "print('legacy')"
    def digest(value):
        return hashlib.sha256(value).hexdigest()
    provenance = {"tool_version": "docker-python-sandbox-v2", "image_id": IMMUTABLE,
        "timeout_seconds": 5, "code_hash": digest(code.encode()),
        "limits": {"code_bytes": 65536, "output_bytes": 32768, "timeout_seconds": 5}}
    job_id = digest(json.dumps(["project-a", "old-call", provenance], sort_keys=True).encode())
    directory = tmp_path / "sandbox-jobs" / digest(b"project-a")
    directory.mkdir(parents=True)
    (directory / (job_id + ".json")).write_text(json.dumps({"status": "complete",
        "code_hash": provenance["code_hash"], "result": {"ok": True, "stdout": "OLD_RESULT"}}))

    def forbidden(*args, **kwargs):
        raise AssertionError("A frozen old calculation must not be relaunched")

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", forbidden)
    result = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE).execute("project-a", "old-call", code, 5)
    assert result["cached"] and result["stdout"] == "OLD_RESULT"


def test_old_background_job_remains_the_same_execution_after_tool_upgrade(tmp_path, monkeypatch):
    code = "print('old background')"
    provenance = {"tool_version": "docker-python-sandbox-v2", "image_id": IMMUTABLE,
        "timeout_seconds": 3600, "code_hash": hashlib.sha256(code.encode()).hexdigest(), "background": True}
    job_id = hashlib.sha256(json.dumps(["project-a", "old-call", provenance], sort_keys=True).encode()).hexdigest()
    directory = tmp_path / "sandbox-jobs" / hashlib.sha256(b"project-a").hexdigest()
    directory.mkdir(parents=True)
    (directory / (job_id + ".json")).write_text(json.dumps({"project_id": "project-a",
        "container": "mathagent-" + job_id, "status": "running", "created_at": 0,
        "code": code, "provenance": provenance}))

    def running(args, **kwargs):
        assert args[1] == "inspect"
        return subprocess.CompletedProcess(args, 0, json.dumps({"Running": True}), "")

    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", running)
    result = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE).execute("project-a", "old-call", code, wait_seconds=0)
    assert result["job_id"] == job_id and result["status"] == "running"
