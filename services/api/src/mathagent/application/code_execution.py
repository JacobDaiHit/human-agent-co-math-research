"""Explicit project opt-in and draft-only receipts for isolated computation."""

import json

from mathagent.application.errors import DomainError
from mathagent.persistence.models import Project
from mathagent.providers.actions import operation_schemas
from mathagent.tools.code_sandbox import CodeSandbox


def permitted_operations(project):
    default = [name for name in operation_schemas() if name != "run_code"]
    allowed = project.policies.get("agent_operations", default)
    if not project.policies.get("code_sandbox", {}).get("enabled", False):
        allowed = [name for name in allowed if name != "run_code"]
    return allowed


class CodeExecutionService:
    def __init__(self, state):
        self.state = state

    def status(self, session, project_id):
        project = session.get(Project, project_id)
        if not project:
            raise DomainError(404, "project_not_found", "研究项目不存在。")
        settings = project.policies.get("code_sandbox", {})
        engine = CodeSandbox(self.state.db.path, image_id=settings.get("image_id"))
        return {**engine.status(), "enabled": settings.get("enabled", False)}

    def configure(self, session, payload):
        project = session.get(Project, payload["project_id"])
        if not project:
            raise DomainError(404, "project_not_found", "研究项目不存在。")
        status = self.status(session, project.id)
        if payload["enabled"] and not status["ready"]:
            raise DomainError(409, "sandbox_unavailable", "隔离引擎尚未就绪；不会在宿主机运行代码。", reason=status.get("reason"))
        allowed = [name for name in permitted_operations(project) if name != "run_code"]
        if payload["enabled"]:
            allowed.append("run_code")
        project.policies = {**project.policies, "agent_operations": allowed,
            "code_sandbox": {"enabled": payload["enabled"], "image_id": status.get("image_id")}}
        self.state.emit(session, project.id, None, "project.code_sandbox_changed", {
            "enabled": payload["enabled"], "image_id": status.get("image_id"), "network": "none"})
        return 200, {**status, "enabled": payload["enabled"]}

    def execute(self, session, run, values):
        branch = self.state.require_branch(session, run.branch_id)
        project = session.get(Project, branch.project_id)
        settings = project.policies.get("code_sandbox", {})
        if not settings.get("enabled") or "run_code" not in permitted_operations(project):
            raise DomainError(403, "sandbox_not_enabled", "项目未启用离线代码沙箱。")
        target = values.get("target_revision_id")
        if target:
            self.state.check_revision(session, branch, target, current=True)
        engine = CodeSandbox(self.state.db.path, image_id=settings.get("image_id"))
        status = engine.status()
        if not status["ready"]:
            raise DomainError(409, "sandbox_unavailable", "隔离引擎不可用；本次没有运行代码。", reason=status.get("reason"))
        result = engine.execute(project.id, run.current_attempt_id, values["code"], values["timeout_seconds"])
        payload = {"artifact_type": "code_execution", "code": values["code"], "result": result,
                   "input_revisions": [target] if target else [], "coverage": "partial",
                   "scope": "离线程序执行观察；不构成完整数学证明。", "run_id": run.id}
        body = "离线代码执行记录。计算观察不等于完整证明。\n\n```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"
        obj, revision = self.state.new_object(session, branch, "artifact", body, payload, "agent:" + run.provider)
        self.state._add_reference(session, branch.id, revision.id)
        self.state.emit(session, project.id, branch.id, "code.executed", {
            "object_id": obj.id, "revision_id": revision.id, "result": result}, author="agent:" + run.provider)
        return {"object_id": obj.id, "revision_id": revision.id, "result": result, "coverage": "partial"}
