"""Human-only workspace contracts and routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from mathagent.api.schemas import Body, Command, Id
from pydantic import Field


class Position(Command):
    x: Annotated[float, Field(allow_inf_nan=False, ge=-1_000_000, le=1_000_000)]
    y: Annotated[float, Field(allow_inf_nan=False, ge=-1_000_000, le=1_000_000)]


class LayoutUpdate(Command):
    expected_version: Annotated[int, Field(ge=0, strict=True)]
    positions: dict[Id, Position] = Field(max_length=10_000)


class AnnotationCreate(Command):
    branch_id: Id
    revision_id: Id
    body: Body
    anchor_quote: Annotated[str, Field(min_length=1, max_length=20_000)] | None = None


class BlockUpdate(Command):
    branch_id: Id
    expected_version: Annotated[int, Field(ge=0, strict=True)]
    expected_body: Annotated[str, Field(max_length=200_000)]
    body: Annotated[str, Field(max_length=200_000)]


class ResolveConflict(Command):
    branch_id: Id
    expected_current_revision_id: Id
    selected_revision_id: Id


def build_workspace_router(service, human, key, command):
    router = APIRouter(dependencies=[Depends(human)], tags=["workspace"])

    @router.get("/projects/{project_id}/branches")
    def branches(project_id: str):
        return service.list_branches(project_id)

    @router.get("/branches/{branch_id}/layout")
    def layout(branch_id: str):
        return service.get_layout(branch_id)

    @router.put("/branches/{branch_id}/layout")
    def update_layout(branch_id: str, p: LayoutUpdate, k: str = Depends(key)):
        return command(
            "workspace.layout_update",
            k,
            {**p.model_dump(mode="json"), "branch_id": branch_id},
            service.update_layout,
        )

    @router.post("/annotations")
    def annotation(p: AnnotationCreate, k: str = Depends(key)):
        return command("annotation.create", k, p.model_dump(mode="json"), service.add_annotation)

    @router.get("/branches/{branch_id}/annotations")
    def annotations(branch_id: str, revision_id: str | None = None):
        return service.get_annotations(branch_id, revision_id)

    @router.post("/manuscript/blocks/{block_id}/revisions")
    def update_block(block_id: str, p: BlockUpdate, k: str = Depends(key)):
        return command(
            "manuscript.block_revise",
            k,
            {**p.model_dump(mode="json"), "block_id": block_id},
            service.revise_block,
        )

    @router.get("/manuscript/blocks/{block_id}/revisions")
    def block_revisions(block_id: str):
        return service.get_block_revisions(block_id)

    @router.post("/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: str, p: ResolveConflict, k: str = Depends(key)):
        return command(
            "conflict.resolve",
            k,
            {**p.model_dump(mode="json"), "conflict_id": conflict_id},
            service.resolve_conflict,
        )

    @router.get("/projects/{project_id}/search")
    def search(
        project_id: str,
        q: str = Query(min_length=1, max_length=300),
        branch_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=200),
    ):
        return service.search(project_id, q, branch_id, limit)

    return router
