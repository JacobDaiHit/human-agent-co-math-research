"""Human control/read APIs and worker-only durable research proposal endpoints."""

from fastapi import Depends
from mathagent.api.schemas import Command, Id
from mathagent.application.errors import DomainError
from mathagent.application.state import record
from mathagent.persistence.agent_models import AgentStep, ProviderCall
from mathagent.persistence.models import Project
from mathagent.persistence.runtime_models import ProviderRequest
from mathagent.providers.actions import operation_schemas
from mathagent.providers.options import MAX_OUTPUT_TOKENS, ReasoningEffort, ThinkingMode
from pydantic import Field
from sqlalchemy import select


class RunUpdate(Command):
    request_budget: int = Field(ge=1, le=1000)
    autonomous: bool
    max_steps: int = Field(ge=1, le=40)
    max_review_rounds: int = Field(ge=0, le=2)
    max_children: int = Field(ge=0, le=12)
    max_depth: int = Field(ge=0, le=4)
    max_output_tokens: int = Field(ge=256, le=MAX_OUTPUT_TOKENS)
    request_timeout_seconds: int = Field(ge=1, le=600)
    thinking_mode: ThinkingMode = "provider_default"
    reasoning_effort: ReasoningEffort = "provider_default"


class BranchBudget(Command):
    request_budget: int = Field(ge=0, le=1000000)


class BranchIntervention(Command):
    action: str = Field(pattern="^(pause|cancel|steer|resume)$")
    instruction: str = Field(default="", max_length=20000)


class ObservationCreate(Command):
    token: Id
    observation: dict
    result: dict | None = None


class StepCreate(Command):
    token: Id
    request_id: Id
    result: dict


class PolicyUpdate(Command):
    allowed_operations: list[str] = Field(max_length=12)


def mount_agent_routes(app, runtime, human, worker, key, command):
    agent = runtime.agent

    @app.get("/runs/{run_id}/options", dependencies=[Depends(human)])
    def options(run_id: str):
        with runtime.service.db.sessions() as session:
            return agent.options(session, run_id)

    @app.put("/runs/{run_id}/options", dependencies=[Depends(human)])
    def update_options(run_id: str, p: RunUpdate, k: str = Depends(key)):
        return command("agent.options", k, {"run_id": run_id, **p.model_dump()}, agent.update_options)

    @app.get("/branches/{branch_id}/runtime-settings", dependencies=[Depends(human)])
    def branch_settings(branch_id: str):
        with runtime.service.db.sessions() as session:
            return agent.branch_settings(session, branch_id)

    @app.put("/branches/{branch_id}/runtime-settings", dependencies=[Depends(human)])
    def branch_budget(branch_id: str, p: BranchBudget, k: str = Depends(key)):
        return command("agent.branch_budget", k, {"branch_id": branch_id, **p.model_dump()}, agent.update_branch_settings)

    @app.post("/branches/{branch_id}/interventions", dependencies=[Depends(human)])
    def branch_intervention(branch_id: str, p: BranchIntervention, k: str = Depends(key)):
        return command("agent.branch_intervention", k, {"branch_id": branch_id, **p.model_dump()}, agent.intervene_branch)

    @app.get("/runs/{run_id}/steps", dependencies=[Depends(human)])
    def steps(run_id: str):
        with runtime.service.db.sessions() as session:
            runtime._run(session, run_id)
            return {"steps": [record(r) for r in session.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.number))]}

    @app.get("/runs/{run_id}/calls", dependencies=[Depends(human)])
    def calls(run_id: str):
        with runtime.service.db.sessions() as session:
            runtime._run(session, run_id)
            rows = session.scalars(select(ProviderCall).join(ProviderRequest, ProviderRequest.id == ProviderCall.request_id).where(ProviderRequest.run_id == run_id).order_by(ProviderCall.created_at)).all()
            return {"calls": [record(r) for r in rows]}

    @app.post("/requests/{request_id}/observation", dependencies=[Depends(worker)])
    def observation(request_id: str, p: ObservationCreate, k: str = Depends(key)):
        return command("agent.observation", k, {"request_id": request_id, **p.model_dump()}, agent.observe)

    @app.post("/attempts/{attempt_id}/steps", dependencies=[Depends(worker)])
    def step(attempt_id: str, p: StepCreate, k: str = Depends(key)):
        return command("agent.step", k, {"attempt_id": attempt_id, **p.model_dump()}, agent.apply_step)

    @app.get("/projects/{project_id}/agent-policy", dependencies=[Depends(human)])
    def policy(project_id: str):
        with runtime.service.db.sessions() as session:
            project = session.get(Project, project_id)
            if not project:
                raise DomainError(404, "project_not_found", "研究项目不存在")
            return {"project_id": project_id, "allowed_operations": project.policies.get("agent_operations", list(operation_schemas()))}

    @app.put("/projects/{project_id}/agent-policy", dependencies=[Depends(human)])
    def update_policy(project_id: str, p: PolicyUpdate, k: str = Depends(key)):
        def apply(session, payload):
            project = session.get(Project, project_id)
            if not project:
                raise DomainError(404, "project_not_found", "研究项目不存在")
            if set(payload["allowed_operations"]) - set(operation_schemas()):
                raise DomainError(422, "invalid_operation_policy", "包含不支持的操作")
            project.policies = {**project.policies, "agent_operations": list(dict.fromkeys(payload["allowed_operations"]))}
            runtime.service.emit(session, project_id, None, "project.agent_policy_changed", payload)
            return 200, payload
        return command("agent.policy", k, {"project_id": project_id, **p.model_dump()}, apply)
