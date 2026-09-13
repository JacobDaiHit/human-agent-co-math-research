"""Worker and human runtime contracts; credentials never appear in read responses."""

from typing import Annotated, Literal

from fastapi import Depends
from mathagent.api.schemas import Command, Id
from mathagent.providers.remote import provider_status
from pydantic import Field

Provider = Literal["fake", "deepseek", "glm"]


class Settings(Command):
    request_budget: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    allow_real_api: bool
    allowed_providers: list[Provider] = Field(min_length=1, max_length=3)


class ClaimNext(Command):
    providers: list[Provider] = Field(default_factory=lambda: ["fake"], min_length=1, max_length=3)


class Execution(Command):
    token: Id


class RequestReservation(Execution):
    # The worker sends the exact cap it will give the provider.  The service
    # durably reserves this amount before dispatch, so concurrent descendants
    # cannot collectively exceed an enabled cumulative output budget.
    requested_output_tokens: Annotated[int, Field(strict=True, ge=256, le=65_536)] | None = None


class Heartbeat(Execution):
    boundary: bool = False


class Settlement(Execution):
    outcome: Literal["spent", "unaccepted", "unknown"]
    retry_unknown: bool = False
    usage: dict[str, Annotated[int, Field(strict=True, ge=0)]] = Field(
        default_factory=dict, max_length=30
    )
    provider_request_id: Annotated[str, Field(max_length=300)] | None = None
    reason: Annotated[str, Field(max_length=1000)] = ""


class Failure(Execution):
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    retryable: bool = False


class Reconcile(Command):
    outcome: Literal["spent", "unaccepted"]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


def mount_runtime_routes(app, runtime, service, human, worker, key, command):
    @app.get("/providers/status", dependencies=[Depends(human)])
    def status():
        return provider_status()

    @app.get("/projects/{project_id}/runtime-settings", dependencies=[Depends(human)])
    def settings(project_id: str):
        return runtime.get_settings(project_id)

    @app.put("/projects/{project_id}/runtime-settings", dependencies=[Depends(human)])
    def update_settings(project_id: str, p: Settings, k: str = Depends(key)):
        return command(
            "runtime.settings",
            k,
            {**p.model_dump(), "project_id": project_id},
            runtime.update_settings,
        )

    @app.get("/projects/{project_id}/budget", dependencies=[Depends(human)])
    def project_budget(project_id: str):
        return runtime.get_budget(project_id=project_id)

    @app.get("/runs/{run_id}/budget", dependencies=[Depends(human)])
    def run_budget(run_id: str):
        return runtime.get_budget(run_id=run_id)

    @app.post("/worker/claim-next", dependencies=[Depends(worker)])
    def claim_next(p: ClaimNext, k: str = Depends(key)):
        return command("worker.claim_next", k, p.model_dump(), runtime.claim_next)

    @app.post("/attempts/{attempt_id}/heartbeat", dependencies=[Depends(worker)])
    def heartbeat(attempt_id: str, p: Heartbeat, k: str = Depends(key)):
        return command(
            "attempt.heartbeat", k, {**p.model_dump(), "attempt_id": attempt_id}, runtime.heartbeat
        )

    @app.post("/attempts/{attempt_id}/requests", dependencies=[Depends(worker)])
    def reserve(attempt_id: str, p: RequestReservation, k: str = Depends(key)):
        return command(
            "request.reserve",
            k,
            {**p.model_dump(), "attempt_id": attempt_id},
            runtime.reserve_request,
        )

    @app.post("/requests/{request_id}/start", dependencies=[Depends(worker)])
    def start(request_id: str, p: Execution, k: str = Depends(key)):
        return command(
            "request.start", k, {**p.model_dump(), "request_id": request_id}, runtime.start_request
        )

    @app.post("/requests/{request_id}/settle", dependencies=[Depends(worker)])
    def settle(request_id: str, p: Settlement, k: str = Depends(key)):
        return command(
            "request.settle",
            k,
            {**p.model_dump(), "request_id": request_id},
            runtime.settle_request,
        )

    @app.post("/attempts/{attempt_id}/fail", dependencies=[Depends(worker)])
    def fail(attempt_id: str, p: Failure, k: str = Depends(key)):
        return command(
            "attempt.fail", k, {**p.model_dump(), "attempt_id": attempt_id}, runtime.fail
        )

    @app.post("/requests/{request_id}/reconcile", dependencies=[Depends(human)])
    def reconcile(request_id: str, p: Reconcile, k: str = Depends(key)):
        return command(
            "request.reconcile",
            k,
            {**p.model_dump(), "request_id": request_id},
            runtime.reconcile_request,
        )
