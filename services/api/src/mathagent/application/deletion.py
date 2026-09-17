"""Explicit irreversible erasure of material and its recorded local derivatives.

References and resource accounting survive as tombstones. Downloaded copies and
external backups are outside this database's authority and are reported as such.
"""

import hashlib
import json
import sqlite3

from mathagent.application.errors import DomainError
from mathagent.persistence.agent_models import AgentStep, BranchRuntime, ProviderCall
from mathagent.persistence.artifacts import ArtifactStore, collect_references, database_references
from mathagent.persistence.models import (
    Adoption,
    Attempt,
    Branch,
    CommandReceipt,
    Conflict,
    Dependency,
    Event,
    ManuscriptBlock,
    ProofPlan,
    ResearchObject,
    Review,
    Revision,
    Run,
    now,
)
from mathagent.persistence.research_models import ResearchRecordReference
from mathagent.persistence.runtime_models import ProviderRequest
from mathagent.persistence.search_models import (
    SearchDecision,
    SearchGap,
    SearchMemory,
    SearchRoute,
    SearchSession,
    SearchWork,
)
from mathagent.persistence.workspace_models import Annotation, BlockRevision
from sqlalchemy import select

WARNINGS = [
    "永久清除所选材料的全部版本及系统记录的派生产物；引用保留为不可用占位。",
    "清除本项目运行文本、事件详情和旧导出回执，并停止本项目现有任务；费用占用保留。",
    "单独保存的评测归档、已下载副本、外部备份和未关联的手工复制不在本操作范围内，需另行处理。",
    "这是应用数据删除，不承诺对磁盘、同步服务或备份介质作取证级擦除。",
]


def _budget_only(value):
    """Retain numeric accounting knobs while removing any copied task prose."""
    if isinstance(value, dict):
        return {key: _budget_only(item) for key, item in value.items()
                if isinstance(item, (dict, list, int, float, bool))}
    if isinstance(value, list):
        return [_budget_only(item) for item in value if isinstance(item, (dict, list, int, float, bool))]
    return value if isinstance(value, (int, float, bool)) else None


def _work_budget_only(value):
    if not isinstance(value, dict):
        return {}
    # Work ids and pool live in columns; output capacity is the only snapshot
    # field retained for resource-accounting review.
    cap = value.get("output_cap")
    return {"output_cap": cap} if isinstance(cap, int | float) else {}


