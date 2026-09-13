"""Local authenticated API. No paid providers or background worker start implicitly."""

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from mathagent import __version__
from mathagent.api.agent_routes import mount_agent_routes
from mathagent.api.artifact_routes import build_artifact_router
from mathagent.api.research_assets_routes import build_research_assets_router
from mathagent.api.research_routes import build_research_router
from mathagent.api.runtime_routes import mount_runtime_routes
from mathagent.api.schemas import (
    AdoptionCreate,
    BlockCreate,
    BranchCreate,
    CompleteAttempt,
    InterventionCreate,
    ObjectCreate,
    ProjectCreate,
    ProofCreate,
    RelationCreate,
    ReviewCreate,
    RevisionCreate,
    RunCreate,
)
from mathagent.api.workspace_routes import build_workspace_router
from mathagent.application.errors import DomainError
from mathagent.application.research_records import ResearchRecordsService
from mathagent.application.state import StateService
from mathagent.application.workspace import WorkspaceService
from mathagent.config import load_local_environment
from mathagent.persistence.database import Database
from mathagent.runtime.service import Runtime

bearer = HTTPBearer(auto_error=False)


def _local_secret(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return path.read_text(encoding="utf-8").strip()


def create_app(database_path=None, token=None, worker_token=None, allowed_origins=None):
    load_local_environment()
    database_path = Path(database_path or os.getenv("MATHAGENT_DATABASE", "data/mathagent.db"))
    user_secret = (
        token
        or os.getenv("MATHAGENT_TOKEN")
        or _local_secret(database_path.parent / "session.token")
    )
    worker_secret = (
        worker_token
        or os.getenv("MATHAGENT_WORKER_TOKEN")
        or _local_secret(database_path.parent / "worker.token")
    )
    if not user_secret or not worker_secret or user_secret == worker_secret:
        raise ValueError("Human and worker tokens must be distinct and nonempty.")
    origins = set(allowed_origins or {"http://127.0.0.1:8000", "http://localhost:8000"})
    if os.getenv("MATHAGENT_FRONTEND_ORIGIN"):
        origins.add(os.environ["MATHAGENT_FRONTEND_ORIGIN"])
    database = Database(database_path)
    service = StateService(database)
    workspace = WorkspaceService(service)
    runtime = Runtime(service)

    @asynccontextmanager
    async def lifespan(app):
        database.migrate()
        yield
        database.close()

    app = FastAPI(
        title="MathAgent 研究状态服务",
        version=__version__,
        lifespan=lifespan,
        description="本地研究工作台与受控运行接口。support 表示证据策略下的支持，不是数学真值。Fake 运行均是确定性测试。",
    )
    app.state.database = database
    app.state.service = service
    app.state.workspace = workspace

    @app.middleware("http")
    async def api_prefix(request, call_next):
        if request.scope["path"].startswith("/api/"):
            request.scope["path"] = request.scope["path"][4:]
            request.scope["raw_path"] = request.scope["path"].encode()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_request, error):
        return JSONResponse(error.response, status_code=error.status)

    def check_auth(request, credential, expected):
        if not credential or not secrets.compare_digest(credential.credentials, expected):
            raise DomainError(401, "unauthorized", "需要有效的本机会话令牌。")
        if request.headers.get("origin") and request.headers["origin"] not in origins:
            raise DomainError(403, "origin_rejected", "此网页来源不允许访问本地研究材料。")

    def human(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credential is None and request.cookies.get("mathagent_session"):
            if (
                request.method not in {"GET", "HEAD"}
                and request.headers.get("x-mathagent-client") != "workbench"
            ):
                raise DomainError(403, "csrf_rejected", "本地网页操作缺少请求校验。")
            credential = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=request.cookies["mathagent_session"]
            )
        check_auth(request, credential, user_secret)

    def worker(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
        check_auth(request, credential, worker_secret)

    def key(value: str = Header(alias="Idempotency-Key", min_length=1, max_length=200)):
        return value

    def command(operation, command_key, payload, handler):
        status, response = service.execute(operation, command_key, payload, handler)
        return JSONResponse(response, status_code=status)

    @app.post("/session")
    def local_session(request: Request):
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise DomainError(403, "nonlocal_host", "仅允许从本机地址打开研究工作台。")
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise DomainError(403, "nonlocal_client", "网页会话只能从本机建立。")
        if request.headers.get("origin") and request.headers["origin"] not in origins:
            raise DomainError(403, "origin_rejected", "此网页来源不能建立会话。")
        if request.headers.get("x-mathagent-client") != "workbench":
            raise DomainError(403, "csrf_rejected", "本地网页操作缺少请求校验。")
        response = JSONResponse({"authenticated": True, "local_only": True})
        response.set_cookie(
            "mathagent_session", user_secret, httponly=True, samesite="strict", path="/"
        )
        return response

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "stage": "offline-api-readiness",
            "real_models_enabled": os.getenv("MATHAGENT_ENABLE_REAL_API") == "1",
        }

    @app.get("/projects", dependencies=[Depends(human)])
    def projects():
        return service.list_projects()

    @app.post("/projects", dependencies=[Depends(human)])
    def project_create(p: ProjectCreate, k: str = Depends(key)):
        return command("project.create", k, p.model_dump(mode="json"), service.create_project)

    @app.post("/objects", dependencies=[Depends(human)])
    def object_create(p: ObjectCreate, k: str = Depends(key)):
        return command("object.create", k, p.model_dump(mode="json"), service.create_object)

    @app.post("/objects/{object_id}/revisions", dependencies=[Depends(human)])
    def revise(object_id: str, p: RevisionCreate, k: str = Depends(key)):
        return command(
            "object.revise",
            k,
            {**p.model_dump(mode="json"), "object_id": object_id},
            service.revise_object,
        )

    @app.get("/objects/{object_id}/revisions", dependencies=[Depends(human)])
    def revisions(object_id: str):
        return service.get_revisions(object_id)

    @app.post("/branches", dependencies=[Depends(human)])
    def branch_create(p: BranchCreate, k: str = Depends(key)):
        return command("branch.create", k, p.model_dump(mode="json"), service.create_branch)

    @app.post("/proof-plans", dependencies=[Depends(human)])
    def proof_create(p: ProofCreate, k: str = Depends(key)):
        return command("proof.create", k, p.model_dump(mode="json"), service.create_proof)

    @app.post("/reviews", dependencies=[Depends(human)])
    def review_create(p: ReviewCreate, k: str = Depends(key)):
        return command("review.create", k, p.model_dump(mode="json"), service.add_review)

    @app.post("/adoptions", dependencies=[Depends(human)])
    def adoption_create(p: AdoptionCreate, k: str = Depends(key)):
        return command("adoption.create", k, p.model_dump(mode="json"), service.adopt)

    @app.post("/relations", dependencies=[Depends(human)])
    def relation_create(p: RelationCreate, k: str = Depends(key)):
        return command("relation.create", k, p.model_dump(mode="json"), service.add_relation)

    @app.post("/manuscript/blocks", dependencies=[Depends(human)])
    def block_create(p: BlockCreate, k: str = Depends(key)):
        return command("block.create", k, p.model_dump(mode="json"), service.add_block)

    @app.get("/projects/{project_id}/snapshot", dependencies=[Depends(human)])
    def snapshot(project_id: str, branch_id: str | None = None):
        return workspace.get_snapshot(project_id, branch_id)

    @app.get("/projects/{project_id}/events", dependencies=[Depends(human)])
    def events(project_id: str, after_seq: int = Query(0, ge=0)):
        return service.get_events(project_id, after_seq)

    @app.get("/projects/{project_id}/stream", dependencies=[Depends(human)])
    async def stream(project_id: str, request: Request, after_seq: int = Query(0, ge=0)):
        raw_cursor = request.headers.get("last-event-id", str(after_seq))
        try:
            cursor = int(raw_cursor)
            if cursor < 0:
                raise ValueError
        except ValueError:
            raise DomainError(422, "invalid_event_cursor", "事件游标必须是非负整数。") from None
        service.get_events(project_id, cursor)

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(service.get_events, project_id, cursor)
                for item in batch["events"]:
                    yield f"id: {item['seq']}\nevent: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    cursor = item["seq"]
                if not batch["events"]:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/runs", dependencies=[Depends(human)])
    def run_create(p: RunCreate, k: str = Depends(key)):
        return command("run.create", k, p.model_dump(mode="json"), runtime.create)

    @app.post("/runs/{run_id}/claim", dependencies=[Depends(worker)])
    def claim(run_id: str, k: str = Depends(key)):
        return command("run.claim", k, {"run_id": run_id}, runtime.claim)

    @app.post("/attempts/{attempt_id}/complete", dependencies=[Depends(worker)])
    def complete(attempt_id: str, p: CompleteAttempt, k: str = Depends(key)):
        return command(
            "attempt.complete",
            k,
            {**p.model_dump(mode="json"), "attempt_id": attempt_id},
            runtime.complete,
        )

    @app.post("/runs/{run_id}/interventions", dependencies=[Depends(human)])
    def intervene(run_id: str, p: InterventionCreate, k: str = Depends(key)):
        return command(
            "run.intervene", k, {**p.model_dump(mode="json"), "run_id": run_id}, runtime.intervene
        )

    @app.post("/runs/{run_id}/resume", dependencies=[Depends(human)])
    def resume(run_id: str, k: str = Depends(key)):
        return command("run.resume", k, {"run_id": run_id}, runtime.resume)

    app.include_router(build_workspace_router(workspace, human, key, command))
    app.include_router(build_research_assets_router(service, human, key, command))
    mount_runtime_routes(app, runtime, service, human, worker, key, command)
    app.include_router(build_artifact_router(service, human, key, command))
    app.include_router(build_research_router(ResearchRecordsService(service), human, key, command))
    mount_agent_routes(app, runtime, human, worker, key, command)

    frontend = Path(
        os.getenv(
            "MATHAGENT_FRONTEND_DIST",
            str(Path(__file__).resolve().parents[5] / "apps" / "web" / "dist"),
        )
    )
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def frontend_index():
        if (frontend / "index.html").is_file():
            return FileResponse(frontend / "index.html")
        return JSONResponse(
            {"message": "前端尚未构建；请运行 apps/web 下的 npm run build。", "docs": "/docs"}
        )

    return app
