"""Read-only compatibility for retired bounded-search records.

There is one executable autonomous solver: continuous mathematical research.
Historical route, gap and memory indexes remain readable and exportable.
"""

from mathagent.application.errors import DomainError
from mathagent.application.state import record
from mathagent.persistence.agent_models import AgentRun
from mathagent.persistence.search_models import (
    SearchDecision,
    SearchGap,
    SearchMemory,
    SearchRoute,
    SearchSession,
    SearchWork,
)
from sqlalchemy import select

# Kept only to recognize old permission lists and historical action records.
SEARCH_ACTIONS = {"propose_routes", "report_progress", "report_gap", "propose_check", "request_memory", "share_memory"}


class SearchController:
    def __init__(self, runtime):
        self.runtime = runtime

    def session_for(self, session, run_id):
        agent = session.get(AgentRun, run_id)
        return session.get(SearchSession, agent.root_run_id if agent else run_id)

    def inputs_stale(self, session, run, attempt):
        return bool(self.session_for(session, run.id))

    def snapshot(self, session, run_id):
        self.runtime._run(session, run_id)
        search = self.session_for(session, run_id)
        if not search:
            return {"session": None, "routes": [], "gaps": [], "memory": [], "decisions": [], "budget": None}
        result = {"session": record(search), "retired": True, "budget": None}
        for key, model in (("routes", SearchRoute), ("gaps", SearchGap), ("memory", SearchMemory),
                           ("decisions", SearchDecision), ("works", SearchWork)):
            result[key] = [record(row) for row in session.scalars(
                select(model).where(model.root_run_id == search.root_run_id))]
        return result

    def intervene_route(self, session, payload):
        raise DomainError(409, "solver_retired", "旧路线仅保留历史记录。请从原题新建连续研究任务。")
