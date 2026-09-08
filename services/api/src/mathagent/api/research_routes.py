"""Authenticated human recording and display-state API; no external retrieval."""

from typing import Annotated

from fastapi import APIRouter, Depends
from mathagent.api.schemas import Body, Command, Id
from mathagent.application.research_records import FailurePayload, SourcePayload
from pydantic import Field


class FailureCreate(FailurePayload):
    branch_id: Id
    body: Body


class SourceCreate(SourcePayload):
    branch_id: Id
    body: Body


class PresentationUpdate(Command):
    expected_version: Annotated[int, Field(ge=0, strict=True)]
    hidden_object_ids: list[Id] = Field(max_length=10_000)
    collapsed_object_ids: list[Id] = Field(max_length=10_000)
    archived: bool = Field(strict=True)


def build_research_router(service, human, key, command):
    router = APIRouter(dependencies=[Depends(human)], tags=["research-records"])

    @router.post("/failures")
    def failure(payload: FailureCreate, command_key: str = Depends(key)):
        return command(
            "failure.create", command_key, payload.model_dump(mode="json"), service.create_failure
        )

    @router.post("/sources")
    def source(payload: SourceCreate, command_key: str = Depends(key)):
        return command(
            "source.create", command_key, payload.model_dump(mode="json"), service.create_source
        )

    @router.get("/branches/{branch_id}/presentation")
    def presentation(branch_id: str):
        return service.get_presentation(branch_id)

    @router.put("/branches/{branch_id}/presentation")
    def update_presentation(
        branch_id: str, payload: PresentationUpdate, command_key: str = Depends(key)
    ):
        return command(
            "workspace.presentation_update",
            command_key,
            {**payload.model_dump(mode="json"), "branch_id": branch_id},
            service.update_presentation,
        )

    return router
