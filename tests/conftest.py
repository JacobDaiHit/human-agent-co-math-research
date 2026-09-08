"""Keep automated tests independent of local account configuration."""

import pytest


@pytest.fixture(autouse=True)
def isolate_local_configuration(monkeypatch):
    monkeypatch.setenv("MATHAGENT_LOAD_ENV", "0")
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "0")
    for provider in ("DEEPSEEK", "GLM"):
        for suffix in ("API_KEY", "MODEL", "BASE_URL"):
            monkeypatch.delenv(f"MATHAGENT_{provider}_{suffix}", raising=False)
