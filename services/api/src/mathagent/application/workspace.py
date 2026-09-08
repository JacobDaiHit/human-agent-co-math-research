"""Transactional human workspace commands and revision-aware presentation queries."""

from mathagent.application.errors import DomainError
from mathagent.application.research_records import (
    ResearchRecordsService,
    save_record_references,
    validate_record_payload,
)
from mathagent.application.state import record
from mathagent.persistence.models import (
    Adoption,
    Attempt,
    Branch,
    Conflict,
    Dependency,
    Head,
    ManuscriptBlock,
    Project,
    ProofPlan,
    ResearchObject,
    Revision,
    RevisionParent,
    Run,
    now,
)
from mathagent.persistence.workspace_models import (
    Annotation,
    BlockRevision,
    BranchLayout,
    ConflictResolution,
)
from sqlalchemy import func, select, text


class WorkspaceService:
    def __init__(self, state):
        self.state = state
        self.db = state.db

    @staticmethod
    def require_project(s, project_id):
        project = s.get(Project, project_id)
        if not project:
            raise DomainError(404, "project_not_found", "研究项目不存在。")
        return project

    @staticmethod
    def require_block(s, block_id):
        block = s.get(ManuscriptBlock, block_id)
        if not block:
            raise DomainError(404, "block_not_found", "工作稿段落不存在。")
        return block

    def list_branches(self, project_id):
        with self.db.sessions() as s:
            self.require_project(s, project_id)
            return {
                "branches": [
                    {**record(b), "presentation": ResearchRecordsService.presentation(s, b.id)}
                    for b in s.scalars(
                        select(Branch)
                        .where(Branch.project_id == project_id)
                        .order_by(Branch.created_at, Branch.id)
                    )
                ]
            }

    @staticmethod
    def layout(s, branch_id):
        layout = s.get(BranchLayout, branch_id)
        return (
            record(layout)
            if layout
            else {
                "branch_id": branch_id,
                "version": 0,
                "positions": {},
                "updated_at": None,
            }
        )

    def get_layout(self, branch_id):
        with self.db.sessions() as s:
            self.state.require_branch(s, branch_id)
            return self.layout(s, branch_id)

    def update_layout(self, s, p):
        branch = self.state.require_branch(s, p["branch_id"])
        layout = s.get(BranchLayout, branch.id)
        version = layout.version if layout else 0
        if version != p["expected_version"]:
            raise DomainError(
                409,
                "layout_conflict",
                "画布布局已变化，请加载后重新拖动。",
                current_version=version,
                layout=self.layout(s, branch.id),
            )
        for object_id in p["positions"]:
            if not s.get(Head, (branch.id, object_id)):
                raise DomainError(422, "object_outside_branch", "布局只能包含当前分支的对象。")
        if not layout:
            layout = BranchLayout(branch_id=branch.id)
            s.add(layout)
        layout.positions = p["positions"]
        layout.version = version + 1
        layout.updated_at = now()
        s.flush()
        self.state.emit(
            s,
            branch.project_id,
            branch.id,
            "workspace.layout_updated",
            {
                "layout_version": layout.version,
            },
        )
        return 200, record(layout)

    def add_annotation(self, s, p):
        branch = self.state.require_branch(s, p["branch_id"])
        rev, _ = self.state.check_revision(s, branch, p["revision_id"])
        if p.get("anchor_quote") and p["anchor_quote"] not in rev.body:
            raise DomainError(422, "anchor_not_found", "批注选段不在目标版本正文中。")
        annotation = Annotation(**p, author="human")
        s.add(annotation)
        s.flush()
        self.state.emit(
            s,
            branch.project_id,
            branch.id,
            "annotation.created",
            {
                "annotation_id": annotation.id,
                "revision_id": rev.id,
            },
        )
        return 201, record(annotation)

    def get_annotations(self, branch_id, revision_id=None):
        with self.db.sessions() as s:
            branch = self.state.require_branch(s, branch_id)
            query = select(Annotation).where(Annotation.branch_id == branch_id)
            if revision_id:
                self.state.check_revision(s, branch, revision_id)
                query = query.where(Annotation.revision_id == revision_id)
            return {
                "annotations": [
                    record(a)
                    for a in s.scalars(query.order_by(Annotation.created_at, Annotation.id))
                ]
            }

    def revise_block(self, s, p):
        branch = self.state.require_branch(s, p["branch_id"])
        block = self.require_block(s, p["block_id"])
        if block.branch_id != branch.id:
            raise DomainError(422, "wrong_block_branch", "段落不属于当前分支。")
        if block.kind != "text":
            raise DomainError(
                422, "reference_block_requires_revision", "引用块请修改其研究对象版本。"
            )
        version = s.scalar(
            select(func.count())
            .select_from(BlockRevision)
            .where(
                BlockRevision.block_id == block.id,
            )
        )
        if version != p["expected_version"] or block.body != p["expected_body"]:
            raise DomainError(
                409,
                "block_conflict",
                "段落正文已变化，请对比当前文本。",
                current_body=block.body,
                current_version=version,
            )
        revision = BlockRevision(block_id=block.id, previous_body=block.body, body=p["body"])
        s.add(revision)
        block.body = p["body"]
        s.flush()
        result = {
            "block_id": block.id,
            "block_revision_id": revision.id,
            "body": block.body,
            "version": version + 1,
        }
        self.state.emit(s, branch.project_id, branch.id, "manuscript.block_revised", result)
        return 201, result

    def get_block_revisions(self, block_id):
        with self.db.sessions.begin() as s:
            s.execute(text("BEGIN"))
            block = self.require_block(s, block_id)
            revisions = [
                record(r)
                for r in s.scalars(
                    select(BlockRevision)
                    .where(BlockRevision.block_id == block_id)
                    .order_by(BlockRevision.created_at, BlockRevision.id)
                )
            ]
            return {"block": {**record(block), "version": len(revisions)}, "revisions": revisions}

    @staticmethod
    def copy_proof(s, source_id, target_id):
        plan = s.get(ProofPlan, source_id)
        if not plan:
            return
        s.add(
            ProofPlan(
                revision_id=target_id,
                conclusion_revision_id=plan.conclusion_revision_id,
                gaps=plan.gaps,
                rule=plan.rule,
            )
        )
        s.flush()
        for dep in s.scalars(select(Dependency).where(Dependency.plan_revision_id == source_id)):
            s.add(
                Dependency(
                    plan_revision_id=target_id,
                    revision_id=dep.revision_id,
                    role=dep.role,
                    origin=dep.origin,
                    scope=dep.scope,
                )
            )

    def resolve_conflict(self, s, p):
        branch = self.state.require_branch(s, p["branch_id"])
        conflict = s.get(Conflict, p["conflict_id"])
        if not conflict:
            raise DomainError(404, "conflict_not_found", "版本冲突不存在。")
        if conflict.branch_id != branch.id:
            raise DomainError(422, "wrong_conflict_branch", "版本冲突不属于当前分支。")
        resolved = s.get(ConflictResolution, conflict.id)
        if resolved:
            raise DomainError(
                409,
                "conflict_already_resolved",
                "此冲突已处理。",
                resolution=record(resolved),
            )
        head = s.get(Head, (branch.id, conflict.object_id))
        if not head or head.revision_id != p["expected_current_revision_id"]:
            raise DomainError(
                409,
                "stale_conflict_resolution",
                "当前版本已再次变化，请重新比较后选择。",
                current_revision_id=head.revision_id if head else None,
            )
        if p["selected_revision_id"] not in {head.revision_id, conflict.candidate_revision_id}:
            raise DomainError(
                422, "invalid_conflict_selection", "只能选择当前版本或此冲突保存的候选。"
            )
        source, obj = self.state.check_revision(s, branch, p["selected_revision_id"])
        if obj.id != conflict.object_id:
            raise DomainError(422, "wrong_revision_object", "所选版本不属于冲突对象。")
        before = self.state.snapshot(s, branch.id)
        previous_id = head.revision_id
        payload = validate_record_payload(obj.kind, source.payload, s, branch)
        revision = Revision(object_id=obj.id, body=source.body, payload=payload, author="human")
        s.add(revision)
        s.flush()
        save_record_references(s, revision.id, payload)
        for parent_id in {previous_id, conflict.candidate_revision_id}:
            s.add(RevisionParent(revision_id=revision.id, parent_id=parent_id))
        self.copy_proof(s, source.id, revision.id)
        head.revision_id = revision.id
        updated_blocks = []
        for block in s.scalars(
            select(ManuscriptBlock).where(
                ManuscriptBlock.branch_id == branch.id,
                ManuscriptBlock.revision_id == previous_id,
            )
        ):
            block.revision_id = revision.id
            updated_blocks.append(block.id)
        resolution = ConflictResolution(
            conflict_id=conflict.id,
            selected_revision_id=source.id,
            previous_revision_id=previous_id,
            revision_id=revision.id,
        )
        s.add(resolution)
        s.flush()
        after = self.state.snapshot(s, branch.id)
        affected = []
        for plan in after["proof_plans"]:
            pid = plan["revision_id"]
            refs = sum(
                (
                    plan[f"{role}_revision_ids"]
                    for role in (
                        "premise",
                        "context",
                        "assumption",
                    )
                ),
                [],
            ) + [plan["conclusion_revision_id"]]
            if previous_id in refs or (
                before["support"]["plans"].get(pid) != after["support"]["plans"].get(pid)
            ):
                affected.append(pid)
        stale_runs = []
        for run in s.scalars(select(Run).where(Run.branch_id == branch.id)):
            attempt = s.get(Attempt, run.current_attempt_id) if run.current_attempt_id else None
            if attempt and run.state in {"running", "pause_requested", "cancel_requested"}:
                if attempt.read_set.get(obj.id) == previous_id:
                    stale_runs.append(run.id)
        result = {
            **record(resolution),
            "object_id": obj.id,
            "parent_revision_ids": sorted({previous_id, conflict.candidate_revision_id}),
            "affected_plan_revision_ids": affected,
            "updated_block_ids": updated_blocks,
            "stale_run_ids": stale_runs,
        }
        self.state.emit(s, branch.project_id, branch.id, "conflict.resolved", result)
        return 201, result

    def get_snapshot(self, project_id, branch_id=None):
        with self.db.sessions.begin() as s:
            s.execute(text("BEGIN"))
            self.require_project(s, project_id)
            if branch_id is None:
                branch_id = s.scalar(
                    select(Branch.id).where(
                        Branch.project_id == project_id,
                        Branch.name == "main",
                    )
                )
            branch = self.state.require_branch(s, branch_id)
            if branch.project_id != project_id:
                raise DomainError(422, "wrong_project_branch", "分支不属于该项目。")
            result = self.state.snapshot(s, branch_id)
            result["layout"] = self.layout(s, branch_id)
            result["presentation"] = ResearchRecordsService.presentation(s, branch_id)
            result["annotations"] = [
                record(a)
                for a in s.scalars(
                    select(Annotation)
                    .where(Annotation.branch_id == branch_id)
                    .order_by(Annotation.created_at, Annotation.id)
                )
            ]
            block_ids = [block["id"] for block in result["manuscript"]]
            result["block_history"] = [
                record(r)
                for r in s.scalars(
                    select(BlockRevision)
                    .where(BlockRevision.block_id.in_(block_ids))
                    .order_by(BlockRevision.created_at, BlockRevision.id)
                )
            ]
            block_versions = {}
            for revision in result["block_history"]:
                bid = revision["block_id"]
                block_versions[bid] = block_versions.get(bid, 0) + 1
            for block in result["manuscript"]:
                if block["kind"] == "text":
                    block["version"] = block_versions.get(block["id"], 0)
            for conflict in result["conflicts"]:
                resolution = s.get(ConflictResolution, conflict["id"])
                conflict["resolution"] = record(resolution) if resolution else None
            return result

    @staticmethod
    def snippet(body, q):
        # Keep complete Markdown/LaTeX delimiters. The UI limits the card height;
        # character slicing here can turn part of a formula into ordinary text.
        return body

    def search(self, project_id, query, branch_id=None, limit=100):
        query = query.strip()
        if not query:
            raise DomainError(422, "empty_search", "请输入要查找的研究内容。")
        with self.db.sessions.begin() as s:
            s.execute(text("BEGIN"))
            self.require_project(s, project_id)
            if branch_id:
                branch = self.state.require_branch(s, branch_id)
                if branch.project_id != project_id:
                    raise DomainError(422, "wrong_project_branch", "分支不属于该项目。")
                branches = [branch]
            else:
                branches = list(
                    s.scalars(
                        select(Branch)
                        .where(Branch.project_id == project_id)
                        .order_by(Branch.created_at, Branch.id)
                    )
                )
            objects = {
                o.id: o
                for o in s.scalars(
                    select(ResearchObject).where(
                        ResearchObject.project_id == project_id,
                    )
                )
            }
            revisions = list(
                s.scalars(
                    select(Revision)
                    .where(Revision.object_id.in_(objects))
                    .order_by(Revision.created_at, Revision.id)
                )
            )
            versions, counts = {}, {}
            for revision in revisions:
                counts[revision.object_id] = counts.get(revision.object_id, 0) + 1
                versions[revision.id] = counts[revision.object_id]
            parents = {}
            for parent in s.scalars(
                select(RevisionParent).where(
                    RevisionParent.revision_id.in_(versions),
                )
            ):
                parents.setdefault(parent.revision_id, []).append(parent.parent_id)
            results = []
            needle = query.casefold()
            for branch in branches:
                snapshot = self.state.snapshot(s, branch.id)
                heads = {obj["id"]: obj["revision"]["id"] for obj in snapshot["objects"]}
                candidates = {
                    conflict["candidate_revision_id"]: conflict["id"]
                    for conflict in snapshot["conflicts"]
                    if not s.get(ConflictResolution, conflict["id"])
                }
                # A project-wide revision list cannot tell which branch selected a version.
                # Follow selected ancestry and explicit references to avoid assigning another
                # branch's revisions to this branch's history.
                visible, pending = set(), list(heads.values()) + list(candidates)
                pending.extend(
                    block["revision_id"] for block in snapshot["manuscript"] if block["revision_id"]
                )
                while pending:
                    revision_id = pending.pop()
                    if revision_id in visible:
                        continue
                    visible.add(revision_id)
                    pending.extend(parents.get(revision_id, []))
                adoptions = {}
                for adoption in s.scalars(
                    select(Adoption)
                    .where(Adoption.branch_id == branch.id)
                    .order_by(Adoption.event_seq)
                ):
                    adoptions[adoption.revision_id] = adoption.state
                for revision in revisions:
                    if revision.id not in visible or needle not in revision.body.casefold():
                        continue
                    current = heads[revision.object_id] == revision.id
                    support = (
                        snapshot["support"]["claims"].get(revision.id)
                        or snapshot["support"]["plans"].get(revision.id)
                        or {}
                    )
                    results.append(
                        {
                            "object_id": revision.object_id,
                            "revision_id": revision.id,
                            "version": versions[revision.id],
                            "kind": objects[revision.object_id].kind,
                            "branch_id": branch.id,
                            "branch_name": branch.name,
                            "is_current": current,
                            "revision_state": "current"
                            if current
                            else (
                                "conflict_candidate" if revision.id in candidates else "historical"
                            ),
                            "conflict_id": candidates.get(revision.id),
                            "status": adoptions.get(revision.id, "draft"),
                            "support_status": support.get("status", "unassessed")
                            if current
                            else "historical",
                            "snippet": self.snippet(revision.body, query),
                            "match_in": "body",
                        }
                    )
                for block in snapshot["manuscript"]:
                    if block["kind"] == "text" and needle in block["body"].casefold():
                        count = s.scalar(
                            select(func.count())
                            .select_from(BlockRevision)
                            .where(
                                BlockRevision.block_id == block["id"],
                            )
                        )
                        results.append(
                            {
                                "block_id": block["id"],
                                "object_id": None,
                                "revision_id": None,
                                "version": count + 1,
                                "kind": "text",
                                "branch_id": branch.id,
                                "branch_name": branch.name,
                                "is_current": True,
                                "status": "draft",
                                "revision_state": "current",
                                "conflict_id": None,
                                "support_status": "unassessed",
                                "snippet": self.snippet(block["body"], query),
                                "match_in": "body",
                            }
                        )
            results.sort(
                key=lambda item: (not item["is_current"], item["branch_name"], -item["version"])
            )
            return {
                "results": results[:limit],
                "total": len(results),
                "query": query,
                "truncated": len(results) > limit,
            }
