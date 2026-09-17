"""Human-visible controller state and explicit route interventions."""

from typing import Literal

from fastapi import Depends
from mathagent.api.schemas import Command
from pydantic import Field


class RouteIntervention(Command):
    action: Literal["pause", "resume", "priority"]
    priority: int = Field(default=0, ge=-10, le=10)


def mount_search_routes(app, runtime, human, key, command):
    @app.get("/runs/{run_id}/search", dependencies=[Depends(human)])
    def status(run_id: str):
        with runtime.service.db.sessions() as session:
            return runtime.search.snapshot(session, run_id)

    @app.post("/runs/{root_id}/search/routes/{route_id}/interventions", dependencies=[Depends(human)])
    def intervene(root_id: str, route_id: str, p: RouteIntervention, k: str = Depends(key)):
        return command("search.route_intervention", k,
            {"root_run_id": root_id, "route_id": route_id, **p.model_dump()}, runtime.search.intervene_route)
