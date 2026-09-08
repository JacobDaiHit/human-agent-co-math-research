"""Transactional commands shared by people and worker adapters."""

import hashlib
import json

from mathagent.application.errors import DomainError
from mathagent.domain.support import analyze_support
from mathagent.persistence.models import (
    Adoption,
    Attempt,
    Branch,
    CommandReceipt,
    Conflict,
    Dependency,
    Event,
    Head,
    ManuscriptBlock,
    Project,
    ProofPlan,
    Relation,
    ResearchObject,
    Review,
    Revision,
    RevisionParent,
    Run,
)
from sqlalchemy import select, text


def record(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


class StateService:
    def __init__(self, database):
        self.db = database

    def execute(self, operation, key, payload, handler):
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.db.sessions.begin() as session:
            # SQLite writer serialization also protects read/check/write and event sequence allocation.
            session.execute(text("BEGIN IMMEDIATE"))
            old = session.get(CommandReceipt, key)
            if old:
                if old.operation != operation or old.digest != digest:
                    raise DomainError(409, "idempotency_mismatch", "此幂等键已用于另一项命令。")
                return old.status_code, old.response
            status, response = handler(session, payload)
            session.add(
                CommandReceipt(
                    key=key,
                    operation=operation,
                    digest=digest,
                    status_code=status,
                    response=response,
                )
            )
            return status, response

    def require_branch(self, s, branch_id):
        branch = s.get(Branch, branch_id)
        if not branch:
            raise DomainError(404, "branch_not_found", "研究分支不存在。")
        return branch

    def require_object(self, s, object_id):
        obj = s.get(ResearchObject, object_id)
        if not obj:
            raise DomainError(404, "object_not_found", "研究对象不存在。")
        return obj

    def require_revision(self, s, revision_id):
        revision = s.get(Revision, revision_id)
        if not revision:
            raise DomainError(404, "revision_not_found", "对象版本不存在。")
        return revision

    def check_revision(self, s, branch, revision_id, current=False):
        rev = self.require_revision(s, revision_id)
        obj = self.require_object(s, rev.object_id)
        if obj.project_id != branch.project_id:
            raise DomainError(422, "cross_project_reference", "不能引用其他项目的版本。")
        head = s.get(Head, (branch.id, obj.id))
        if not head:
            raise DomainError(422, "object_outside_branch", "该对象不在当前分支。")
        if current and head.revision_id != rev.id:
            raise DomainError(
                409,
                "stale_reference",
                "引用版本已不是当前分支版本。",
                current_revision_id=head.revision_id,
            )
        return rev, obj

    def emit(self, s, project_id, branch_id, event_type, payload, author="human"):
        project = s.get(Project, project_id)
        project.event_seq += 1
        event = Event(
            project_id=project_id,
            seq=project.event_seq,
            branch_id=branch_id,
            type=event_type,
            payload=payload,
            author=author,
        )
        s.add(event)
        s.flush()
        return event

    def read_set(self, s, branch_id):
        return {
            h.object_id: h.revision_id
            for h in s.scalars(select(Head).where(Head.branch_id == branch_id))
        }

    def new_object(self, s, branch, kind, body, payload, author):
        from mathagent.application.research_records import (
            save_record_references,
            validate_record_payload,
        )

        self.validate_payload(kind, payload)
        payload = validate_record_payload(kind, payload, s, branch)
        obj = ResearchObject(project_id=branch.project_id, kind=kind, author=author)
        s.add(obj)
        s.flush()
        rev = Revision(object_id=obj.id, body=body, payload=payload, author=author)
        s.add(rev)
        s.flush()
        save_record_references(s, rev.id, payload)
        s.add(Head(branch_id=branch.id, object_id=obj.id, revision_id=rev.id))
        s.flush()
        return obj, rev

    @staticmethod
    def validate_payload(kind, payload):
        if kind == "context":
            role = payload.get("role", "assumption")
            if not isinstance(role, str) or role not in {"definition", "assumption", "convention"}:
                raise DomainError(
                    422,
                    "invalid_context_role",
                    "上下文 role 只能是 definition、assumption 或 convention。",
                )

    def create_project(self, s, p):
        project = Project(
            title=p["title"],
            policies={
                "research_freedom": {"create_branches": True},
                "resources": {"provider": "fake", "max_parallel": 2},
                "adoption": {"support_policy": "reviewed_actual_proof", "is_truth_verdict": False},
            },
        )
        s.add(project)
        s.flush()
        branch = Branch(project_id=project.id, name="main")
        s.add(branch)
        s.flush()
        obj, rev = self.new_object(s, branch, "problem", p["body"], {}, "human")
        project.original_goal_id = obj.id
        self._add_reference(s, branch.id, rev.id)
        self.emit(
            s,
            project.id,
            branch.id,
            "project.created",
            {"object_id": obj.id, "revision_id": rev.id},
        )
        return 201, {
            "project_id": project.id,
            "branch_id": branch.id,
            "object_id": obj.id,
            "revision_id": rev.id,
        }

    def create_object(self, s, p, *, author="human"):
        branch = self.require_branch(s, p["branch_id"])
        obj, rev = self.new_object(s, branch, p["kind"], p["body"], p.get("payload", {}), author)
        self._add_reference(s, branch.id, rev.id)
        self.emit(
            s,
            branch.project_id,
            branch.id,
            "object.created",
            {"object_id": obj.id, "revision_id": rev.id},
            author,
        )
        return 201, {"object_id": obj.id, "revision_id": rev.id}

    def _add_reference(self, s, branch_id, revision_id):
        positions = list(
            s.scalars(
                select(ManuscriptBlock.position).where(ManuscriptBlock.branch_id == branch_id)
            )
        )
        s.add(
            ManuscriptBlock(
                branch_id=branch_id,
                position=max(positions, default=-1) + 1,
                kind="reference",
                revision_id=revision_id,
                body="",
            )
        )
        s.flush()

    def revise_object(self, s, p, *, author="human"):
        from mathagent.application.research_records import (
            save_record_references,
            validate_record_payload,
        )

        branch = self.require_branch(s, p["branch_id"])
        obj = self.require_object(s, p["object_id"])
        expected, expected_obj = self.check_revision(s, branch, p["expected_revision_id"])
        if expected_obj.id != obj.id:
            raise DomainError(422, "wrong_revision_object", "预期版本不属于被编辑的对象。")
        revised_payload = expected.payload if p.get("payload") is None else p["payload"]
        previous_type = expected.payload.get("artifact_type")
        if (
            previous_type in ("failure", "source")
            and revised_payload.get("artifact_type") != previous_type
        ):
            raise DomainError(
                422,
                "research_record_type_changed",
                "来源或失败记录的新版本不能删除或更换其记录类型。",
            )
        self.validate_payload(obj.kind, revised_payload)
        revised_payload = validate_record_payload(obj.kind, revised_payload, s, branch)
        head = s.get(Head, (branch.id, obj.id))
        before = self.snapshot(s, branch.id)
        mismatches = {}
        for oid, rid in p.get("read_set", {}).items():
            rev, dep_obj = self.check_revision(s, branch, rid)
            if dep_obj.id != oid:
                raise DomainError(422, "invalid_read_set", "版本读集的对象与版本不匹配。")
            current = s.get(Head, (branch.id, oid)).revision_id
            if current != rev.id:
                mismatches[oid] = current
        candidate = Revision(
            object_id=obj.id, body=p["body"], payload=revised_payload, author=author
        )
        s.add(candidate)
        s.flush()
        save_record_references(s, candidate.id, revised_payload)
        s.add(RevisionParent(revision_id=candidate.id, parent_id=expected.id))
        # Copy proof structure into a new version; old reviews intentionally stay behind.
        old_plan = s.get(ProofPlan, expected.id)
        if old_plan:
            s.add(
                ProofPlan(
                    revision_id=candidate.id,
                    conclusion_revision_id=old_plan.conclusion_revision_id,
                    gaps=old_plan.gaps,
                    rule=old_plan.rule,
                )
            )
            s.flush()
            for dep in s.scalars(
                select(Dependency).where(Dependency.plan_revision_id == expected.id)
            ):
                s.add(
                    Dependency(
                        plan_revision_id=candidate.id,
                        revision_id=dep.revision_id,
                        role=dep.role,
                        origin=dep.origin,
                        scope=dep.scope,
                    )
                )
        if head.revision_id != expected.id or mismatches:
            conflict = Conflict(
                branch_id=branch.id,
                object_id=obj.id,
                expected_revision_id=expected.id,
                current_revision_id=head.revision_id,
                candidate_revision_id=candidate.id,
                reason="输入版本已变化；候选已保存，未覆盖分支。",
            )
            s.add(conflict)
            s.flush()
            result = {
                "error": "revision_conflict",
                "message": conflict.reason,
                "conflict_id": conflict.id,
                "candidate_revision_id": candidate.id,
                "current_revision_id": head.revision_id,
                "read_set_mismatches": mismatches,
            }
            self.emit(s, branch.project_id, branch.id, "revision.conflict", result, author)
            return 409, result
        head.revision_id = candidate.id
        updated_blocks = []
        for block in s.scalars(
            select(ManuscriptBlock).where(
                ManuscriptBlock.branch_id == branch.id, ManuscriptBlock.revision_id == expected.id
            )
        ):
            block.revision_id = candidate.id
            updated_blocks.append(block.id)
        s.flush()
        after = self.snapshot(s, branch.id)
        affected = []
        for plan in after["proof_plans"]:
            refs = (
                plan["premise_revision_ids"]
                + plan["context_revision_ids"]
                + plan["assumption_revision_ids"]
                + [plan["conclusion_revision_id"]]
            )
            pid = plan["revision_id"]
            old_status = before["support"]["plans"].get(pid, {}).get("status")
            new_status = after["support"]["plans"].get(pid, {}).get("status")
            if expected.id in refs or (
                old_status in {"supported", "conditional"} and new_status != old_status
            ):
                affected.append(pid)
        stale_runs = []
        for run in s.scalars(select(Run).where(Run.branch_id == branch.id)):
            attempt = s.get(Attempt, run.current_attempt_id) if run.current_attempt_id else None
            if attempt and run.state in {"running", "pause_requested", "cancel_requested"}:
                if attempt.read_set.get(obj.id) == expected.id:
                    stale_runs.append(run.id)
        result = {
            "object_id": obj.id,
            "revision_id": candidate.id,
            "previous_revision_id": expected.id,
            "affected_plan_revision_ids": affected,
            "stale_run_ids": stale_runs,
            "updated_block_ids": updated_blocks,
        }
        self.emit(s, branch.project_id, branch.id, "revision.created", result, author)
        return 201, result

    def clone_branch(self, s, source, name):
        if s.scalar(
            select(Branch.id).where(Branch.project_id == source.project_id, Branch.name == name)
        ):
            raise DomainError(409, "branch_name_exists", "分支名称已存在。")
        target = Branch(project_id=source.project_id, name=name, parent_id=source.id)
        s.add(target)
        s.flush()
        for head in s.scalars(select(Head).where(Head.branch_id == source.id)):
            s.add(Head(branch_id=target.id, object_id=head.object_id, revision_id=head.revision_id))
        for adoption in s.scalars(select(Adoption).where(Adoption.branch_id == source.id)):
            s.add(
                Adoption(
                    branch_id=target.id,
                    revision_id=adoption.revision_id,
                    state=adoption.state,
                    reason=adoption.reason,
                    author=adoption.author,
                    event_seq=adoption.event_seq,
                )
            )
        for rel in s.scalars(select(Relation).where(Relation.branch_id == source.id)):
            s.add(
                Relation(
                    branch_id=target.id,
                    source_id=rel.source_id,
                    target_id=rel.target_id,
                    kind=rel.kind,
                )
            )
        for block in s.scalars(
            select(ManuscriptBlock).where(ManuscriptBlock.branch_id == source.id)
        ):
            s.add(
                ManuscriptBlock(
                    branch_id=target.id,
                    position=block.position,
                    kind=block.kind,
                    body=block.body,
                    revision_id=block.revision_id,
                )
            )
        s.flush()
        return target

    def create_branch(self, s, p, *, author="human"):
        source = self.require_branch(s, p["source_branch_id"])
        target = self.clone_branch(s, source, p["name"])
        self.emit(
            s,
            target.project_id,
            target.id,
            "branch.created",
            {"source_branch_id": source.id},
            author,
        )
        return 201, {"branch_id": target.id}

    def create_proof(self, s, p, *, author="human"):
        branch = self.require_branch(s, p["branch_id"])
        _, conclusion = self.check_revision(s, branch, p["conclusion_revision_id"])
        if conclusion.kind != "claim":
            raise DomainError(422, "invalid_conclusion", "证明方案的结论必须是命题版本。")
        for role in ("premise", "context", "assumption"):
            for rid in p[f"{role}_revision_ids"]:
                _, obj = self.check_revision(s, branch, rid)
                allowed = {"context"} if role == "context" else {"claim", "context"}
                if obj.kind not in allowed:
                    raise DomainError(
                        422,
                        "invalid_dependency",
                        "前提必须是命题或上下文，不能引用活动日志作为证明。",
                    )
        obj, rev = self.new_object(s, branch, "argument", p["body"], {}, author)
        s.add(
            ProofPlan(
                revision_id=rev.id,
                conclusion_revision_id=p["conclusion_revision_id"],
                gaps=p["gaps"],
                rule=p["rule"],
            )
        )
        s.flush()
        for role in ("premise", "context", "assumption"):
            for rid in dict.fromkeys(p[f"{role}_revision_ids"]):
                s.add(Dependency(plan_revision_id=rev.id, revision_id=rid, role=role))
        self._add_reference(s, branch.id, rev.id)
        self.emit(
            s,
            branch.project_id,
            branch.id,
            "proof.created",
            {"object_id": obj.id, "revision_id": rev.id},
            author,
        )
        return 201, {"object_id": obj.id, "revision_id": rev.id}

    def add_review(self, s, p):
        branch = self.require_branch(s, p["branch_id"])
        revision, obj = self.check_revision(s, branch, p["target_revision_id"])
        if p["kind"] == "exact_computation" and p["coverage"] == "whole_plan":
            raise DomainError(
                422,
                "exact_tool_unavailable",
                "尚未接入可信计算执行器；手工计算记录只能作为局部观察，完整论证请另作人工审查。",
            )
        if p["kind"] == "formal_check":
            raise DomainError(
                422, "formal_tool_unavailable", "尚未接入形式化检查器，不能手工制造形式化通过记录。"
            )
        if p["kind"] == "llm_review":
            raise DomainError(
                422,
                "model_review_unavailable",
                "模型审查须由已认证 worker 的实际审查任务生成，不能通过人工录入接口伪造。",
            )
        refs = {obj.id: revision.id}
        plan = s.get(ProofPlan, revision.id)
        if plan:
            for dep in s.scalars(
                select(Dependency).where(Dependency.plan_revision_id == revision.id)
            ):
                dependency = self.require_revision(s, dep.revision_id)
                refs[dependency.object_id] = dependency.id
            conclusion = self.require_revision(s, plan.conclusion_revision_id)
            refs[conclusion.object_id] = conclusion.id
        row = Review(
            project_id=branch.project_id,
            target_revision_id=revision.id,
            kind=p["kind"],
            verdict=p["verdict"],
            coverage=p["coverage"],
            scope=p["scope"],
            findings=p["findings"],
            dependency_snapshot=refs,
            author="human",
        )
        s.add(row)
        s.flush()
        self.emit(
            s,
            branch.project_id,
            branch.id,
            "review.recorded",
            {"review_id": row.id, "target_revision_id": revision.id},
        )
        return 201, {
            "review_id": row.id,
            "provenance": "human_entered",
            "target_revision_id": revision.id,
        }

    def adopt(self, s, p):
        branch = self.require_branch(s, p["branch_id"])
        self.check_revision(s, branch, p["revision_id"])
        event = self.emit(
            s,
            branch.project_id,
            branch.id,
            "adoption.changed",
            {"revision_id": p["revision_id"], "state": p["state"], "reason": p["reason"]},
        )
        row = Adoption(
            branch_id=branch.id,
            revision_id=p["revision_id"],
            state=p["state"],
            reason=p["reason"],
            author="human",
            event_seq=event.seq,
        )
        s.add(row)
        s.flush()
        return 201, {
            "adoption_id": row.id,
            "revision_id": row.revision_id,
            "state": row.state,
            "is_truth_verdict": False,
        }

    def add_relation(self, s, p):
        branch = self.require_branch(s, p["branch_id"])
        for oid in (p["source_id"], p["target_id"]):
            if not s.get(Head, (branch.id, oid)):
                raise DomainError(422, "object_outside_branch", "关系两端必须属于当前分支。")
        rel = Relation(**p)
        s.add(rel)
        s.flush()
        self.emit(s, branch.project_id, branch.id, "relation.created", {"relation_id": rel.id})
        return 201, {"relation_id": rel.id, "is_proof": False}

    def add_block(self, s, p):
        branch = self.require_branch(s, p["branch_id"])
        if p["kind"] == "reference":
            if not p.get("revision_id"):
                raise DomainError(422, "missing_revision", "引用块必须指定版本。")
            self.check_revision(s, branch, p["revision_id"])
        elif p.get("revision_id"):
            raise DomainError(422, "invalid_text_block", "自由文本块不携带对象版本。")
        positions = list(
            s.scalars(
                select(ManuscriptBlock.position).where(ManuscriptBlock.branch_id == branch.id)
            )
        )
        block = ManuscriptBlock(**p, position=max(positions, default=-1) + 1)
        s.add(block)
        s.flush()
        self.emit(s, branch.project_id, branch.id, "manuscript.block_added", {"block_id": block.id})
        return 201, {"block_id": block.id}

    def snapshot(self, s, branch_id):
        branch = self.require_branch(s, branch_id)
        project = s.get(Project, branch.project_id)
        heads = self.read_set(s, branch.id)
        objects = {
            o.id: o
            for o in s.scalars(
                select(ResearchObject).where(ResearchObject.project_id == project.id)
            )
        }
        revisions = list(s.scalars(select(Revision).where(Revision.object_id.in_(heads))))
        adoptions = {}
        for a in s.scalars(
            select(Adoption).where(Adoption.branch_id == branch.id).order_by(Adoption.event_seq)
        ):
            adoptions[a.revision_id] = record(a)
        visible_revision_ids = {rev.id for rev in revisions}
        reviews = [
            record(r)
            for r in s.scalars(
                select(Review).where(Review.target_revision_id.in_(visible_revision_ids))
            )
        ]
        nodes = {}
        for rev in revisions:
            obj = objects[rev.object_id]
            nodes[rev.id] = {
                "object_id": obj.id,
                "kind": obj.kind,
                "body": rev.body,
                "payload": rev.payload,
                "is_current": heads.get(obj.id) == rev.id,
                "adoption_state": adoptions.get(rev.id, {}).get("state", "draft"),
                "evidence": [r for r in reviews if r["target_revision_id"] == rev.id],
            }
        selected_revisions = set(heads.values())
        plans = []
        for plan in s.scalars(
            select(ProofPlan).where(ProofPlan.revision_id.in_(selected_revisions))
        ):
            deps = list(
                s.scalars(select(Dependency).where(Dependency.plan_revision_id == plan.revision_id))
            )
            plans.append(
                {
                    **record(plan),
                    "body": nodes[plan.revision_id]["body"],
                    **{
                        f"{role}_revision_ids": [d.revision_id for d in deps if d.role == role]
                        for role in ("premise", "context", "assumption")
                    },
                }
            )
        support = analyze_support(nodes, plans)
        support["is_truth_verdict"] = False
        support["limitation"] = (
            "只检查已记录依赖与指定证据策略，不自动判断数学真伪或发现全部隐含前提。"
        )
        revision_map = {rev.id: rev for rev in revisions}
        current_objects = []
        for oid, rid in heads.items():
            current_objects.append(
                {
                    **record(objects[oid]),
                    "revision": record(revision_map[rid]),
                    "adoption_state": nodes[rid]["adoption_state"],
                }
            )
        blocks = [
            record(b)
            for b in s.scalars(
                select(ManuscriptBlock)
                .where(ManuscriptBlock.branch_id == branch.id)
                .order_by(ManuscriptBlock.position)
            )
        ]
        for block in blocks:
            if block["revision_id"]:
                block["body"] = revision_map[block["revision_id"]].body
        runs = [record(r) for r in s.scalars(select(Run).where(Run.branch_id == branch.id))]
        for run in runs:
            attempts = [
                record(a)
                for a in s.scalars(
                    select(Attempt).where(Attempt.run_id == run["id"]).order_by(Attempt.number)
                )
            ]
            for attempt in attempts:
                attempt.pop("token", None)
                attempt["stale_inputs"] = [
                    oid for oid, rid in attempt["read_set"].items() if heads.get(oid) != rid
                ]
            run["attempts"] = attempts
        return {
            "project": record(project),
            "branch": record(branch),
            "objects": current_objects,
            "proof_plans": plans,
            "reviews": reviews,
            "support": support,
            "manuscript": blocks,
            "relations": [
                record(r)
                for r in s.scalars(select(Relation).where(Relation.branch_id == branch.id))
            ],
            "runs": runs,
            "conflicts": [
                record(c)
                for c in s.scalars(select(Conflict).where(Conflict.branch_id == branch.id))
            ],
        }

    def get_snapshot(self, project_id, branch_id=None):
        with self.db.sessions.begin() as s:
            s.execute(text("BEGIN"))
            if not s.get(Project, project_id):
                raise DomainError(404, "project_not_found", "研究项目不存在。")
            if branch_id is None:
                branch_id = s.scalar(
                    select(Branch.id).where(Branch.project_id == project_id, Branch.name == "main")
                )
            branch = self.require_branch(s, branch_id)
            if branch.project_id != project_id:
                raise DomainError(422, "wrong_project_branch", "分支不属于该项目。")
            return self.snapshot(s, branch.id)

    def get_events(self, project_id, after_seq=0):
        with self.db.sessions.begin() as s:
            s.execute(text("BEGIN"))
            project = s.get(Project, project_id)
            if not project:
                raise DomainError(404, "project_not_found", "研究项目不存在。")
            events = [
                record(e)
                for e in s.scalars(
                    select(Event)
                    .where(Event.project_id == project_id, Event.seq > after_seq)
                    .order_by(Event.seq)
                    .limit(1000)
                )
            ]
            return {
                "events": events,
                "last_seq": events[-1]["seq"] if events else after_seq,
                "project_seq": project.event_seq,
            }

    def get_revisions(self, object_id):
        with self.db.sessions() as s:
            self.require_object(s, object_id)
            revisions = [
                record(r)
                for r in s.scalars(
                    select(Revision)
                    .where(Revision.object_id == object_id)
                    .order_by(Revision.created_at, Revision.id)
                )
            ]
            for rev in revisions:
                rev["parent_revision_ids"] = list(
                    s.scalars(
                        select(RevisionParent.parent_id).where(
                            RevisionParent.revision_id == rev["id"]
                        )
                    )
                )
            return {"revisions": revisions}

    def list_projects(self):
        with self.db.sessions() as s:
            return {
                "projects": [
                    record(p) for p in s.scalars(select(Project).order_by(Project.created_at))
                ]
            }
