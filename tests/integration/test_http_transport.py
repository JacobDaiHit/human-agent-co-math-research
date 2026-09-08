"""Exercise uvicorn's actual HTTP/SSE transport on a disposable loopback port."""

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

TOKEN = "http-transport-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def http_service(tmp_path: Path):
    workspace = Path(__file__).resolve().parents[2]
    interpreter = workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert interpreter.is_file(), "The project .venv must be installed before transport tests."
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    environment = {
        **os.environ,
        "MATHAGENT_DATABASE": str(tmp_path / "transport.sqlite3"),
        "MATHAGENT_TOKEN": TOKEN,
        "MATHAGENT_WORKER_TOKEN": "http-transport-distinct-worker-token",
    }
    log_path = tmp_path / "uvicorn.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                str(interpreter),
                "-m",
                "uvicorn",
                "mathagent.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=httpx.Timeout(3.0, connect=0.5),
                trust_env=False,
            ) as client:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        pytest.fail(f"uvicorn exited during startup: {log_path.read_text()}")
                    try:
                        ready = client.get("/health")
                        if ready.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail(
                        f"uvicorn was not healthy within 10 seconds: {log_path.read_text()}"
                    )
                yield client
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def post(client: httpx.Client, path: str, payload: dict) -> dict:
    response = client.post(path, json=payload, headers={**AUTH, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 201, response.text
    return response.json()


def next_frame(lines) -> dict:
    """Read one bounded HTTP response frame, retaining server heartbeat comments."""
    frame = {}
    for line in lines:
        if not line:
            if frame:
                return frame
            continue
        if line.startswith(":"):
            frame["comment"] = line[1:].strip()
        else:
            field, _, value = line.partition(":")
            frame[field] = value.lstrip()
    pytest.fail("SSE connection ended before a complete event or heartbeat.")


def next_event(lines) -> dict:
    # The server emits a heartbeat once per second. A stuck stream must fail,
    # even when those comments keep its network read timeout from expiring.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        frame = next_frame(lines)
        if "id" in frame:
            return frame
    pytest.fail("SSE kept the connection alive but did not deliver the expected event.")


def test_real_http_auth_sse_catchup_live_delivery_and_reconnect(http_service: httpx.Client):
    client = http_service
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["real_models_enabled"] is False
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert (
        client.get("/projects", headers={**AUTH, "Origin": "https://untrusted.example"}).status_code
        == 403
    )

    project = post(client, "/projects", {"title": "SSE transport", "body": "A research question"})
    project_id, branch_id = project["project_id"], project["branch_id"]
    stream_path = f"/projects/{project_id}/stream"
    events_path = f"/projects/{project_id}/events"
    objects = [
        post(client, "/objects", {"branch_id": branch_id, "kind": "claim", "body": body})
        for body in ("Lemma A", "Lemma B")
    ]
    assert client.get(stream_path).status_code == 401
    for invalid in ("invalid", "-1", "1.5"):
        response = client.get(stream_path, headers={**AUTH, "Last-Event-ID": invalid})
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "invalid_event_cursor"

    received = []
    # A resumed Last-Event-ID takes precedence over the initial query cursor.
    with client.stream(
        "GET", stream_path, params={"after_seq": 0}, headers={**AUTH, "Last-Event-ID": "1"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        lines = response.iter_lines()
        for expected_seq, obj in zip((2, 3), objects, strict=True):
            frame = next_event(lines)
            event = json.loads(frame["data"])
            assert int(frame["id"]) == event["seq"] == expected_seq
            assert frame["event"] == event["type"] == "object.created"
            assert event["payload"]["object_id"] == obj["object_id"]
            received.append(event)
        assert next_frame(lines) == {"comment": "heartbeat"}
        live = post(
            client,
            "/objects",
            {"branch_id": branch_id, "kind": "claim", "body": "Created while SSE is open"},
        )
        frame = next_event(lines)
        event = json.loads(frame["data"])
        assert int(frame["id"]) == event["seq"] == 4
        assert event["payload"]["object_id"] == live["object_id"]
        received.append(event)

    # Reconnection from the final delivered ID must not replay that event.
    with client.stream("GET", stream_path, headers={**AUTH, "Last-Event-ID": "4"}) as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next_frame(lines) == {"comment": "heartbeat"}
        later = post(
            client,
            "/objects",
            {"branch_id": branch_id, "kind": "context", "body": "Definitions after reconnect"},
        )
        frame = next_event(lines)
        event = json.loads(frame["data"])
        assert int(frame["id"]) == event["seq"] == 5
        assert event["payload"]["object_id"] == later["object_id"]
        received.append(event)

    finite = client.get(events_path, params={"after_seq": 1}, headers=AUTH)
    assert finite.status_code == 200
    assert finite.json()["events"] == received
    assert finite.json()["last_seq"] == 5
    assert [event["seq"] for event in received] == [2, 3, 4, 5]
    empty = client.get(events_path, params={"after_seq": 5}, headers=AUTH)
    assert empty.json()["events"] == []
    assert empty.json()["last_seq"] == 5
