"""Human-controlled semantic merge, article assembly and material erasure."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from mathagent.api.schemas import Body, Command, Id
from mathagent.application.articles import ArticleService
from mathagent.application.branch_merge import BranchMergeService
from mathagent.application.code_execution import CodeExecutionService
from mathagent.application.deletion import DeletionService
from pydantic import Field


class MergeResolution(Command):
    object_id: Id
    choice: Literal["source", "target", "rewrite"]
    reason: Annotated[str, Field(min_length=1, max_length=20000)]
    body: Body | None = None
    payload: dict | None = None


class MergeApply(Command):
    source_branch_id: Id
    preview_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    resolutions: list[MergeResolution] = Field(max_length=10000)


class ArticleSection(Command):
    heading: Annotated[str, Field(min_length=1, max_length=500)]
    revision_ids: list[Id] = Field(min_length=1, max_length=100)
    transition: Annotated[str, Field(max_length=20000)] = ""


class ArticleCreate(Command):
    branch_id: Id
    title: Annotated[str, Field(min_length=1, max_length=500)]
    sections: list[ArticleSection] = Field(min_length=1, max_length=40)


class MaterialDelete(Command):
    preview_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    confirmation: Literal["永久删除"]


class SandboxConfigure(Command):
    enabled: bool


def build_research_assets_router(state, human, key, command):
    router = APIRouter(dependencies=[Depends(human)], tags=["research-assets"])
    merge, articles, deletion = BranchMergeService(state), ArticleService(state), DeletionService(state)
    sandbox = CodeExecutionService(state)

    @router.get("/projects/{project_id}/code-sandbox")
    def sandbox_status(project_id: str):
        with state.db.sessions() as session:
            return sandbox.status(session, project_id)

    @router.put("/projects/{project_id}/code-sandbox")
    def sandbox_configure(project_id: str, p: SandboxConfigure, k: str = Depends(key)):
        return command("code_sandbox.configure", k, {"project_id": project_id, **p.model_dump()}, sandbox.configure)

    @router.get("/branches/{branch_id}/merge-preview")
    def merge_preview(branch_id: str, source_branch_id: str):
        with state.db.sessions() as session:
            return merge.preview(session, {"source_branch_id": source_branch_id, "target_branch_id": branch_id})[1]

    @router.post("/branches/{branch_id}/merge")
    def merge_apply(branch_id: str, p: MergeApply, k: str = Depends(key)):
        return command("branch.merge", k, {**p.model_dump(mode="json"), "target_branch_id": branch_id}, merge.apply)

    @router.post("/articles")
    def article_create(p: ArticleCreate, k: str = Depends(key)):
        return command("article.create", k, p.model_dump(mode="json"), articles.create)

    @router.get("/articles/{revision_id}/check")
    def article_check(revision_id: str, branch_id: str):
        with state.db.sessions() as session:
            return articles.check(session, {"revision_id": revision_id, "branch_id": branch_id})[1]

    @router.get("/objects/{object_id}/deletion-preview")
    def deletion_preview(object_id: str):
        with state.db.sessions() as session:
            return deletion.preview(session, {"object_id": object_id})[1]

    @router.post("/objects/{object_id}/permanent-delete")
    def permanent_delete(object_id: str, p: MaterialDelete, k: str = Depends(key)):
        status, response = state.execute("material.permanent_delete", k,
            {**p.model_dump(mode="json"), "object_id": object_id}, deletion.erase)
        if status == 200 and response.get("deleted"):
            response = deletion.cleanup(response)
        return JSONResponse(response, status_code=status)

    return router