class DeletionService:
    def __init__(self, state):
        self.state = state

    def _scope(self, session, object_id):
        root = self.state.require_object(session, object_id)
        revisions = list(session.scalars(select(Revision).join(ResearchObject).where(
            ResearchObject.project_id == root.project_id)))
        by_id = {rev.id: rev for rev in revisions}
        selected = {root.id}
        while True:
            ids = {rev.id for rev in revisions if rev.object_id in selected}
            related = set(session.scalars(select(ResearchRecordReference.record_revision_id).where(
                ResearchRecordReference.target_revision_id.in_(ids))))
            related.update(session.scalars(select(Dependency.plan_revision_id).where(Dependency.revision_id.in_(ids))))
            related.update(session.scalars(select(ProofPlan.revision_id).where(ProofPlan.conclusion_revision_id.in_(ids))))
            # Typed worker outputs and other explicitly linked payloads can copy
            # source material even when they are not proof-plan edges.
            for rev in revisions:
                if rev.object_id not in selected:
                    encoded = json.dumps(rev.payload, ensure_ascii=False)
                    if any(rid in encoded for rid in ids):
                        related.add(rev.id)
            # Route cards may copy deleted proof text in their revision body.
            # Treat all controller references for this project as derived scope.
            roots = set(session.scalars(select(SearchSession.root_run_id).where(
                SearchSession.project_id == root.project_id
            )))
            related.update(session.scalars(select(SearchSession.goal_revision_id).where(
                SearchSession.root_run_id.in_(roots)
            )))
            related.update(session.scalars(select(SearchRoute.card_revision_id).where(
                SearchRoute.root_run_id.in_(roots)
            )))
            related.update(value for value in session.scalars(select(SearchRoute.candidate_revision_id).where(
                SearchRoute.root_run_id.in_(roots)
            )) if value)
            related.update(session.scalars(select(SearchGap.target_revision_id).where(
                SearchGap.root_run_id.in_(roots)
            )))
            related.update(value for value in session.scalars(select(SearchGap.source_revision_id).where(
                SearchGap.root_run_id.in_(roots)
            )) if value)
            related.update(session.scalars(select(SearchMemory.revision_id).where(
                SearchMemory.root_run_id.in_(roots)
            )))
            for memory in session.scalars(select(SearchMemory).where(SearchMemory.root_run_id.in_(roots))):
                related.update(memory.dependency_snapshot.values())
            expanded = selected | {by_id[rid].object_id for rid in related if rid in by_id}
            if expanded == selected:
                return root.project_id, selected, ids
            selected = expanded

    def preview(self, session, payload):
        project_id, objects, revisions = self._scope(session, payload["object_id"])
        branches = list(session.scalars(select(Branch).where(Branch.project_id == project_id)))
        heads = {branch.id: self.state.read_set(session, branch.id) for branch in branches}
        frozen = json.dumps({"objects": sorted(objects), "revisions": sorted(revisions), "heads": heads}, sort_keys=True)
        return 200, {"project_id": project_id, "object_id": payload["object_id"],
                     "object_count": len(objects), "revision_count": len(revisions),
                     "object_ids": sorted(objects), "preview_token": hashlib.sha256(frozen.encode()).hexdigest(),
                     "warnings": WARNINGS}

    def erase(self, session, payload):
        if payload.get("confirmation") != "永久删除":
            raise DomainError(422, "deletion_confirmation_required", "须明确输入永久删除。")
        _, preview = self.preview(session, payload)
        if payload.get("preview_token") != preview["preview_token"]:
            raise DomainError(409, "stale_deletion_preview", "材料范围已变化；请重新预览。")
        project_id, objects, revision_ids = self._scope(session, payload["object_id"])
        branches = list(session.scalars(select(Branch).where(Branch.project_id == project_id)))
        branch_ids = {branch.id for branch in branches}
        project_objects = set(session.scalars(select(ResearchObject.id).where(ResearchObject.project_id == project_id)))
        runs = list(session.scalars(select(Run).where(Run.branch_id.in_(branch_ids))))
        run_ids = {run.id for run in runs}
        attempts = list(session.scalars(select(Attempt).where(Attempt.run_id.in_(run_ids))))
        attempt_ids = {attempt.id for attempt in attempts}
        revisions = list(session.scalars(select(Revision).where(Revision.id.in_(revision_ids))))
        artifacts = collect_references(rev.payload for rev in revisions)
        deleted_at = now()
        for revision in revisions:
            revision.body = "此材料已永久删除；其证据不可取得。"
            revision.payload = {"deleted": True, "deleted_at": deleted_at}
        for plan in session.scalars(select(ProofPlan).where(ProofPlan.revision_id.in_(revision_ids))):
            plan.gaps = ["材料已永久删除。"]
        for review in session.scalars(select(Review).where(Review.project_id == project_id)):
            if review.target_revision_id in revision_ids or set(review.dependency_snapshot.values()) & revision_ids:
                review.verdict, review.coverage, review.scope, review.findings = "inconclusive", "partial", "证据已永久删除。", []
        for adoption in session.scalars(select(Adoption).where(Adoption.revision_id.in_(revision_ids))):
            adoption.state, adoption.reason = "withdrawn", "材料已永久删除。"
        for annotation in session.scalars(select(Annotation).where(Annotation.revision_id.in_(revision_ids))):
            annotation.body, annotation.anchor_quote = "已删除", None
        for conflict in session.scalars(select(Conflict).where(Conflict.object_id.in_(objects))):
            conflict.reason = "材料已永久删除。"
        blocks = list(session.scalars(select(ManuscriptBlock).where(ManuscriptBlock.revision_id.in_(revision_ids))))
        for block in blocks:
            block.body = ""
        for history in session.scalars(select(BlockRevision).where(BlockRevision.block_id.in_([b.id for b in blocks]))):
            history.previous_body = history.body = ""
        # Keep idempotency keys as denied receipts: replaying an old command must
        # not recreate text after its previous response has been erased.
        markers = project_objects | branch_ids | run_ids | attempt_ids | {project_id}
        for receipt in session.scalars(select(CommandReceipt)):
            if receipt.operation == "material.permanent_delete":
                continue  # Retain only erasure manifests so interrupted cleanup can be retried.
            encoded = json.dumps(receipt.response, ensure_ascii=False)
            if any(marker in encoded for marker in markers):
                receipt.status_code = 410
                receipt.response = {"error": "material_deleted", "message": "原回执已随材料永久清除。"}
        for event in session.scalars(select(Event).where(Event.project_id == project_id)):
            event.payload = {"material_erased": True}
        for run in runs:
            run.instruction, run.state = "", "cancelled"
            run.control_epoch += 1
        for branch in branches:
            branch.control_epoch += 1
            settings = session.get(BranchRuntime, branch.id)
            if settings:
                settings.instruction, settings.state = "", "cancelled"
        for attempt in attempts:
            attempt.state = "cancelled"
            attempt.checkpoint = {"material_erased": True,
                "continued_unknown_request_ids": attempt.checkpoint.get("continued_unknown_request_ids", [])}
        for call in session.scalars(select(ProviderCall).where(ProviderCall.attempt_id.in_(attempt_ids))):
            call.raw_text, call.result = "", None
            call.raw_sha256 = hashlib.sha256(b"").hexdigest()
        for step in session.scalars(select(AgentStep).where(AgentStep.run_id.in_(run_ids))):
            step.body, step.actions, step.receipt = "", [], {"material_erased": True}
        roots = set(session.scalars(select(SearchSession.root_run_id).where(
            SearchSession.project_id == project_id
        )))
        for search in session.scalars(select(SearchSession).where(SearchSession.root_run_id.in_(roots))):
            search.config = _budget_only(search.config)
            search.phase, search.selected_route_id = "terminated", None
        for route in session.scalars(select(SearchRoute).where(SearchRoute.root_run_id.in_(roots))):
            route.progress, route.candidate_revision_id, route.state = {}, None, "closed"
        for gap in session.scalars(select(SearchGap).where(SearchGap.root_run_id.in_(roots))):
            gap.details, gap.state = {}, "closed"
        for memory in session.scalars(select(SearchMemory).where(SearchMemory.root_run_id.in_(roots))):
            memory.dependency_snapshot, memory.assumptions, memory.state = {}, [], "deleted"
            memory.source_event = f"erased:{memory.id}"
        for work in session.scalars(select(SearchWork).where(SearchWork.root_run_id.in_(roots))):
            work.input_snapshot, work.state = _work_budget_only(work.input_snapshot), "cancelled"
        for decision in session.scalars(select(SearchDecision).where(SearchDecision.root_run_id.in_(roots))):
            decision.details = {}
        for request in session.scalars(select(ProviderRequest).where(ProviderRequest.project_id == project_id)):
            if request.state == "reserved":
                request.state = "released"
            elif request.state == "dispatched":
                request.state = "released" if request.provider == "fake" else "unknown"
            request.reason = "material_erased_accounting_retained"
        self.state.emit(session, project_id, None, "material.permanently_deleted", {
            "object_ids": sorted(objects), "revision_count": len(revision_ids), "external_copies_affected": False})
        return 200, {"deleted": True, "deleted_object_ids": sorted(objects), "project_id": project_id,
                     "artifact_candidates": artifacts, "storage_cleanup_pending": True,
                     "external_copies_affected": False, "warnings": WARNINGS}

    def cleanup(self, response):
        """Remove only verified, now-unreferenced content-addressed local files."""
        store = ArtifactStore(self.state.db.path)
        with self.state.db.sessions() as session:
            referenced = collect_references(session.scalars(select(Revision.payload)))
        pending = False
        # ArtifactStore is shared by databases in one directory. Never erase a
        # file still referenced by another local database using that store.
        shared_store_uncertain = False
        for peer in self.state.db.path.parent.iterdir():
            if peer == self.state.db.path or peer.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
                continue
            try:
                with sqlite3.connect(peer.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
                    referenced.update(database_references(connection))
            except (sqlite3.Error, ValueError, OSError):
                shared_store_uncertain = True
        from mathagent.tools.code_sandbox import CodeSandbox
        try:
            pending |= bool(CodeSandbox(self.state.db.path).erase_project(response["project_id"])["pending"])
        except OSError:
            pending = True
        for name, metadata in response.get("artifact_candidates", {}).items():
            if name in referenced:
                continue
            if shared_store_uncertain:
                pending = True
                continue
            try:
                path = store.directory / (metadata["sha256"] + ".json")
                store._directory()
                path.lstat()  # distinguish an already removed file from an unsafe store
                store.read(metadata)  # validates root, symlinks, exact size/hash
                path.unlink()
            except FileNotFoundError:
                continue
            except Exception:
                pending = True
        with self.state.db.engine.connect() as connection:
            try:
                busy, _, _ = connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
                pending |= bool(busy)
            except Exception:
                pending = True
        return {**response, "storage_cleanup_pending": pending}
