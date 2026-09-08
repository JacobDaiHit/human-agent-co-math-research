"""Startup integration using only synthetic configuration and temporary tokens."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mathagent.api.app import create_app
from mathagent.config import ALLOWED_ENVIRONMENT_KEYS
from mathagent.providers.remote import RemoteProvider
from mathagent.runtime import worker


@pytest.fixture
def synthetic_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ALLOWED_ENVIRONMENT_KEYS:
        # Register an undo even if the name was initially absent. The loader
        # writes os.environ directly, so delenv(absent) alone would not undo it.
        monkeypatch.setenv(key, "synthetic-fixture-marker")
        monkeypatch.delenv(key)

    # In particular the root fixture's LOAD_ENV=0 is now removed explicitly;
    # the only .env that can be found is the one this test writes in tmp_path.
    async def no_remote_calls(*_args, **_kwargs):
        pytest.fail("Startup configuration checks must never call a remote provider")

    monkeypatch.setattr(RemoteProvider, "generate", no_remote_calls)
    return tmp_path


def test_api_factory_loads_local_configuration_before_database_auth_and_provider_status(
    synthetic_directory,
):
    (synthetic_directory / ".env").write_text(
        "MATHAGENT_ENABLE_REAL_API=1\n"
        "MATHAGENT_DEEPSEEK_API_KEY=synthetic-provider-key\n"
        "MATHAGENT_DEEPSEEK_MODEL=synthetic-model\n"
        "MATHAGENT_DATABASE='configured storage/research.sqlite3'\n"
        "MATHAGENT_TOKEN=synthetic-human-token\n"
        "MATHAGENT_WORKER_TOKEN=synthetic-worker-token\n",
        encoding="utf-8-sig",
    )
    app = create_app()
    assert (
        app.state.database.path
        == (synthetic_directory / "configured storage" / "research.sqlite3").resolve()
    )
    with TestClient(app) as client:
        response = client.get(
            "/providers/status", headers={"Authorization": "Bearer synthetic-human-token"}
        )
        assert response.status_code == 200
        status = response.json()
        assert status["real_api_gate"] is True
        deepseek = next(item for item in status["providers"] if item["provider"] == "deepseek")
        assert deepseek["key_configured"] is True
        assert deepseek["model_configured"] is True
        assert deepseek["configured"] is True
        assert deepseek["enabled"] is True
        glm = next(item for item in status["providers"] if item["provider"] == "glm")
        assert glm["configured"] is False
        assert glm["enabled"] is False
        assert "synthetic-provider-key" not in response.text
        assert "synthetic-model" not in response.text
        assert client.get("/health").json()["real_models_enabled"] is True
    assert not (synthetic_directory / "data" / "mathagent.db").exists()


@pytest.mark.parametrize("explicit_token_file", [False, True])
def test_worker_loads_database_before_token_path_and_explicit_path_wins(
    synthetic_directory, monkeypatch, explicit_token_file
):
    configured_directory = synthetic_directory / "configured storage"
    configured_directory.mkdir()
    configured_token = configured_directory / "worker.token"
    configured_token.write_text("synthetic-database-token", encoding="utf-8")
    explicit_token = synthetic_directory / "explicit worker.token"
    explicit_token.write_text("synthetic-explicit-token", encoding="utf-8")
    default_directory = synthetic_directory / "data"
    default_directory.mkdir()
    (default_directory / "worker.token").write_text("synthetic-wrong-default", encoding="utf-8")
    (synthetic_directory / ".env").write_text(
        "MATHAGENT_ENABLE_REAL_API=0\nMATHAGENT_DATABASE='configured storage/research.sqlite3'\n",
        encoding="utf-8",
    )
    expected_file = explicit_token if explicit_token_file else configured_token
    expected_token = (
        "synthetic-explicit-token" if explicit_token_file else "synthetic-database-token"
    )
    reads = []
    read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        assert path.resolve() == expected_file.resolve(), "Worker selected an unexpected token path"
        reads.append(path.resolve())
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    observed = []

    async def record_worker_run(self, *, once=False, stop=None):
        observed.append(
            {
                "authorization": self.client.headers["Authorization"],
                "providers": self.providers,
                "once": once,
            }
        )

    monkeypatch.setattr(worker.HTTPWorker, "run", record_worker_run)
    arguments = ["mathagent-worker", "--once", "--providers", "fake"]
    if explicit_token_file:
        arguments.extend(["--token-file", str(explicit_token)])
    monkeypatch.setattr(sys, "argv", arguments)
    worker.main()
    assert reads == [expected_file.resolve()]
    assert observed == [
        {"authorization": "Bearer " + expected_token, "providers": ["fake"], "once": True}
    ]
