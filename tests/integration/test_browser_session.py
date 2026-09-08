"""The local cookie bootstrap is separate from the worker's authorization."""

from uuid import uuid4

from fastapi.testclient import TestClient
from mathagent.api.app import create_app


def test_loopback_cookie_and_api_prefix_use_csrf_checks(tmp_path):
    app = create_app(tmp_path / "browser.db", token="browser-user", worker_token="browser-worker")
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        assert client.post("/api/session").status_code == 403
        assert (
            client.post(
                "/api/session",
                headers={"X-MathAgent-Client": "workbench", "Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        session = client.post("/api/session", headers={"X-MathAgent-Client": "workbench"})
        assert session.status_code == 200
        assert "browser-user" not in session.text
        cookie = session.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert client.get("/api/projects").status_code == 200
        data = {"title": "Cookie project", "body": "A question"}
        headers = {"Idempotency-Key": str(uuid4())}
        assert client.post("/api/projects", json=data, headers=headers).status_code == 403
        headers["X-MathAgent-Client"] = "workbench"
        result = client.post("/api/projects", json=data, headers=headers)
        assert result.status_code == 201
        assert result.headers["x-content-type-options"] == "nosniff"
        assert (
            client.post(
                "/api/worker/claim-next", json={"providers": ["fake"]}, headers=headers
            ).status_code
            == 401
        )
        assert (
            client.get("/api/projects", headers={"Origin": "https://foreign.example"}).status_code
            == 403
        )


def test_nonloopback_client_or_hostname_cannot_bootstrap(tmp_path):
    app = create_app(tmp_path / "browser.db", token="browser-user", worker_token="browser-worker")
    headers = {"X-MathAgent-Client": "workbench"}
    with TestClient(
        app, base_url="http://rebound.example:8000", client=("127.0.0.1", 12345)
    ) as client:
        assert client.post("/session", headers=headers).status_code == 403
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("192.0.2.50", 12345)) as client:
        assert client.post("/session", headers=headers).status_code == 403
