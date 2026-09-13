import json
import subprocess

from mathagent.tools.code_sandbox import MAX_EXECUTIONS_PER_PROJECT, CodeSandbox

IMMUTABLE = "example.invalid/mathagent@sha256:" + "a" * 64


def _completed(args, **kwargs):
    if args[:3] == ["docker", "image", "inspect"]:
        return subprocess.CompletedProcess(args, 0, "sha256:" + "b" * 64 + "\n", "")
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
    command, kwargs = calls[-1]
    assert command[:3] == ["docker", "run", "--rm"]
    for part in ("--pull", "never", "--network", "none", "--read-only", "--memory", "256m", "--pids-limit", "32"):
        assert part in command
    assert "/work:rw,noexec,nosuid,nodev,size=16m" in command
    assert "-i" in command
    assert "--cap-drop" in command and "ALL" in command
    assert kwargs["env"].keys() <= {"PATH", "SystemRoot"}
    assert kwargs["input"] == "print(2 + 2)"

    cached = sandbox.execute("project-a", "attempt-1", "print(2 + 2)")
    assert cached["cached"] is True
    assert len([call for call in calls if call[0][1:3] == ["run", "--rm"]]) == 1


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
    assert len([args for args in calls if args[1:3] == ["run", "--rm"]]) == 2
    journal = next((tmp_path / "sandbox-jobs").rglob("*.json"))
    journal.write_text("{")
    assert sandbox.execute("project-a", "attempt-1", "print(1)", timeout_seconds=1)["reason"] == "execution_unknown"


def test_budget_and_redaction_preserve_accounting(tmp_path, monkeypatch):
    monkeypatch.setattr("mathagent.tools.code_sandbox.subprocess.run", _completed)
    sandbox = CodeSandbox(tmp_path / "mathagent.db", IMMUTABLE)
    for index in range(MAX_EXECUTIONS_PER_PROJECT):
        assert sandbox.execute("project-a", f"attempt-{index}", "print('secret')")["ok"]
    assert sandbox.execute("project-a", "attempt-over", "print('secret')")["reason"] == "project_execution_limit"
    assert sandbox.erase_project("project-a") == {"redacted": MAX_EXECUTIONS_PER_PROJECT, "pending": 0}
    record = json.loads(next((tmp_path / "sandbox-jobs").rglob("*.json")).read_text())
    assert "code" not in record
    assert "code_hash" in record
    assert "stdout" not in record["result"]
