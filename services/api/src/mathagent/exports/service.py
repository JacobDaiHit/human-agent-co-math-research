"""Snapshot-bound, self-contained research working-draft exports.

Export records live in command receipts, so creation and its read snapshot commit
together. Downloads rebuild bytes from that receipt, never from today's branch.
"""

import base64
import hashlib
import io
import json
import zipfile

from mathagent.application.errors import DomainError
from mathagent.application.state import record
from mathagent.persistence.agent_models import AgentRun, AgentStep, BranchRuntime, ProviderCall
from mathagent.persistence.artifacts import (
    ArtifactError,
    ArtifactStore,
    collect_references,
    validate_content,
)
from mathagent.persistence.models import (
    Adoption,
    Attempt,
    CommandReceipt,
    Dependency,
    Event,
    Project,
    ProofPlan,
    Review,
    Revision,
    RevisionParent,
    Run,
    now,
    uid,
)
from mathagent.persistence.research_models import ResearchRecordReference
from mathagent.persistence.runtime_models import ProviderRequest, RunOptions
from mathagent.persistence.search_models import (
    SearchDecision,
    SearchGap,
    SearchMemory,
    SearchRoute,
    SearchSession,
    SearchWork,
)
from mathagent.persistence.workspace_models import Annotation, ConflictResolution
from sqlalchemy import or_, select

FORMAT_VERSION = 1
SECRET_KEYS = {
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "worker_token",
    "session_token",
    "password",
    "secret",
    "client_secret",
}


