"""Command-line policy defaults for reproducible AnswerBench runs."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("imo_answerbench", ROOT / "scripts/imo_answerbench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_answer_mode_does_not_add_a_paid_format_recovery(monkeypatch):
    module = _script()
    captured = {}

    class CapturedLimits:
        def __init__(self, **values):
            captured.update(values)

    monkeypatch.setattr(module, "Limits", CapturedLimits)
    monkeypatch.setattr(module, "load_local_environment", lambda: None)
    provider = module.ProviderConfig("deepseek", "synthetic-key", "synthetic-model",
                                     "https://api.deepseek.com", True)
    monkeypatch.setattr(module, "ProviderConfig", type("Config", (), {
        "from_env": staticmethod(lambda _: provider),
    }))
    monkeypatch.setattr(module, "run_batch", lambda *_args, **_kwargs: {
        "all_completed": False, "source_unchanged": True,
    })
    monkeypatch.setattr(module.asyncio, "run", lambda _coroutine: _coroutine)
    monkeypatch.setattr("sys.argv", ["imo_answerbench.py", "--execute", "--output", "out", "--evaluation-mode", "answer"])
    module.main()
    assert "length_recovery" not in captured
    assert "completion_policy" not in captured
    assert captured["solver_controller"] == "continuous_research"
    assert captured["discussion"] is True


def test_discussion_can_be_disabled_without_changing_request_budget(monkeypatch):
    module = _script()
    captured = {}

    class CapturedLimits:
        def __init__(self, **values):
            captured.update(values)

    monkeypatch.setattr(module, "Limits", CapturedLimits)
    monkeypatch.setattr(module, "load_local_environment", lambda: None)
    provider = module.ProviderConfig("deepseek", "synthetic-key", "synthetic-model",
                                     "https://api.deepseek.com", True)
    monkeypatch.setattr(module, "ProviderConfig", type("Config", (), {
        "from_env": staticmethod(lambda _: provider),
    }))
    monkeypatch.setattr(module, "run_batch", lambda *_args, **_kwargs: {
        "all_completed": False, "source_unchanged": True,
    })
    monkeypatch.setattr(module.asyncio, "run", lambda _coroutine: _coroutine)
    monkeypatch.setattr("sys.argv", ["imo_answerbench.py", "--execute", "--output", "out", "--no-discussion"])
    module.main()
    assert captured["discussion"] is False
    assert captured["request_budget"] == 12
    assert "max_steps" not in captured
