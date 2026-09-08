"""Authenticated immutable exports and the offline elementary research example."""

import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from mathagent.api.schemas import Command, Id
from mathagent.exports.service import ExportService, export_zip
from mathagent.tools.hermitian import HermitianExample
from pydantic import Field


class ExportCreate(Command):
    project_id: Id
    branch_id: Id
    object_ids: list[Id] | None = Field(default=None, min_length=1, max_length=10_000)


def build_artifact_router(service, human, key, command):
    router = APIRouter(dependencies=[Depends(human)], tags=["artifacts"])
    exports = ExportService(service)
    example = HermitianExample(service)

    @router.post("/exports", status_code=201)
    def create_export(p: ExportCreate, k: str = Depends(key)):
        response = command("export.create", k, p.model_dump(mode="json"), exports.create)
        # The complete immutable bundle stays in the idempotent command receipt.
        body = json.loads(response.body)
        body.pop("bundle", None)
        return JSONResponse(body, status_code=response.status_code)

    @router.get("/exports/{export_id}/download")
    def download(export_id: str):
        return Response(
            export_zip(exports.get(export_id)),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="research-export.zip"',
                "Cache-Control": "private, no-store",
            },
        )

    @router.post("/examples/hermitian", status_code=201)
    def hermitian(p: Command, k: str = Depends(key)):
        return command("example.hermitian", k, p.model_dump(mode="json"), example.create)

    return router
