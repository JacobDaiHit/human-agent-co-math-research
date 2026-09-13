"""Load a small, explicit subset of one-line dotenv syntax without evaluation.

Only application-owned names are accepted. Process environment entries, including
empty strings, always win. UTF-8 (with optional BOM) and BOM-marked UTF-16 files
are supported. Quotes may escape their delimiter or a backslash; other backslash
sequences remain literal, which preserves Windows paths. There is no variable,
command, or escape-sequence interpolation.
"""

import os
import re
from pathlib import Path

ALLOWED_ENVIRONMENT_KEYS = frozenset(
    {
        "MATHAGENT_LOAD_ENV",
        "MATHAGENT_ENABLE_REAL_API",
        "MATHAGENT_DEEPSEEK_API_KEY",
        "MATHAGENT_DEEPSEEK_MODEL",
        "MATHAGENT_DEEPSEEK_BASE_URL",
        "MATHAGENT_GLM_API_KEY",
        "MATHAGENT_GLM_MODEL",
        "MATHAGENT_GLM_BASE_URL",
        "MATHAGENT_DATABASE",
        "MATHAGENT_TOKEN",
        "MATHAGENT_WORKER_TOKEN",
        "MATHAGENT_FRONTEND_ORIGIN",
        "MATHAGENT_FRONTEND_DIST",
        "MATHAGENT_SANDBOX_IMAGE",
    }
)
MAX_ENVIRONMENT_FILE_BYTES = 1_048_576
MAX_ENVIRONMENT_VALUE_LENGTH = 32_000
_KEY = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)")
_EXPORT = re.compile(r"export[ \t]+")
_INLINE_COMMENT = re.compile(r"[ \t]+#")


class EnvironmentFileError(ValueError):
    """A sanitized configuration error: never include source text or a path."""

    def __init__(self, line_number: int):
        self.line_number = line_number
        super().__init__(f"Invalid local environment file at line {line_number}")


def _parse_value(raw: str, line_number: int) -> str:
    raw = raw.strip(" \t")
    if not raw or raw.startswith("#"):
        return ""
    if raw[0] not in {"'", '"'}:
        value = _INLINE_COMMENT.split(raw, maxsplit=1)[0].rstrip(" \t")
    else:
        quote = raw[0]
        characters = []
        index = 1
        while index < len(raw):
            character = raw[index]
            if character == quote:
                trailing = raw[index + 1 :].strip(" \t")
                if trailing and not trailing.startswith("#"):
                    raise EnvironmentFileError(line_number)
                value = "".join(characters)
                break
            if character == "\\" and index + 1 < len(raw) and raw[index + 1] in {quote, "\\"}:
                characters.append(raw[index + 1])
                index += 2
            else:
                characters.append(character)
                index += 1
        else:
            raise EnvironmentFileError(line_number)
    if "\x00" in value or len(value) > MAX_ENVIRONMENT_VALUE_LENGTH:
        raise EnvironmentFileError(line_number)
    return value


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse recognized names atomically; unknown names and comments are ignored.

    Repeated recognized names use the final definition. Unsupported syntax on a
    recognized assignment raises an error containing only its line number.
    """
    parsed = {}
    for line_number, original in enumerate(text.removeprefix("\ufeff").splitlines(), start=1):
        line = original.strip(" \t")
        if not line or line.startswith("#"):
            continue
        line = _EXPORT.sub("", line, count=1)
        match = _KEY.match(line)
        if match is None or match.group() not in ALLOWED_ENVIRONMENT_KEYS:
            continue
        key = match.group()
        remainder = line[match.end() :].lstrip(" \t")
        if not remainder.startswith("="):
            raise EnvironmentFileError(line_number)
        parsed[key] = _parse_value(remainder[1:], line_number)
    return parsed


def _decode_file(data: bytes) -> str:
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        return data.decode(encoding)
    except UnicodeError as error:
        line_number = data[: error.start].count(b"\n") + 1
        raise EnvironmentFileError(line_number) from None


def load_local_environment(path: str | Path | None = None) -> list[str]:
    """Load a local file and return only newly loaded variable names.

    The process switch ``MATHAGENT_LOAD_ENV=0`` skips all file access. A missing
    file is optional. All parsing completes before the process is changed, so
    malformed recognized lines cannot leave partially applied configuration.
    """
    if os.environ.get("MATHAGENT_LOAD_ENV") == "0":
        return []
    target = Path(path) if path is not None else Path.cwd() / ".env"
    try:
        with target.open("rb") as stream:
            data = stream.read(MAX_ENVIRONMENT_FILE_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError:
        raise EnvironmentFileError(0) from None
    if len(data) > MAX_ENVIRONMENT_FILE_BYTES:
        raise EnvironmentFileError(0)
    parsed = parse_dotenv(_decode_file(data))
    loaded = []
    for key, value in parsed.items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
