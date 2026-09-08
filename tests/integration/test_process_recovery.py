"""Actually kill owned API/worker processes and recover their durable HTTP state.

All files, credentials, ports, and providers are synthetic. Each killed PID is
held by this test's Popen; no running user service or external API is contacted.
"""

import json
import os
import socket
import subprocess
import sys
import sysconfig
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

TOKEN = "process-recovery-synthetic-human"
WORKER_TOKEN = "process-recovery-synthetic-worker"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OUTPUT_BODY = "Synthetic completed proof: $1+1=2$. No external model was called."


class ProcessWorkspace:
    def __init__(self, directory):
        self.directory = directory
        self.workspace = Path(__file__).resolve().parents[2]
        self.helper = self.workspace / "tests" / "helpers" / "fault_worker.py"
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        assert self.port not in {8000, 8001}, "Never attach a test to either user service port."
        # Windows venv python.exe can be a redirector with an unowned child.
        # Launch the base interpreter itself and explicitly add this venv's
        # libraries, so Popen.kill targets the actual API/worker process.
        self.interpreter = Path(getattr(sys, "_base_executable", sys.executable))
        self.environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(self.workspace / "services" / "api" / "src"), sysconfig.get_paths()["purelib"]]
            ),
            "PYTHONNOUSERSITE": "1",
            "MATHAGENT_LOAD_ENV": "0",
            "MATHAGENT_DATABASE": str(directory / "process-recovery.db"),
            "MATHAGENT_TOKEN": TOKEN,
            "MATHAGENT_WORKER_TOKEN": WORKER_TOKEN,
            # Permit the named provider in API accounting; the worker always
            # injects a deterministic local provider and blocks non-loopback sockets.
            "MATHAGENT_ENABLE_REAL_API": "1",
            "MATHAGENT_DEEPSEEK_API_KEY": "synthetic-never-sent-to-a-vendor",
            "MATHAGENT_DEEPSEEK_MODEL": "synthetic-process-test",
            "MATHAGENT_DEEPSEEK_BASE_URL": "https://example.invalid",
            "MATHAGENT_GLM_API_KEY": "",
            "MATHAGENT_GLM_MODEL": "",
            "MATHAGENT_GLM_BASE_URL": "https://example.invalid",
        }
        self.processes = []
        self.resources = ExitStack()
        self.client = self.resources.enter_context(
            httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=3, trust_env=False)
        )
        self.journal = directory / "synthetic-provider-acceptances.jsonl"
        self.sequence = 0

    def spawn(self, mode, stage="normal"):
        self.sequence += 1
        marker = self.directory / f"boundary-{self.sequence}.json"
        log_path = self.directory / f"{mode}-{self.sequence}.log"
        log = self.resources.enter_context(log_path.open("wb"))
        process = subprocess.Popen(
            [
                str(self.interpreter),
                "-X",
                "utf8",
                str(self.helper),
                mode,
                "--port",
                str(self.port),
                "--stage",
                stage,
                "--marker",
                str(marker),
                "--journal",
                str(self.journal),
            ],
            cwd=self.directory,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.processes.append(process)
        return process, marker, log_path

    def start_api(self):
        self.api, _, log_path = self.spawn("api")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.api.poll() is not None:
                pytest.fail("Synthetic API exited during startup: " + log_path.read_text())
            try:
                if self.client.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        pytest.fail("Synthetic API startup timed out: " + log_path.read_text())

    def kill_owned(self, process):
        assert process in self.processes
        assert process.pid not in {os.getpid(), os.getppid()}
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    def restart_after_crash(self, worker):
        self.kill_owned(worker)
        self.kill_owned(self.api)
        self.start_api()
        # Let the actual two-second lease expire. No direct database writes or
        # fabricated timestamps are used to simulate expiry.
        time.sleep(2.25)

    def wait_boundary(self, process, marker, log_path):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if marker.exists():
                boundary = json.loads(marker.read_text(encoding="utf-8"))
                assert boundary["pid"] == process.pid
                return boundary
            if process.poll() is not None:
                pytest.fail(
                    "Synthetic worker exited before fault boundary: " + log_path.read_text()
                )
            time.sleep(0.05)
        pytest.fail("Synthetic worker boundary timed out: " + log_path.read_text())

    def run_worker(self, stage="normal"):
        process, marker, log_path = self.spawn("worker", stage)
        process.wait(timeout=12)
        assert process.returncode == 0, log_path.read_text()
        return marker

    def write(self, path, payload=None, *, method="POST", worker=False, key=None):
        auth = {"Authorization": f"Bearer {WORKER_TOKEN}"} if worker else AUTH
        response = self.client.request(
            method,
            path,
            json=payload or {},
            headers={**auth, "Idempotency-Key": key or str(uuid4())},
        )
        assert response.is_success, response.text
        return response.json()

    def read(self, path, **params):
        response = self.client.get(path, params=params, headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    def prepare(self):
        self.project = self.write("/projects", {"title": "Process crash test", "body": "$1+1=?$"})
        self.write(
            f"/projects/{self.project['project_id']}/runtime-settings",
            {"request_budget": 1, "allow_real_api": True, "allowed_providers": ["deepseek"]},
            method="PUT",
        )
        self.run = self.write(
            "/runs",
            {
                "branch_id": self.project["branch_id"],
                "goal_object_id": self.project["object_id"],
                "provider": "deepseek",
                "instruction": "A synthetic local provider fixture only.",
                "request_budget": 1,
            },
        )

    def budget(self):
        return self.read(f"/projects/{self.project['project_id']}/budget")

    def snapshot(self, branch_id=None):
        return self.read(
            f"/projects/{self.project['project_id']}/snapshot",
            branch_id=branch_id or self.project["branch_id"],
        )

    def accepted_count(self):
        return len(self.journal.read_text().splitlines()) if self.journal.exists() else 0

    def close(self):
        try:
            for process in reversed(self.processes):
                self.kill_owned(process)
        finally:
            self.resources.close()


@pytest.fixture
def workspace(tmp_path):
    workspace = ProcessWorkspace(tmp_path)
    try:
        workspace.start_api()
        workspace.prepare()
        yield workspace
    finally:
        workspace.close()


def test_kill_before_dispatch_releases_reservation_and_new_attempt_runs_once(workspace):
    process, marker, log = workspace.spawn("worker", "before_dispatch")
    assert workspace.wait_boundary(process, marker, log)["phase"] == "reserved"
    assert workspace.accepted_count() == 0
    workspace.restart_after_crash(process)
    workspace.run_worker()
    budget = workspace.budget()
    assert budget["released"] == 1 and budget["spent"] == 1 and budget["unknown"] == 0
    run = workspace.snapshot()["runs"][0]
    assert run["state"] == "completed"
    assert [attempt["number"] for attempt in run["attempts"]] == [1, 2]
    assert workspace.accepted_count() == 1
    assert (
        sum(item["revision"]["body"] == OUTPUT_BODY for item in workspace.snapshot()["objects"])
        == 1
    )


def test_kill_after_synthetic_acceptance_blocks_unknown_request_without_redispatch(workspace):
    process, marker, log = workspace.spawn("worker", "in_flight")
    assert workspace.wait_boundary(process, marker, log)["phase"] == "accepted"
    assert workspace.accepted_count() == 1
    workspace.restart_after_crash(process)
    workspace.run_worker()
    budget = workspace.budget()
    assert budget["unknown"] == 1 and budget["spent"] == 0 and budget["remaining"] == 0
    assert workspace.snapshot()["runs"][0]["state"] == "reconciliation_required"
    assert workspace.write(f"/runs/{workspace.run['run_id']}/resume")["effect"] == "blocked"
    workspace.run_worker()
    assert workspace.accepted_count() == 1
    assert len(workspace.budget()["requests"]) == 1


@pytest.mark.parametrize(
    "stage,phase", [("after_observation", "observed"), ("after_settlement", "settled")]
)
def test_kill_after_saved_observation_recovers_original_output_without_another_call(
    workspace, stage, phase
):
    process, marker, log = workspace.spawn("worker", stage)
    assert workspace.wait_boundary(process, marker, log)["phase"] == phase
    calls_path = f"/runs/{workspace.run['run_id']}/calls"
    before = workspace.read(calls_path)["calls"]
    assert len(before) == 1 and before[0]["complete"] is True
    assert before[0]["result"]["body"] == OUTPUT_BODY
    workspace.restart_after_crash(process)
    workspace.run_worker()
    after = workspace.read(calls_path)["calls"]
    assert after == before, "The provider's original output and usage must survive process death."
    assert workspace.accepted_count() == 1
    assert len(workspace.budget()["requests"]) == 1
    branches = workspace.read(f"/projects/{workspace.project['project_id']}/branches")["branches"]
    outputs = {
        item["revision"]["id"]
        for branch in branches
        for item in workspace.snapshot(branch["id"])["objects"]
        if item["kind"] == "artifact" and item["revision"]["body"] == OUTPUT_BODY
    }
    assert len(outputs) == 1, (
        "A complete durable observation exists, but no original output was recovered "
        "as a research artifact or isolated candidate after API/worker restart."
    )
    budget = workspace.budget()
    assert budget["spent"] == 1 and budget["unknown"] == 0


def test_replayed_http_completion_keeps_one_output_and_one_synthetic_charge(workspace):
    marker = workspace.run_worker("duplicate_complete")
    report = json.loads(marker.read_text(encoding="utf-8"))
    assert report["phase"] == "completed_repeatedly" and report["identical"]
    before = workspace.snapshot()
    workspace.kill_owned(workspace.api)
    workspace.start_api()
    workspace.run_worker()
    after = workspace.snapshot()
    assert after["objects"] == before["objects"]
    assert (
        sum(item["revision"]["id"] == report["output_revision_id"] for item in after["objects"])
        == 1
    )
    assert workspace.accepted_count() == 1
    assert workspace.budget()["spent"] == 1


def test_invalid_saved_result_stays_unknown_without_recovery_or_redispatch(workspace):
    process, marker, log = workspace.spawn("worker", "invalid_observation")
    assert workspace.wait_boundary(process, marker, log)["phase"] == "observed"
    workspace.restart_after_crash(process)
    workspace.run_worker()
    assert workspace.accepted_count() == 1
    assert workspace.budget()["unknown"] == 1
    assert workspace.snapshot()["runs"][0]["state"] == "reconciliation_required"
    branches = workspace.read(f"/projects/{workspace.project['project_id']}/branches")["branches"]
    assert all(
        item["revision"]["body"] != OUTPUT_BODY
        for branch in branches
        for item in workspace.snapshot(branch["id"])["objects"]
    )


def test_recovery_of_autonomous_response_quarantines_draft_and_skips_old_actions(workspace):
    workspace.write(
        f"/runs/{workspace.run['run_id']}/options",
        {
            "request_budget": 1,
            "autonomous": True,
            "max_steps": 2,
            "max_review_rounds": 0,
            "max_children": 0,
            "max_depth": 0,
            "max_output_tokens": 256,
            "request_timeout_seconds": 5,
        },
        method="PUT",
    )
    process, marker, log = workspace.spawn("worker", "autonomous_observation")
    assert workspace.wait_boundary(process, marker, log)["phase"] == "observed"
    workspace.restart_after_crash(process)
    workspace.run_worker()
    assert workspace.accepted_count() == 1
    assert workspace.budget()["spent"] == 1 and workspace.budget()["unknown"] == 0
    branches = workspace.read(f"/projects/{workspace.project['project_id']}/branches")["branches"]
    bodies = {
        item["revision"]["body"]
        for branch in branches
        for item in workspace.snapshot(branch["id"])["objects"]
    }
    assert OUTPUT_BODY in bodies
    assert "THIS OLD PROPOSAL MUST NOT EXECUTE." not in bodies
    steps = workspace.read(f"/runs/{workspace.run['run_id']}/steps")["steps"]
    assert len(steps) == 1 and steps[0]["receipt"]["quarantined"] is True
    assert steps[0]["actions"] == []
    run = workspace.snapshot()["runs"][0]
    assert run["state"] == "interrupted"
    assert (
        run["attempts"][0]["checkpoint"]["recovered_request_id"]
        == workspace.budget()["requests"][0]["request_id"]
    )
    events = workspace.read(f"/projects/{workspace.project['project_id']}/events")["events"]
    recovered = [event for event in events if event["type"] == "attempt.saved_response_recovered"]
    assert len(recovered) == 1 and recovered[0]["payload"]["skipped_actions"] == 1
