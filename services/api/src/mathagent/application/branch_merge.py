"""Explicit, version-fenced branch merge planning for mathematical workspaces."""

import hashlib
import json

from mathagent.application.errors import DomainError
from mathagent.application.research_records import save_record_references, validate_record_payload
from mathagent.persistence.models import (
    Attempt,
    Dependency,
    Head,
    ManuscriptBlock,
    ProofPlan,
    Revision,
    RevisionParent,
    Run,
)
from sqlalchemy import select


class BranchMergeService:
    """Plan and apply only human-selected branch-head changes.

    Text equality is deliberately not a merge criterion: mathematical equivalence,
    definitions and assumptions require a human resolution.
    """

    def __init__(self, state):
        self.state = state

    @staticmethod
    def _parents(session, revision_id):
        return list(session.scalars(select(RevisionParent.parent_id).where(
            RevisionParent.revision_id == revision_id)))

    def _ancestors(self, session, revision_id):
        distances, pending = {revision_id: 0}, [revision_id]
        while pending:
            current = pending.pop(0)
            for parent in self._parents(session, current):
                if parent not in distances:
                    distances[parent] = distances[current] + 1
                    pending.append(parent)
        return distances

    def _common_ancestor(self, session, left, right):
        left_ancestors, right_ancestors = self._ancestors(session, left), self._ancestors(session, right)
        shared = set(left_ancestors) & set(right_ancestors)
        return min(shared, key=lambda item: (left_ancestors[item] + right_ancestors[item], item)) if shared else None

    @staticmethod
    def _token(data):
        source = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(source.encode()).hexdigest()

    def _branches(self, session, payload):
        source = self.state.require_branch(session, payload["source_branch_id"])
        target = self.state.require_branch(session, payload["target_branch_id"])
        if source.id == target.id:
            raise DomainError(422, "merge_same_branch", "源分支和目标分支必须不同。")
        if source.project_id != target.project_id:
            raise DomainError(422, "merge_cross_project", "只能合并同一项目的分支。")
        return source, target

    def preview(self, session, payload):
        source, target = self._branches(session, payload)
        source_heads, target_heads = self.state.read_set(session, source.id), self.state.read_set(session, target.id)
        items = []
        for object_id in sorted(set(source_heads) | set(target_heads)):
            source_revision_id, target_revision_id = source_heads.get(object_id), target_heads.get(object_id)
            common = self._common_ancestor(session, source_revision_id, target_revision_id) if source_revision_id and target_revision_id else None
            if source_revision_id == target_revision_id:
                status = "unchanged"
            elif source_revision_id is None:
                status = "target_only"
            elif target_revision_id is None:
                status = "source_only_requires_resolution"
            elif common == target_revision_id:
                status = "fast_forward_candidate_requires_confirmation"
            elif common == source_revision_id:
                status = "target_ahead_requires_confirmation"
            else:
                status = "diverged_requires_resolution"
            source_revision = session.get(Revision, source_revision_id) if source_revision_id else None
            target_revision = session.get(Revision, target_revision_id) if target_revision_id else None
            ancestor_revision = session.get(Revision, common) if common else None
            affected = list(session.scalars(select(Dependency.plan_revision_id).where(
                Dependency.revision_id == target_revision_id))) if target_revision_id else []
            items.append({
                "object_id": object_id, "kind": self.state.require_object(session, object_id).kind,
                "source_revision_id": source_revision_id, "target_revision_id": target_revision_id,
                "common_ancestor_revision_id": common, "status": status,
                "semantic_review_required": source_revision_id != target_revision_id,
                "semantic_reason": "数学等价、定义和假设不能由文本差异自动判定。" if source_revision_id != target_revision_id else None,
                "affected_plan_revision_ids": sorted(set(affected)),
                "source_body": source_revision.body if source_revision else None,
                "target_body": target_revision.body if target_revision else None,
                "ancestor_body": ancestor_revision.body if ancestor_revision else None,
                "source_payload": source_revision.payload if source_revision else None,
            })
        frozen = {"source_branch_id": source.id, "target_branch_id": target.id,
                  "source_heads": source_heads, "target_heads": target_heads,
                  "source_epoch": source.control_epoch, "target_epoch": target.control_epoch}
        return 200, {"source_branch_id": source.id, "target_branch_id": target.id, "items": items,
                     "frozen_heads": frozen, "preview_token": self._token(frozen)}

    def apply(self, session, payload):
        source, target = self._branches(session, payload)
        _, preview = self.preview(session, {"source_branch_id": source.id, "target_branch_id": target.id})
        if payload.get("preview_token") != preview["preview_token"]:
            raise DomainError(409, "stale_merge_preview", "分支版本已变化；请重新预览合并。")
        items = {item["object_id"]: item for item in preview["items"]}
        raw_resolutions = payload.get("resolutions", [])
        resolutions = {entry.get("object_id"): entry for entry in raw_resolutions}
        if len(resolutions) != len(raw_resolutions):
            raise DomainError(422, "duplicate_merge_resolution", "同一对象不能提交重复合并决议。")
        required = {oid for oid, item in items.items() if item["status"] not in {"unchanged", "target_only"}}
        if set(resolutions) != required:
            raise DomainError(422, "merge_resolutions_required", "每个源侧变化都必须给出明确人工决议。")
        planned = {oid: (items[oid]["target_revision_id"] if resolution.get("choice") == "target"
                         else items[oid]["source_revision_id"])
                   for oid, resolution in resolutions.items()}
        for object_id, resolution in resolutions.items():
            if resolution.get("choice") not in {"source", "rewrite"}:
                continue
            source_id = items[object_id]["source_revision_id"]
            self.state.check_revision(session, source, source_id)
            plan = session.get(ProofPlan, source_id)
            if not plan:
                continue
            dependency_ids = [plan.conclusion_revision_id, *session.scalars(
                select(Dependency.revision_id).where(Dependency.plan_revision_id == source_id))]
            for dependency_id in dependency_ids:
                dependency_revision = self.state.require_revision(session, dependency_id)
                if resolutions.get(dependency_revision.object_id, {}).get("choice") == "rewrite":
                    raise DomainError(422, "merge_dependency_resolution_required",
                                      "依赖正在改写；请先合并依赖，再重建证明对新版本的引用。")
                selected = planned.get(dependency_revision.object_id)
                current = self.state.read_set(session, target.id).get(dependency_revision.object_id)
                if (selected or current) != dependency_id:
                    raise DomainError(422, "merge_dependency_resolution_required",
                                      "导入证明的依赖必须在目标分支显式选择同一固定版本。")
        changes, stale_runs = [], []
        for object_id in sorted(required):
            item, resolution = items[object_id], resolutions[object_id]
            choice = resolution.get("choice")
            if choice not in {"source", "target", "rewrite"} or not str(resolution.get("reason", "")).strip():
                raise DomainError(422, "invalid_merge_resolution", "决议必须选择 source、target 或 rewrite，并说明理由。")
            old = item["target_revision_id"]
            selected = old if choice == "target" else item["source_revision_id"]
            if choice == "rewrite":
                if not isinstance(resolution.get("body"), str) or not resolution["body"].strip():
                    raise DomainError(422, "merge_rewrite_body_required", "改写合并必须提供正文。")
                source_revision = self.state.require_revision(session, item["source_revision_id"])
                revised_payload = source_revision.payload if resolution.get("payload") is None else resolution["payload"]
                self.state.validate_payload(item["kind"], revised_payload)
                revised_payload = validate_record_payload(item["kind"], revised_payload, session, target)
                revision = Revision(object_id=object_id, body=resolution["body"], payload=revised_payload,
                                    author="human")
                session.add(revision)
                session.flush()
                save_record_references(session, revision.id, revised_payload)
                for parent_id in {item["source_revision_id"], old} - {None}:
                    session.add(RevisionParent(revision_id=revision.id, parent_id=parent_id))
                plan = session.get(ProofPlan, source_revision.id)
                if plan:
                    session.add(ProofPlan(revision_id=revision.id, conclusion_revision_id=plan.conclusion_revision_id,
                                          rule=plan.rule, gaps=[*plan.gaps, "merge rewrite requires recheck"]))
                    for dependency in session.scalars(select(Dependency).where(Dependency.plan_revision_id == source_revision.id)):
                        session.add(Dependency(plan_revision_id=revision.id, revision_id=dependency.revision_id,
                                               role=dependency.role, origin=dependency.origin, scope=dependency.scope))
                selected = revision.id
            if selected != old:
                head = session.get(Head, (target.id, object_id))
                if head:
                    head.revision_id = selected
                    for block in session.scalars(select(ManuscriptBlock).where(
                        ManuscriptBlock.branch_id == target.id, ManuscriptBlock.revision_id == old
                    )):
                        block.revision_id = selected
                else:
                    session.add(Head(branch_id=target.id, object_id=object_id, revision_id=selected))
                    self.state._add_reference(session, target.id, selected)
                for run in session.scalars(select(Run).where(Run.branch_id == target.id)):
                    attempt = session.get(Attempt, run.current_attempt_id) if run.current_attempt_id else None
                    if attempt and attempt.read_set.get(object_id) == old:
                        stale_runs.append(run.id)
                changes.append({"object_id": object_id, "previous_revision_id": old,
                                "revision_id": selected, "choice": choice,
                                "reason": resolution["reason"].strip(),
                                "review_or_adoption_migrated": False})
        session.flush()
        for change in changes:
            revision = self.state.require_revision(session, change["revision_id"])
            kind = self.state.require_object(session, revision.object_id).kind
            validate_record_payload(kind, revision.payload, session, target)
        target.control_epoch += 1
        self.state.emit(session, target.project_id, target.id, "branch.merge_applied", {
            "source_branch_id": source.id, "target_branch_id": target.id, "changes": changes,
            "stale_run_ids": sorted(set(stale_runs)), "reviews_migrated": False, "adoptions_migrated": False,
        })
        return 201, {"source_branch_id": source.id, "target_branch_id": target.id, "changes": changes,
                     "stale_run_ids": sorted(set(stale_runs)), "reviews_migrated": False,
                     "adoptions_migrated": False}
