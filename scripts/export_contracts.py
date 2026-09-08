"""Generate OpenAPI without connecting to the project database or loading credentials."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from mathagent.api.app import create_app

root = Path(__file__).resolve().parents[1]
os.environ["MATHAGENT_LOAD_ENV"] = "0"
with TemporaryDirectory() as directory:
    app = create_app(
        Path(directory) / "unused.db",
        token="contract-only-human",
        worker_token="contract-only-worker",
    )
    schema = app.openapi()
    app.state.database.close()
target = root / "contracts" / "openapi.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(target)
