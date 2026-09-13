"""Offline code execution is opt-in and never promotes mathematical evidence."""

import pytest
from mathagent.application.code_execution import CodeExecutionService, permitted_operations
from mathagent.application.errors import DomainError
from mathagent.persistence.models import Adoption, Project, Review, Run
from mathagent.providers.actions import operation_schemas
from test_autonomous_agent import app as upstream_app
from test_autonomous_agent import exercise, project_and_run

app_fixture = upstream_app


class Sandbox:
    def __init__(self, *_args, **_kwargs):
        pass

    def status(self):
        return {"ready": True, "image_id": "sha256:" + "a" * 64, "network": "none"}

    def execute(self, *_args):
        return {"stdout": "2\n", "stderr": "", "exit_code": 0}


def test_code_execution_requires_ready_explicit_opt_in_and_stays_partial(app_fixture, monkeypatch):
    async def scenario(api, _client):
        project, run = await project_and_run(api, autonomous=False, provider="fake")
        with app_fixture.state.database.sessions() as session:
            service = CodeExecutionService(app_fixture.state.service)
            row = session.get(Run, run["run_id"])
            assert "run_code" in operation_schemas()
            assert "run_code" not in permitted_operations(session.get(Project, project["project_id"]))
            with pytest.raises(DomainError, match="未启用"):
                service.execute(session, row, {"code": "print(2)", "timeout_seconds": 1})
        class Unready:
            def __init__(self, *_args, **_kwargs): pass
            def status(self): return {"ready": False, "reason": "missing", "image_id": None}
        monkeypatch.setattr("mathagent.application.code_execution.CodeSandbox", Unready)
        with app_fixture.state.database.sessions() as session:
            with pytest.raises(DomainError, match="尚未就绪"):
                CodeExecutionService(app_fixture.state.service).configure(session, {"project_id": project["project_id"], "enabled": True})
        monkeypatch.setattr("mathagent.application.code_execution.CodeSandbox", Sandbox)
        with app_fixture.state.database.sessions.begin() as session:
            service = CodeExecutionService(app_fixture.state.service)
            service.configure(session, {"project_id": project["project_id"], "enabled": True})
            receipt = service.execute(session, session.get(Run, run["run_id"]), {"code": "print(2)", "timeout_seconds": 1})
            assert receipt["coverage"] == "partial"
            assert session.scalars(Review.__table__.select()).all() == []
            assert session.scalars(Adoption.__table__.select()).all() == []
        with app_fixture.state.database.sessions.begin() as session:
            service = CodeExecutionService(app_fixture.state.service)
            service.configure(session, {"project_id": project["project_id"], "enabled": False})
            with pytest.raises(DomainError, match="未启用"):
                service.execute(session, session.get(Run, run["run_id"]), {"code": "print(3)", "timeout_seconds": 1})
    exercise(app_fixture, scenario)