def redact_credentials(value):
    """Remove credential fields in stored machine metadata before portability."""
    if isinstance(value, dict):
        return {
            key: redact_credentials(item)
            for key, item in value.items()
            if key.lower().replace("-", "_") not in SECRET_KEYS
            and not key.lower().replace("-", "_").endswith(("_api_key", "_access_token"))
        }
    if isinstance(value, list):
        return [redact_credentials(item) for item in value]
    return value


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class ExportService:
    def __init__(self, service):
        self.service = service

    def _runtime_snapshot(self, session, snapshot):
        """Freeze the related task family and its actual, version-bound operations."""
        runs = {run["id"]: run for run in snapshot["runs"]}
        roots = {
            config.root_run_id
            for run_id in runs
            if (config := session.get(AgentRun, run_id)) is not None
        }
        if roots:
            for run in session.scalars(
                select(Run)
                .join(AgentRun, AgentRun.run_id == Run.id)
                .where(AgentRun.root_run_id.in_(roots))
                .order_by(Run.created_at, Run.id)
            ):
                branch = self.service.require_branch(session, run.branch_id)
                if branch.project_id != snapshot["project"]["id"]:
                    raise DomainError(422, "cross_project_reference", "任务族引用越过项目边界。")
                if run.id not in runs:
                    runs[run.id] = record(run)
                    heads = self.service.read_set(session, branch.id)
                    attempts = []
                    for row in session.scalars(
                        select(Attempt).where(Attempt.run_id == run.id).order_by(Attempt.number)
                    ):
                        item = record(row)
                        item.pop("token", None)
                        item["stale_inputs"] = [
                            oid for oid, rid in row.read_set.items() if heads.get(oid) != rid
                        ]
                        attempts.append(item)
                    runs[run.id]["attempts"] = attempts
        required = set()
        branches = {snapshot["branch"]["id"]}
        for run_id, run in runs.items():
            branches.add(run["branch_id"])
            goal_head = self.service.read_set(session, run["branch_id"]).get(run["goal_object_id"])
            if goal_head:
                required.add(goal_head)
            config = session.get(AgentRun, run_id)
            run["agent"] = record(config) if config else None
            if config and config.target_revision_id:
                required.add(config.target_revision_id)
            options = session.get(RunOptions, run_id)
            run["options"] = record(options) if options else None
            run["provider_requests"] = [
                record(row)
                for row in session.scalars(
                    select(ProviderRequest)
                    .where(ProviderRequest.run_id == run_id)
                    .order_by(ProviderRequest.created_at, ProviderRequest.id)
                )
            ]
            run["provider_calls"] = [
                record(row)
                for row in session.scalars(
                    select(ProviderCall)
                    .join(Attempt, Attempt.id == ProviderCall.attempt_id)
                    .where(Attempt.run_id == run_id)
                    .order_by(ProviderCall.created_at, ProviderCall.request_id)
                )
            ]
            steps = session.scalars(
                select(AgentStep)
                .where(AgentStep.run_id == run_id)
                .order_by(AgentStep.number, AgentStep.id)
            ).all()
            run["steps"] = [record(step) for step in steps]
            for attempt in run["attempts"]:
                required.update(attempt["read_set"].values())
                checkpoint = attempt["checkpoint"]
                required.update(checkpoint.get("context_revision_ids", []))
                for rid in (
                    attempt.get("output_revision_id"),
                    checkpoint.get("target_revision_id"),
                ):
                    if rid:
                        required.add(rid)
                for item in checkpoint.get("external_read_set", []):
                    required.add(item["revision_id"])
            for step in steps:
                if step.output_revision_id:
                    required.add(step.output_revision_id)
                for action in step.actions:
                    if action.get("status") != "completed":
                        continue
                    result = action.get("result", {})
                    # These are validated service receipts, not model proposals.
                    # Do not recursively treat arbitrary draft payloads as links.
                    for item in [result, *result.get("items", [])]:
                        for key in (
                            "revision_id",
                            "previous_revision_id",
                            "candidate_revision_id",
                            "current_revision_id",
                            "target_revision_id",
                            "output_revision_id",
                        ):
                            if item.get(key):
                                required.add(item[key])
                        required.update(item.get("read_set_mismatches", {}).values())
        snapshot["runs"] = list(runs.values())
        snapshot["run_scope"] = "selected_branch_and_related_task_families"
        snapshot["runtime_branches"] = []
        for branch_id in sorted(branches):
            branch = self.service.require_branch(session, branch_id)
            settings = session.get(BranchRuntime, branch_id)
            snapshot["runtime_branches"].append(
                {"branch": record(branch), "settings": record(settings) if settings else None}
            )
        return required

    @staticmethod
    def _snapshot_revision_ids(value, key=""):
        if isinstance(value, dict):
            if key in {"read_set", "dependency_snapshot"}:
                return {item for item in value.values() if isinstance(item, str)}
            found = set()
            for name, item in value.items():
                found.update(ExportService._snapshot_revision_ids(item, name))
            return found
        if isinstance(value, list):
            return set().union(*(ExportService._snapshot_revision_ids(item, key) for item in value))
        if isinstance(value, str) and (key.endswith("revision_id") or key.endswith("revision_ids")):
            return {value}
        return set()

    def _search_snapshot(self, session, project_id):
        """Freeze controller indexes with their exact revision-reference closure."""
        required, sessions = set(), []
        for search in session.scalars(select(SearchSession).where(
            SearchSession.project_id == project_id
        ).order_by(SearchSession.created_at, SearchSession.root_run_id)):
            root = session.get(Run, search.root_run_id)
            if root is None or self.service.require_branch(session, root.branch_id).project_id != project_id:
                raise DomainError(422, "cross_project_reference", "搜索会话引用越过项目边界。")
            item = {"session": record(search), "routes": [], "work": [], "gaps": [],
                    "memory": [], "decisions": []}
            required.add(search.goal_revision_id)
            for route in session.scalars(select(SearchRoute).where(
                SearchRoute.root_run_id == search.root_run_id
            ).order_by(SearchRoute.ordinal, SearchRoute.id)):
                item["routes"].append(record(route))
                required.add(route.card_revision_id)
                if route.candidate_revision_id:
                    required.add(route.candidate_revision_id)
            for work in session.scalars(select(SearchWork).where(
                SearchWork.root_run_id == search.root_run_id
            ).order_by(SearchWork.created_at, SearchWork.id)):
                row = record(work)
                item["work"].append(row)
                required.update(self._snapshot_revision_ids(row["input_snapshot"]))
            for gap in session.scalars(select(SearchGap).where(
                SearchGap.root_run_id == search.root_run_id
            ).order_by(SearchGap.id)):
                item["gaps"].append(record(gap))
                required.add(gap.target_revision_id)
                if gap.source_revision_id:
                    required.add(gap.source_revision_id)
                required.update(self._snapshot_revision_ids(gap.details))
            for memory in session.scalars(select(SearchMemory).where(
                SearchMemory.root_run_id == search.root_run_id
            ).order_by(SearchMemory.id)):
                row = record(memory)
                item["memory"].append(row)
                required.add(memory.revision_id)
                required.update(memory.dependency_snapshot.values())
                required.update(memory.assumptions)
            for decision in session.scalars(select(SearchDecision).where(
                SearchDecision.root_run_id == search.root_run_id
            ).order_by(SearchDecision.sequence, SearchDecision.id)):
                row = record(decision)
                item["decisions"].append(row)
                required.update(self._snapshot_revision_ids(row["details"]))
            sessions.append(item)
        return required, {"scope": "all_project_search_sessions_at_snapshot", "sessions": sessions}

    def create(self, session, payload):
        project = session.get(Project, payload["project_id"])
        if project is None:
            raise DomainError(404, "project_not_found", "研究项目不存在。")
        branch = self.service.require_branch(session, payload["branch_id"])
        if branch.project_id != project.id:
            raise DomainError(422, "wrong_project_branch", "分支不属于该项目。")
        # The enclosing command uses BEGIN IMMEDIATE: every following read has
        # the same committed project/event watermark, including old references.
        snapshot = self.service.snapshot(session, branch.id)
        heads = self.service.read_set(session, branch.id)
        requested = payload.get("object_ids")
        selected = set(heads if requested is None else requested)
        if not selected.issubset(heads):
            raise DomainError(422, "object_outside_branch", "选择的对象必须位于导出分支。")
        explicit = sorted(selected)
        selected.add(project.original_goal_id)
        # A selected refutation/rewrite must carry the question it changes, so a
        # local result cannot silently become an unrelated positive conclusion.
        for relation in snapshot["relations"]:
            if relation["kind"] in {"refutes", "rewrites"} and relation["source_id"] in selected:
                selected.add(relation["target_id"])
        # Context is cheap and prevents a local result silently losing notation
        # or an ambient assumption when exported on its own.
        selected.update(o["id"] for o in snapshot["objects"] if o["kind"] == "context")
        required = {heads[oid] for oid in selected if oid in heads}
        for plan in snapshot["proof_plans"]:
            conclusion = self.service.require_revision(session, plan["conclusion_revision_id"])
            if conclusion.object_id in selected:
                required.add(plan["revision_id"])
        # Preserve the actual original question even when its current head was edited.
        required.update(
            session.scalars(
                select(Revision.id).where(Revision.object_id == project.original_goal_id)
            )
        )
        required.update(self._runtime_snapshot(session, snapshot))
        search_required, snapshot["search_state"] = self._search_snapshot(session, project.id)
        required.update(search_required)
        for conflict in snapshot["conflicts"]:
            required.update(
                conflict[key]
                for key in ("expected_revision_id", "current_revision_id", "candidate_revision_id")
            )
            resolution = session.get(ConflictResolution, conflict["id"])
            if resolution:
                conflict["resolution"] = record(resolution)
                required.update(
                    (
                        resolution.selected_revision_id,
                        resolution.previous_revision_id,
                        resolution.revision_id,
                    )
                )
        if requested is None:
            required.update(b["revision_id"] for b in snapshot["manuscript"] if b["revision_id"])
        annotations = [
            record(item)
            for item in session.scalars(select(Annotation).where(Annotation.branch_id == branch.id))
        ]
        for annotation in annotations:
            rev = self.service.require_revision(session, annotation["revision_id"])
            if requested is None or rev.object_id in selected:
                required.add(rev.id)

        revisions, objects, plans, reviews = {}, {}, {}, {}
        while required - revisions.keys():
            rid = sorted(required - revisions.keys())[0]
            rev = self.service.require_revision(session, rid)
            obj = self.service.require_object(session, rev.object_id)
            if obj.project_id != project.id:
                raise DomainError(422, "cross_project_reference", "导出引用越过项目边界。")
            parents = list(
                session.scalars(
                    select(RevisionParent.parent_id).where(RevisionParent.revision_id == rid)
                )
            )
            required.update(parents)
            record_references = [
                record(reference)
                for reference in session.scalars(
                    select(ResearchRecordReference).where(
                        ResearchRecordReference.record_revision_id == rid
                    )
                )
            ]
            required.update(reference["target_revision_id"] for reference in record_references)
            if obj.id in heads:
                required.add(heads[obj.id])
            revisions[rid] = {
                **record(rev),
                "parent_revision_ids": parents,
                "is_current": heads.get(obj.id) == rid,
                "research_record_references": record_references,
            }
            objects[obj.id] = record(obj)
            plan = session.get(ProofPlan, rid)
            if plan:
                deps = [
                    record(dep)
                    for dep in session.scalars(
                        select(Dependency).where(Dependency.plan_revision_id == rid)
                    )
                ]
                plans[rid] = {**record(plan), "dependencies": deps}
                required.add(plan.conclusion_revision_id)
                required.update(dep["revision_id"] for dep in deps)
            for review in session.scalars(select(Review).where(Review.target_revision_id == rid)):
                reviews[review.id] = record(review)
                required.update(review.dependency_snapshot.values())

        snapshot["objects"] = [o for o in snapshot["objects"] if o["id"] in objects]
        snapshot["manuscript"] = [
            b
            for b in snapshot["manuscript"]
            if b["revision_id"] in revisions or (requested is None and b["kind"] == "text")
        ]
        snapshot["relations"] = [
            r
            for r in snapshot["relations"]
            if r["source_id"] in objects and r["target_id"] in objects
        ]
        snapshot["proof_plans"] = [
            p for p in snapshot["proof_plans"] if p["revision_id"] in revisions
        ]
        snapshot["reviews"] = list(reviews.values())
        snapshot["annotations"] = [a for a in annotations if a["revision_id"] in revisions]
        snapshot["adoptions"] = [
            record(a)
            for a in session.scalars(
                select(Adoption)
                .where(Adoption.branch_id == branch.id, Adoption.revision_id.in_(revisions))
                .order_by(Adoption.event_seq)
            )
        ]
        snapshot["events"] = [
            record(e)
            for e in session.scalars(
                select(Event)
                .where(
                    Event.project_id == project.id,
                    or_(
                        Event.branch_id == branch.id,
                        Event.payload["run_id"]
                        .as_string()
                        .in_([r["id"] for r in snapshot["runs"]]),
                    ),
                    Event.seq <= project.event_seq,
                )
                .order_by(Event.seq)
            )
        ]
        # Full-branch support is kept explicitly as context; it is not recomputed
        # against a subset, which could silently change the research conclusion.
        snapshot["support_scope"] = "full_branch_at_snapshot"
        artifact_files = {}
        try:
            references = collect_references(rev["payload"] for rev in revisions.values())
            store = ArtifactStore(self.service.db.path)
            for name, metadata in references.items():
                data = store.read(metadata)
                decoded = json.loads(data)
                if redact_credentials(decoded) != decoded:
                    raise ArtifactError("artifact_contains_credentials")
                artifact_files[name] = {
                    **metadata,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }
        except ArtifactError as error:
            raise DomainError(
                409, "artifact_export_failed", "产物文件缺失、校验失败或不适合导出。"
            ) from error
        export_id = uid()
        bundle = redact_credentials(
            {
                "format": "mathagent.research-export",
                "format_version": FORMAT_VERSION,
                "export_id": export_id,
                "created_at": now(),
                "mode": "research_working_draft",
                "is_truth_verdict": False,
                "limitation": "研究工作稿；不是经保证的论文、一般证明或形式化验证。",
                "snapshot_seq": project.event_seq,
                "requested_object_ids": explicit,
                "included_object_ids": sorted(objects),
                "snapshot": snapshot,
                "objects": [objects[key] for key in sorted(objects)],
                "revisions": [revisions[key] for key in sorted(revisions)],
                "proof_plans": [plans[key] for key in sorted(plans)],
                "reviews": [reviews[key] for key in sorted(reviews)],
                "artifact_files": artifact_files,
            }
        )
        return 201, {
            "export_id": export_id,
            "download_url": f"/exports/{export_id}/download",
            "snapshot_seq": project.event_seq,
            "project_id": project.id,
            "branch_id": branch.id,
            "included_object_count": len(objects),
            "included_revision_count": len(revisions),
            "bundle": bundle,
        }

    def get(self, export_id):
        with self.service.db.sessions() as session:
            receipt = session.scalar(
                select(CommandReceipt).where(
                    CommandReceipt.operation == "export.create",
                    CommandReceipt.response["export_id"].as_string() == export_id,
                )
            )
            if receipt is None:
                raise DomainError(404, "export_not_found", "导出快照不存在。")
            return receipt.response["bundle"]


