"""Synthetic local configuration fixtures; no project credentials are read."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from mathagent import config


@pytest.fixture
def environment(monkeypatch):
    # Replace the mapping instead of inspecting real process secrets. The root
    # autouse fixture separately disables real APIs for every other test module.
    values = {}
    monkeypatch.setattr(config, "os", SimpleNamespace(environ=values))
    return values


def test_parse_quotes_comments_export_and_windows_paths():
    parsed = config.parse_dotenv(
        "\ufeff# Synthetic only\n\n"
        " export MATHAGENT_ENABLE_REAL_API = 1 # enabled\n"
        'MATHAGENT_DEEPSEEK_MODEL="synthetic # model" # comment\n'
        "MATHAGENT_GLM_MODEL=' synthetic value ' # comment\n"
        "MATHAGENT_DEEPSEEK_API_KEY=synthetic#literal # comment\n"
        "MATHAGENT_GLM_API_KEY= # empty\n"
        "MATHAGENT_DATABASE='C:\\Users\\Example\\data\\test.db'\n"
    )
    assert parsed == {
        "MATHAGENT_ENABLE_REAL_API": "1",
        "MATHAGENT_DEEPSEEK_MODEL": "synthetic # model",
        "MATHAGENT_GLM_MODEL": " synthetic value ",
        "MATHAGENT_DEEPSEEK_API_KEY": "synthetic#literal",
        "MATHAGENT_GLM_API_KEY": "",
        "MATHAGENT_DATABASE": "C:\\Users\\Example\\data\\test.db",
    }


def test_parser_does_not_interpolate_or_execute_values():
    parsed = config.parse_dotenv(
        'MATHAGENT_DEEPSEEK_API_KEY="${MISSING} $(never-run) `never-run`"\n'
        r'MATHAGENT_DEEPSEEK_MODEL="quoted \"value\" and \\ and \n"' + "\n"
        "MATHAGENT_GLM_MODEL='it\\'s literal'\n"
    )
    assert parsed["MATHAGENT_DEEPSEEK_API_KEY"] == "${MISSING} $(never-run) `never-run`"
    assert parsed["MATHAGENT_DEEPSEEK_MODEL"] == 'quoted "value" and \\ and \\n'
    assert parsed["MATHAGENT_GLM_MODEL"] == "it's literal"


def test_only_allowlisted_names_are_parsed():
    parsed = config.parse_dotenv(
        "PATH=not-an-executable-path\n"
        "PYTHONPATH=not-importable\n"
        "OPENAI_API_KEY=synthetic\n"
        "DEEPSEEK_API_KEY=synthetic\n"
        "MATHAGENT_UNKNOWN='unterminated ignored value\n"
        "MATHAGENT_DEEPSEEK_MODEL=first\n"
        "MATHAGENT_DEEPSEEK_MODEL=last\n"
    )
    assert parsed == {"MATHAGENT_DEEPSEEK_MODEL": "last"}


@pytest.mark.parametrize(
    "line",
    [
        "MATHAGENT_DEEPSEEK_API_KEY missing-equals-secret",
        "export MATHAGENT_DEEPSEEK_API_KEY: synthetic-secret",
        "MATHAGENT_DEEPSEEK_API_KEY='unterminated synthetic-secret",
        'MATHAGENT_DEEPSEEK_API_KEY="synthetic-secret" trailing-garbage',
        "MATHAGENT_DEEPSEEK_API_KEY=synthetic\x00secret",
        "MATHAGENT_DEEPSEEK_API_KEY=" + "s" * (config.MAX_ENVIRONMENT_VALUE_LENGTH + 1),
    ],
)
def test_malformed_recognized_assignment_reports_only_line_number(line):
    with pytest.raises(config.EnvironmentFileError) as caught:
        config.parse_dotenv("# synthetic\n" + line)
    assert caught.value.line_number == 2
    assert str(caught.value) == "Invalid local environment file at line 2"
    assert "SECRET" not in str(caught.value).upper()
    assert "MATHAGENT" not in str(caught.value)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_load_supported_encodings_returns_names_only(tmp_path, environment, encoding):
    path = tmp_path / "synthetic.env"
    path.write_bytes(
        "MATHAGENT_DEEPSEEK_API_KEY='synthetic-key'\nMATHAGENT_DEEPSEEK_MODEL=synthetic-model\n".encode(
            encoding
        )
    )
    loaded = config.load_local_environment(path)
    assert loaded == ["MATHAGENT_DEEPSEEK_API_KEY", "MATHAGENT_DEEPSEEK_MODEL"]
    assert environment["MATHAGENT_DEEPSEEK_API_KEY"] == "synthetic-key"
    assert "synthetic-key" not in repr(loaded)


def test_load_utf16_big_endian(tmp_path, environment):
    path = tmp_path / "synthetic.env"
    path.write_bytes(b"\xfe\xff" + "MATHAGENT_GLM_MODEL=synthetic-model\r\n".encode("utf-16-be"))
    assert config.load_local_environment(path) == ["MATHAGENT_GLM_MODEL"]


def test_existing_process_values_including_empty_and_disabled_win(tmp_path, environment):
    environment.update({"MATHAGENT_ENABLE_REAL_API": "0", "MATHAGENT_DEEPSEEK_API_KEY": ""})
    path = tmp_path / "synthetic.env"
    path.write_text(
        "MATHAGENT_ENABLE_REAL_API=1\nMATHAGENT_DEEPSEEK_API_KEY=synthetic-key\nMATHAGENT_DEEPSEEK_MODEL=synthetic-model\n",
        encoding="utf-8",
    )
    assert config.load_local_environment(path) == ["MATHAGENT_DEEPSEEK_MODEL"]
    assert environment["MATHAGENT_ENABLE_REAL_API"] == "0"
    assert environment["MATHAGENT_DEEPSEEK_API_KEY"] == ""
    assert config.load_local_environment(path) == []


def test_disabled_loader_does_not_access_files(environment, monkeypatch):
    environment["MATHAGENT_LOAD_ENV"] = "0"

    def denied(*_args, **_kwargs):
        pytest.fail("Disabled loader must not access a file")

    monkeypatch.setattr(Path, "open", denied)
    assert config.load_local_environment() == []
    assert config.load_local_environment("never-read.env") == []


def test_default_path_is_only_current_directory(tmp_path, environment, monkeypatch):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".env").write_text("MATHAGENT_GLM_MODEL=parent-value\n", encoding="utf-8")
    monkeypatch.chdir(child)
    assert config.load_local_environment() == []
    (child / ".env").write_text("MATHAGENT_GLM_MODEL=child-value\n", encoding="utf-8")
    assert config.load_local_environment() == ["MATHAGENT_GLM_MODEL"]
    assert environment["MATHAGENT_GLM_MODEL"] == "child-value"


def test_malformed_file_is_atomic_even_when_error_key_already_exists(tmp_path, environment):
    environment["MATHAGENT_GLM_API_KEY"] = "already-present"
    path = tmp_path / "synthetic.env"
    path.write_text(
        "MATHAGENT_DEEPSEEK_MODEL=should-not-be-applied\nMATHAGENT_GLM_API_KEY='synthetic-unclosed\n",
        encoding="utf-8",
    )
    with pytest.raises(config.EnvironmentFileError, match="line 2"):
        config.load_local_environment(path)
    assert environment == {"MATHAGENT_GLM_API_KEY": "already-present"}


def test_invalid_encoding_is_sanitized_and_does_not_partially_load(tmp_path, environment):
    path = tmp_path / "synthetic.env"
    path.write_bytes(b"MATHAGENT_DEEPSEEK_MODEL=synthetic\n\xffbroken")
    with pytest.raises(config.EnvironmentFileError) as caught:
        config.load_local_environment(path)
    assert str(caught.value) == "Invalid local environment file at line 2"
    assert environment == {}


def test_oversize_file_is_bounded_and_atomic(tmp_path, environment):
    path = tmp_path / "synthetic.env"
    path.write_bytes(b"#" * (config.MAX_ENVIRONMENT_FILE_BYTES + 1))
    with pytest.raises(config.EnvironmentFileError, match="line 0"):
        config.load_local_environment(path)
    assert environment == {}


def test_missing_explicit_file_is_optional(tmp_path, environment):
    assert config.load_local_environment(tmp_path / "missing.env") == []
    assert environment == {}
