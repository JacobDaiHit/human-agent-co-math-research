"""Scoped, revision-backed memory for bounded search.

The service is intentionally usable by a future controller without importing
the controller itself.  It only grants references; it never promotes a
candidate to a mathematical fact.
"""

from mathagent.application.errors import DomainError
from mathagent.persistence.models import Dependency, Head, ProofPlan, Revision, Run
from mathagent.persistence.research_models import ResearchRecordReference
from mathagent.persistence.search_models import SearchMemory, SearchRoute, SearchSession
from sqlalchemy import or_, select


class MemoryService:
    def __init__(self, state):
        self.state = state

    @staticmethod
    def _session(session, root_id):
        row = session.get(SearchSession, root_id)
        if not row:
            raise DomainError(404, "search_session_not_found", "受控搜索会话不存在。")
        return row

    def _root_branch(self, session, root_id):
        run = session.get(Run, root_id)
        if not run:
            raise DomainError(404, "run_not_found", "根任务不存在。")
        return self.state.require_branch(session, run.branch_id)

    def _route(self, session, root_id, route_id):
        if route_id is None:
            return None
        route = session.get(SearchRoute, route_id)
        if not route or route.root_run_id != root_id:
            raise DomainError(422, "search_route_outside_session", "路线不属于该搜索会话。")
        return route

    def _project_revision(self, session, project_id, revision_id):
        revision = self.state.require_revision(session, revision_id)
        obj = self.state.require_object(session, revision.object_id)
        if obj.project_id != project_id:
            raise DomainError(422, "cross_project_reference", "不能引用其他项目的版本。")
        if revision.payload.get("deleted"):
            raise DomainError(410, "material_deleted", "此材料已永久删除，不能写入搜索记忆。")
        return revision

    @staticmethod
    def _snapshot_visible(session, branch_id):
        return {head.object_id: head.revision_id for head in session.scalars(
            select(Head).where(Head.branch_id == branch_id)
        )}

    @staticmethod
    def _payload_references(value, key=""):
        """Extract declared revision references without treating prose as links."""
        if isinstance(value, dict):
            if key in {"read_set", "dependency_snapshot"}:
                return {item for item in value.values() if isinstance(item, str)}
            found = set()
            for name, item in value.items():
                found.update(MemoryService._payload_references(item, name))
            return found
        if isinstance(value, list):
            return set().union(*(MemoryService._payload_references(item, key) for item in value))
        if isinstance(value, str) and (key.endswith("revision_id") or key.endswith("revision_ids")):
            return {value}
        if isinstance(value, str) and key in {"read_refs", "read_set", "dependency_snapshot"}:
            return {value}
        return set()

    def _dependency_snapshot(self, session, project_id, revision_id, extra_revision_ids=()):
        """Freeze only declared dependencies, not every mutable branch head."""
        pending, visited, snapshot = [revision_id, *extra_revision_ids], set(), {}
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            revision = self._project_revision(session, project_id, current)
            obj = self.state.require_object(session, revision.object_id)
            snapshot[obj.id] = revision.id
            pending.extend(self._payload_references(revision.payload))
            plan = session.get(ProofPlan, revision.id)
            if plan:
                pending.append(plan.conclusion_revision_id)
                pending.extend(session.scalars(select(Dependency.revision_id).where(
                    Dependency.plan_revision_id == revision.id
                )))
            pending.extend(session.scalars(select(ResearchRecordReference.target_revision_id).where(
                ResearchRecordReference.record_revision_id == revision.id
            )))
        return snapshot

    def _initial_revisions(self, session, root_id):
        search = self._session(session, root_id)
        frozen = search.config.get("initial_read_set", {})
        if isinstance(frozen, dict):
            return set(frozen.values())
        # Legacy callers without a frozen config may safely use only the root
        # branch's current heads; session creation should always freeze it.
        return set(self._snapshot_visible(session, self._root_branch(session, root_id).id).values())

    def allowed_revisions(self, session, root_id, route_id):
        """Return the exact references a route may consume without an import."""
        search = self._session(session, root_id)
        route = self._route(session, root_id, route_id)
        allowed = self._initial_revisions(session, root_id)
        if route and route.branch_id:
            branch = self.state.require_branch(session, route.branch_id)
            if branch.project_id != search.project_id:
                raise DomainError(422, "cross_project_reference", "路线分支不属于该项目。")
        entries = session.scalars(select(SearchMemory).where(
            SearchMemory.root_run_id == root_id,
            SearchMemory.state == "current",
            (SearchMemory.route_id.is_(None) if route_id is None else or_(
                SearchMemory.route_id.is_(None), SearchMemory.route_id == route_id)),
        ))
        for entry in entries:
            allowed.add(entry.revision_id)
            # Explicit memory imports carry a fixed dependency closure.  These
            # references are authorized by the entry, never by arbitrary heads.
            allowed.update((entry.dependency_snapshot or {}).values())
        return allowed

    def record(self, session, root_id, route_id, revision_id, branch_id, kind, source_event,
               evidence_type="proposed", assumptions=None):
        search = self._session(session, root_id)
        route = self._route(session, root_id, route_id)
        revision = self._project_revision(session, search.project_id, revision_id)
        branch = self.state.require_branch(session, branch_id) if branch_id else None
        if branch and branch.project_id != search.project_id:
            raise DomainError(422, "cross_project_reference", "记忆来源分支不属于该项目。")
        if route and branch_id and route.branch_id and route.branch_id != branch_id:
            raise DomainError(422, "search_memory_wrong_branch", "路线记忆必须来自该路线分支。")
        existing = session.scalar(select(SearchMemory).where(
            SearchMemory.root_run_id == root_id, SearchMemory.source_event == source_event))
        if existing:
            if existing.revision_id != revision_id or existing.route_id != route_id:
                raise DomainError(409, "search_memory_event_conflict", "来源事件已记录为不同的记忆。")
            return existing
        snapshot = self._dependency_snapshot(session, search.project_id, revision.id)
        entry = SearchMemory(root_run_id=root_id, route_id=route_id, revision_id=revision.id,
            branch_id=branch_id, kind=kind, evidence_type=evidence_type,
            dependency_snapshot=snapshot, assumptions=list(assumptions or []),
            state="current", source_event=source_event)
        session.add(entry)
        session.flush()
        return entry

    def import_shared(self, session, root_id, route_id, memory_id, source_event):
        """Explicitly import a same-session item into one route after scope checks."""
        target = self._route(session, root_id, route_id)
        if not target:
            raise DomainError(422, "search_import_target_required", "共享材料必须导入到具体路线。")
        source = session.get(SearchMemory, memory_id)
        if not source or source.root_run_id != root_id or source.state != "current":
            raise DomainError(422, "search_import_not_available", "该记忆不能导入此会话。")
        if source.route_id == route_id:
            raise DomainError(422, "search_import_same_route", "路线已有该材料的本地范围。")
        imported = self.record(session, root_id, route_id, source.revision_id, target.branch_id,
            source.kind, source_event, source.evidence_type, source.assumptions)
        # The target branch has received a pinned copy through the controller,
        # but freshness belongs to the actual source branch.  Persist that
        # origin so an upstream source edit/deletion invalidates this import.
        imported.branch_id = source.branch_id
        imported.dependency_snapshot = {**source.dependency_snapshot, **imported.dependency_snapshot}
        return imported

    def refresh(self, session, root_id):
        """Mark only affected entries stale/deleted; alternatives remain current."""
        search = self._session(session, root_id)
        changed = []
        entries = list(session.scalars(select(SearchMemory).where(SearchMemory.root_run_id == root_id)))
        for entry in entries:
            revision = session.get(Revision, entry.revision_id)
            if not revision or revision.payload.get("deleted"):
                if entry.state != "deleted":
                    entry.state = "deleted"
                    changed.append(entry.id)
                continue
            snapshot = entry.dependency_snapshot or {}
            branch_id = entry.branch_id or (self._route(session, root_id, entry.route_id).branch_id if entry.route_id else self._root_branch(session, root_id).id)
            if branch_id and any(
                (head := session.get(Head, (branch_id, object_id))) is None or head.revision_id != expected
                for object_id, expected in snapshot.items()
            ) and entry.state == "current":
                entry.state = "stale"
                changed.append(entry.id)
        # An import/candidate can be pinned on another branch, so its local
        # head may remain unchanged after the real source becomes stale.  Walk
        # reverse dependency edges to invalidate exactly those consumers.
        while True:
            stale_versions = {entry.revision_id for entry in entries if entry.state in {"stale", "deleted"}}
            affected = [entry for entry in entries if entry.state == "current"
                        and set((entry.dependency_snapshot or {}).values()) & stale_versions]
            if not affected:
                break
            for entry in affected:
                entry.state = "stale"
                changed.append(entry.id)
        return {"root_run_id": root_id, "changed_entry_ids": changed, "project_id": search.project_id}

    def packet(self, session, root_id, route_id, required_revision_ids=(), offset=0, limit=40):
        self._session(session, root_id)
        self._route(session, root_id, route_id)
        if offset < 0 or limit < 1 or limit > 200:
            raise DomainError(422, "invalid_memory_pagination", "记忆分页范围无效。")
        allowed = self.allowed_revisions(session, root_id, route_id)
        required = set(required_revision_ids)
        if not required <= allowed:
            raise DomainError(422, "search_memory_not_authorized", "请求包含未授权的版本。")
        scope = SearchMemory.route_id.is_(None) if route_id is None else or_(
            SearchMemory.route_id.is_(None), SearchMemory.route_id == route_id)
        rows = list(session.scalars(select(SearchMemory).where(
            SearchMemory.root_run_id == root_id, scope, SearchMemory.state == "current"
        ).order_by(SearchMemory.id)))
        entries = [self._entry(row) for row in rows[offset:offset + limit]]
        refs = self._read_refs(session, root_id, route_id, required | {row["revision_id"] for row in entries})
        return {"entries": entries, "read_refs": refs, "offset": offset, "limit": limit,
                "total": len(rows), "next_offset": offset + len(entries) if offset + len(entries) < len(rows) else None}

    @staticmethod
    def _entry(row):
        return {"id": row.id, "route_id": row.route_id, "revision_id": row.revision_id,
                "branch_id": row.branch_id, "kind": row.kind, "evidence_type": row.evidence_type,
                "dependency_snapshot": row.dependency_snapshot, "assumptions": row.assumptions,
                "state": row.state, "source_event": row.source_event}

    def _read_refs(self, session, root_id, route_id, revision_ids):
        root_branch = self._root_branch(session, root_id).id
        entries = list(session.scalars(select(SearchMemory).where(
            SearchMemory.root_run_id == root_id, SearchMemory.revision_id.in_(revision_ids)
        ))) if revision_ids else []
        branch_by_revision = {entry.revision_id: entry.branch_id for entry in entries if entry.branch_id}
        refs = []
        for revision_id in sorted(revision_ids):
            revision = self.state.require_revision(session, revision_id)
            refs.append({"object_id": revision.object_id, "revision_id": revision_id,
                "branch_id": branch_by_revision.get(revision_id) or root_branch,
                "section": "record", "offset": 0})
        return refs

    def purge_project(self, session, project_id, deleted_revision_ids):
        """Tombstone affected indexes without deleting the controller ledger."""
        deleted = set(deleted_revision_ids)
        changed = []
        roots = set(session.scalars(select(SearchSession.root_run_id).where(SearchSession.project_id == project_id)))
        for entry in session.scalars(select(SearchMemory).where(SearchMemory.root_run_id.in_(roots))):
            if entry.revision_id in deleted or set((entry.dependency_snapshot or {}).values()) & deleted:
                if entry.state != "deleted":
                    entry.state = "deleted"
                    changed.append(entry.id)
        return {"project_id": project_id, "changed_entry_ids": changed}