def markdown(bundle):
    snapshot = bundle["snapshot"]
    lines = [
        f"# {snapshot['project']['title']} · 研究工作稿",
        "",
        bundle["limitation"],
        "",
        f"项目 `{snapshot['project']['id']}` · 分支 `{snapshot['branch']['id']}` "
        f"({snapshot['branch']['name']}) · 事件快照 `{bundle['snapshot_seq']}`",
        "",
        f"原始目标对象：`{snapshot['project']['original_goal_id']}`。原问题及历史版本见下文。",
        "",
        "证据支持与人工采用分别记录；条件假设、异议、缺口以及历史材料不会自动消失。",
        "",
        "## 工作稿",
        "",
    ]
    for block in snapshot["manuscript"]:
        if block["revision_id"]:
            lines.extend([f"引用版本 `{block['revision_id']}`", ""])
        lines.extend([block["body"], ""])
    objects = {obj["id"]: obj for obj in bundle["objects"]}
    lines.extend(["## 对象与精确版本（包含依赖和历史）", ""])
    for rev in bundle["revisions"]:
        obj = objects[rev["object_id"]]
        status = "快照当前版本" if rev["is_current"] else "历史或候选版本"
        lines.extend(
            [
                f"### {obj['kind']} · {obj['id']}",
                "",
                f"版本 `{rev['id']}` · {status} · 作者 {rev['author']} · {rev['created_at']}",
                "",
                rev["body"],
                "",
            ]
        )
        if rev["payload"]:
            lines.extend(
                [
                    "来源、条件、失败或产物元数据：",
                    "",
                    "```json",
                    json_bytes(rev["payload"]).decode().rstrip(),
                    "```",
                    "",
                ]
            )
    for title, key in (
        ("论证与未解除假设、缺口", "proof_plans"),
        ("证据类型、范围、来源与固定版本", "reviews"),
    ):
        lines.extend(
            [f"## {title}", "", "```json", json_bytes(bundle[key]).decode().rstrip(), "```", ""]
        )
    for title, key in (
        ("支持状态（整个分支快照，不是数学真值）", "support"),
        ("冲突与争议", "conflicts"),
        ("人工批注与局部异议", "annotations"),
        ("采用历史及理由（不代表数学真值）", "adoptions"),
        ("研究关系", "relations"),
        ("运行与尝试清单", "runs"),
        ("分支事件与失败过程", "events"),
    ):
        lines.extend(
            [f"## {title}", "", "```json", json_bytes(snapshot[key]).decode().rstrip(), "```", ""]
        )
    if "runtime_branches" in snapshot:
        lines.extend(
            [
                "## 相关任务族的分支控制与额度快照",
                "",
                "```json",
                json_bytes(snapshot["runtime_branches"]).decode().rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(lines).encode("utf-8")


def export_zip(bundle):
    draft = markdown(bundle)
    contents = {"research.md": draft}
    try:
        references = collect_references(rev["payload"] for rev in bundle["revisions"])
        frozen = bundle.get("artifact_files", {})
        if set(frozen) != set(references):
            raise ArtifactError("artifact_export_incomplete")
        for name, metadata in references.items():
            data = base64.b64decode(frozen[name]["content_base64"], validate=True)
            contents[name] = validate_content(metadata, data)
    except (ArtifactError, ValueError, KeyError, TypeError) as error:
        raise DomainError(409, "artifact_export_failed", "导出快照的产物文件校验失败。") from error
    manifest = {
        **{key: value for key, value in bundle.items() if key != "artifact_files"},
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data in contents.items()
        },
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # Fixed entry metadata also makes repeated downloads byte-identical.
        for name, content in (*contents.items(), ("manifest.json", json_bytes(manifest))):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return stream.getvalue()
