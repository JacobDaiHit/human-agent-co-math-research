"""Autonomous research through durable, bounded proposals and immutable attempts."""

import hashlib
import json

from mathagent.application.errors import DomainError
from mathagent.persistence.agent_models import AgentRun, AgentStep, BranchRuntime, ProviderCall
from mathagent.persistence.models import (
    Adoption,
    Attempt,
    Head,
    Project,
    ResearchObject,
    Review,
    Revision,
    Run,
)
from mathagent.persistence.runtime_models import ProviderRequest, RunOptions
from mathagent.providers.actions import OPERATION_MODELS, operation_schemas
from mathagent.providers.protocol import validate_result
from pydantic import ValidationError
from sqlalchemy import func, select

DEFAULT_OPTIONS = {
    "max_steps": 8, "max_review_rounds": 2, "max_children": 4, "max_depth": 2,
    "max_output_tokens": 4096, "request_timeout_seconds": 180,
    "thinking_mode": "provider_default", "reasoning_effort": "provider_default",
}
TERMINAL = {"completed", "cancelled", "failed", "interrupted", "budget_exhausted", "step_limit"}


class AgentRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
        self.state = runtime.service

    def register(self, session, run_id, payload, *, parent=None, target_revision_id=None):
        options = {k: payload.get(k, v) for k, v in DEFAULT_OPTIONS.items()}
        row = AgentRun(
            run_id=run_id, root_run_id=parent.root_run_id if parent else run_id,
            parent_run_id=parent.run_id if parent else None,
            target_revision_id=target_revision_id, depth=parent.depth + 1 if parent else 0,
            autonomous=payload.get("autonomous", False), options=options,
        )
        session.add(row)
        session.flush()
        return row

    def options(self, session, run_id):
        self.runtime._run(session, run_id)
        row = session.get(AgentRun, run_id)
        budget = session.get(RunOptions, run_id)
        return {"run_id": run_id, "request_budget": budget.request_budget if budget else 5,
                "autonomous": bool(row and row.autonomous), **DEFAULT_OPTIONS,
                **(row.options if row else {})}

    def update_options(self, session, payload):
        run = self.runtime._run(session, payload["run_id"])
        row = session.get(AgentRun, run.id) or self.register(session, run.id, payload)
        row.autonomous = payload["autonomous"]
        row.options = {k: payload[k] for k in DEFAULT_OPTIONS}
        session.get(RunOptions, run.id).request_budget = payload["request_budget"]
        self.runtime._emit(session, run, "run.options_changed", self.options(session, run.id), "human")
        return 200, self.options(session, run.id)

    def branch_settings(self, session, branch_id):
        self.state.require_branch(session, branch_id)
        row = session.get(BranchRuntime, branch_id)
        return {"branch_id": branch_id, "request_budget": row.request_budget if row else 100,
                "state": row.state if row else "active"}

    def update_branch_settings(self, session, payload):
        branch = self.state.require_branch(session, payload["branch_id"])
        row = session.get(BranchRuntime, branch.id)
        if not row:
            row = BranchRuntime(branch_id=branch.id, state="active", instruction="")
            session.add(row)
        row.request_budget = payload["request_budget"]
        self.state.emit(session, branch.project_id, branch.id, "branch.budget_changed", payload)
        return 200, self.branch_settings(session, branch.id)

    def branch_allowed(self, session, run):
        from mathagent.persistence.research_models import BranchPresentation

        config = session.get(AgentRun, run.id)
        root = session.get(Run, config.root_run_id) if config else run
        for branch_id in {run.branch_id, root.branch_id}:
            settings = self.branch_settings(session, branch_id)
            display = session.get(BranchPresentation, branch_id)
            if settings["state"] != "active" or (display and display.archived):
                raise DomainError(403, "branch_not_active", "此研究范围已暂停、停止或归档")

    def intervene_branch(self, session, payload):
        branch = self.state.require_branch(session, payload["branch_id"])
        row = session.get(BranchRuntime, branch.id)
        if not row:
            row = BranchRuntime(branch_id=branch.id, request_budget=100, instruction="")
            session.add(row)
        action = payload["action"]
        row.state = {"pause": "paused", "cancel": "cancelled", "resume": "active"}.get(action, row.state or "active")
        if action == "steer":
            if not payload.get("instruction", "").strip():
                raise DomainError(422, "instruction_required", "引导需要具体指令")
            row.instruction = payload["instruction"]
        roots = select(Run.id).where(Run.branch_id == branch.id)
        descendants = select(AgentRun.run_id).where(AgentRun.root_run_id.in_(roots))
        affected = []
        for run in session.scalars(select(Run).where((Run.branch_id == branch.id) | Run.id.in_(descendants))):
            if run.state in {"completed", "cancelled"}:
                continue
            if action == "resume":
                if run.state in {"paused", "interrupted", "budget_exhausted"}:
                    _, response = self.runtime.resume(session, {"run_id": run.id})
                    affected.append(response)
            else:
                _, response = self.runtime.intervene(session, {"run_id": run.id, "action": action,
                                                               "instruction": payload.get("instruction", "")})
                affected.append(response)
        session.flush()
        response = {**self.branch_settings(session, branch.id), "affected_runs": affected,
                    "effect": "pending" if any(r.get("effect") == "pending" for r in affected) else "applied"}
        self.state.emit(session, branch.project_id, branch.id, "branch.intervention", response)
        return 200, response

    def subtree_ids(self, session, run_id):
        found = {run_id}
        while True:
            children = set(session.scalars(select(AgentRun.run_id).where(AgentRun.parent_run_id.in_(found))))
            if children <= found:
                return found
            found.update(children)

    def additional_budget_exhausted(self, session, run, requests):
        row = session.get(AgentRun, run.id)
        root = session.get(Run, row.root_run_id) if row else run
        for branch_id in {run.branch_id, root.branch_id}:
            branch_roots = set(session.scalars(select(Run.id).where(Run.branch_id == branch_id)))
            branch_ids = branch_roots | set(session.scalars(select(AgentRun.run_id).where(AgentRun.root_run_id.in_(branch_roots))))
            limit = self.branch_settings(session, branch_id)["request_budget"]
            if self.runtime._counts([r for r in requests if r.run_id in branch_ids])["occupied"] >= limit:
                return True
        while row:
            family = self.subtree_ids(session, row.run_id)
            options = session.get(RunOptions, row.run_id)
            if self.runtime._counts([r for r in requests if r.run_id in family])["occupied"] >= options.request_budget:
                return True
            row = session.get(AgentRun, row.parent_run_id) if row.parent_run_id else None
        return False

    def initial_read_set(self, session, run, all_heads):
        from mathagent.persistence.models import ProofPlan

        config = session.get(AgentRun, run.id)
        if not config or (not config.autonomous and not config.target_revision_id):
            return all_heads
        if config.target_revision_id:
            all_heads = {**all_heads, run.goal_object_id: config.target_revision_id}
        project = session.get(Project, self.state.require_branch(session, run.branch_id).project_id)
        chosen = {run.goal_object_id, project.original_goal_id}
        for obj in session.scalars(select(ResearchObject).where(ResearchObject.id.in_(all_heads))):
            if obj.kind == "context":
                chosen.add(obj.id)
        for plan in session.scalars(select(ProofPlan).where(
                ProofPlan.conclusion_revision_id == all_heads[run.goal_object_id])):
            rev = self.state.require_revision(session, plan.revision_id)
            if all_heads.get(rev.object_id) == rev.id:
                chosen.add(rev.object_id)
        # Previously read objects contribute their latest selected version, even after edits.
        for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)):
            for action in step.actions:
                value = action.get("result", {})
                for item in [value, *value.get("items", [])]:
                    if item.get("revision_id"):
                        rev = session.get(Revision, item["revision_id"])
                        if rev and rev.object_id in all_heads:
                            chosen.add(rev.object_id)
        return {oid: rid for oid, rid in all_heads.items() if oid in chosen}

    def _input(self, session, branch, revision_id):
        from mathagent.persistence.models import Dependency, ProofPlan
        from mathagent.runtime.context import read_ref

        rev, obj = self.state.check_revision(session, branch, revision_id)
        head = session.get(Head, (branch.id, obj.id))
        adoption = session.scalar(select(Adoption).where(
            Adoption.branch_id == branch.id, Adoption.revision_id == rev.id).order_by(Adoption.event_seq.desc()))
        reviews = session.scalars(select(Review).where(Review.target_revision_id == rev.id).order_by(Review.id)).all()
        item = {"object_id": obj.id, "revision_id": rev.id, "branch_id": branch.id,
                "kind": obj.kind, "body": rev.body, "payload": rev.payload,
                "current": bool(head and head.revision_id == rev.id),
                "current_revision_id": head.revision_id if head else None,
                "adoption_state": adoption.state if adoption else "draft",
                "evidence": [{"id": r.id, "kind": r.kind, "verdict": r.verdict,
                              "coverage": r.coverage, "scope": r.scope, "findings": r.findings,
                              "dependency_snapshot": r.dependency_snapshot, "author": r.author,
                              "current": bool(head and head.revision_id == rev.id) and all((h := session.get(Head, (branch.id, oid))) is not None and h.revision_id == rid
                                             for oid, rid in r.dependency_snapshot.items())} for r in reviews]}
        plan = session.get(ProofPlan, rev.id)
        if plan:
            item["proof_plan"] = {"revision_id": rev.id, "conclusion_revision_id": plan.conclusion_revision_id,
                                  "rule": plan.rule, "gaps": plan.gaps,
                                  "dependencies": [{"revision_id": dep.revision_id, "role": dep.role,
                                                    "origin": dep.origin, "scope": dep.scope}
                                      for dep in session.scalars(select(Dependency).where(
                                          Dependency.plan_revision_id == rev.id).order_by(Dependency.revision_id, Dependency.role))]}
        item["operation_receipts"] = [{"run_id": step.run_id, "number": step.number,
                                       "actions": step.actions, "receipt": step.receipt}
            for step in session.scalars(select(AgentStep).where(AgentStep.output_revision_id == rev.id).order_by(AgentStep.number))]
        item["read_ref"] = read_ref(item)
        return item

    def enrich_task(self, session, task):
        import copy

        from mathagent.runtime.context import compact_task, read_ref

        options = self.options(session, task["run_id"])
        task.update(options)
        branch = self.state.require_branch(session, self.runtime._run(session, task["run_id"]).branch_id)
        permitted = session.get(Project, branch.project_id).policies.get("agent_operations", list(operation_schemas()))
        task["operation_schemas"] = {k: v for k, v in operation_schemas().items() if k in permitted} if options["autonomous"] else {}
        task["branch_id"], task["project_id"] = branch.id, branch.project_id
        snapshot = self.state.snapshot(session, branch.id)
        for item in task["inputs"]:
            item.update(self._input(session, branch, item["revision_id"]))
            item["support"] = snapshot["support"]["claims"].get(item["revision_id"], snapshot["support"]["plans"].get(item["revision_id"], {}))
            if task["mode"] == "review":
                item["evidence"] = [e for e in item["evidence"] if e["kind"] != "llm_review"]
                item.pop("support", None)
                # Prior operation receipts may contain a previous model verdict too.
                item.pop("operation_receipts", None)
        for plan in task.get("proof_plans", []):
            rev = session.get(Revision, plan["revision_id"])
            plan.update(object_id=rev.object_id, branch_id=branch.id)
            plan["read_ref"] = read_ref(plan)
        if not options["autonomous"]:
            return compact_task(task)
        rows = session.scalars(select(AgentStep).where(AgentStep.run_id == task["run_id"]).order_by(AgentStep.number)).all()
        extra_ids = [r.output_revision_id for r in rows if r.output_revision_id]
        external_heads = {}
        present = {(i["branch_id"], i["revision_id"]) for i in task["inputs"]}

        def refresh_result(value):
            """Copy a historical receipt; never rewrite the saved operation."""
            value = copy.deepcopy(value)
            for item in [value, *value.get("items", [])]:
                rid = item.get("revision_id")
                rev = session.get(Revision, rid) if rid else None
                if not rev:
                    continue
                obj = session.get(ResearchObject, rev.object_id)
                if obj.project_id != branch.project_id:
                    continue
                extra_ids.append(rid)
                source_id = item.get("branch_id") or branch.id
                source = self.state.require_branch(session, source_id)
                if source.project_id != branch.project_id:
                    continue
                head = session.get(Head, (source.id, rev.object_id))
                if not head:
                    continue
                item.update(object_id=rev.object_id, branch_id=source.id,
                            current=head.revision_id == rid, stale=head.revision_id != rid,
                            current_revision_id=head.revision_id)
                if "is_current" in item:
                    item["is_current"] = item["current"]
                prior_context = item.get("read_ref", {}).get("context_attempt_id")
                item["read_ref"] = read_ref(item, item.get("section", "record"))
                if prior_context:
                    item["read_ref"]["context_attempt_id"] = prior_context
                if not item["current"]:
                    item["current_read_ref"] = {**read_ref(item), "revision_id": head.revision_id}
                    if isinstance(item.get("evidence"), list):
                        item["evidence"] = [{**e, "current": False} for e in item["evidence"]]
                if source.id != branch.id:
                    external_heads[(source.id, rev.object_id)] = {
                        "branch_id": source.id, "object_id": rev.object_id, "revision_id": head.revision_id}
                    if (source.id, head.revision_id) not in present:
                        task["inputs"].append(self._input(session, source, head.revision_id))
                        present.add((source.id, head.revision_id))
                    extra_ids.append(head.revision_id)
            return value

        refreshed = []
        for step in rows:
            actions = [{**a, "result": refresh_result(a["result"])} if "result" in a else copy.deepcopy(a)
                       for a in step.actions]
            output = session.get(Revision, step.output_revision_id) if step.output_revision_id else None
            item = {"number": step.number, "body": step.body, "actions": actions,
                    "output_revision_id": step.output_revision_id}
            if output:
                item.update(object_id=output.object_id, revision_id=output.id, branch_id=branch.id)
                item["read_ref"] = read_ref(item)
            refreshed.append(item)
        task["previous_steps"] = refreshed[-6:]
        task["remaining_steps"] = max(0, options["max_steps"] - len(rows))
        task["operation_results"] = refreshed[-1]["actions"] if refreshed else []
        config = session.get(AgentRun, task["run_id"])
        children = session.scalars(select(AgentRun).where(AgentRun.parent_run_id == config.run_id)).all()
        child_results = []
        for child in children:
            run = session.get(Run, child.run_id)
            attempt = session.get(Attempt, run.current_attempt_id) if run.current_attempt_id else None
            output = session.get(Revision, attempt.output_revision_id) if attempt and attempt.output_revision_id else None
            item = {"run_id": run.id, "state": run.state, "mode": session.get(RunOptions, run.id).mode,
                    "target_revision_id": child.target_revision_id, "output_revision_id": output.id if output else None,
                    "body": output.body if output else None}
            if output:
                item.update(object_id=output.object_id, revision_id=output.id, branch_id=run.branch_id)
                item["read_ref"] = read_ref(item)
                task["context_revision_ids"].append(output.id)
            child_results.append(item)
        task["child_results"] = child_results
        discussions = []
        for rev in session.scalars(select(Revision).join(ResearchObject).where(
                ResearchObject.project_id == branch.project_id, ResearchObject.kind == "activity").order_by(Revision.created_at.desc())):
            if rev.payload.get("artifact_type") != "discussion":
                continue
            recipient = rev.payload.get("recipient_run_id")
            in_branch = session.get(Head, (branch.id, rev.object_id))
            if recipient == task["run_id"] or (recipient is None and in_branch):
                source = in_branch or session.scalar(select(Head).where(Head.object_id == rev.object_id).order_by(Head.branch_id))
                item = {"object_id": rev.object_id, "revision_id": rev.id, "body": rev.body,
                        "branch_id": source.branch_id, "from_run_id": rev.payload.get("run_id"), "author": rev.author}
                item["read_ref"] = read_ref(item)
                discussions.append(item)
                if len(discussions) == 12:
                    break
        task["discussions"] = list(reversed(discussions))
        task["context_revision_ids"].extend(d["revision_id"] for d in discussions)
        attempt = session.get(Attempt, task["attempt_id"])
        task["context_revision_ids"] = sorted(set(task["context_revision_ids"] + extra_ids))
        attempt.checkpoint = {**attempt.checkpoint, "context_revision_ids": task["context_revision_ids"],
                              "external_read_set": list(external_heads.values()), "autonomous": True}
        return compact_task(task)

    def wake_waiting(self, session):
        for run in session.scalars(select(Run).where(Run.state == "waiting_children")):
            children = session.scalars(select(Run).join(AgentRun, AgentRun.run_id == Run.id).where(AgentRun.parent_run_id == run.id)).all()
            if children and all(child.state in TERMINAL for child in children):
                try:
                    self.branch_allowed(session, run)
                except DomainError:
                    continue
                config = session.get(AgentRun, run.id)
                count = session.scalar(select(func.count()).select_from(AgentStep).where(AgentStep.run_id == run.id))
                if count >= config.options["max_steps"]:
                    run.state = "step_limit"
                    self.runtime._emit(session, run, "run.step_limit", {"run_id": run.id})
                    continue
                run.state = "queued"
                run.current_attempt_id = None
                self.runtime._emit(session, run, "run.children_ready", {"run_id": run.id})

    def recover_saved_response(self, session, run, attempt):
        """Recover an expired attempt's durable output without another provider call.

        Complete observations are input evidence, not permission to revive a lease.
        Runtime.complete keeps its normal version/control fencing, and autonomous
        apply_step skips actions whenever that completion is quarantined.
        """
        if attempt.output_revision_id or not self.runtime._expired(attempt):
            return False
        requests = session.scalars(
            select(ProviderRequest).where(ProviderRequest.attempt_id == attempt.id)
        ).all()
        observations = session.execute(
            select(ProviderCall, ProviderRequest)
            .join(ProviderRequest, ProviderRequest.id == ProviderCall.request_id)
            .where(
                ProviderCall.attempt_id == attempt.id,
                ProviderRequest.attempt_id == attempt.id,
                ProviderCall.complete.is_(True),
                ProviderRequest.state.in_(["dispatched", "spent"]),
            )
            .order_by(ProviderCall.created_at.desc(), ProviderCall.request_id.desc())
        ).all()
        for observation, request in observations:
            if observation.result is None:
                continue
            try:
                result = validate_result(
                    observation.result,
                    mode=attempt.checkpoint.get("mode", "research"),
                    read_set=attempt.read_set,
                    context_revision_ids=attempt.checkpoint.get("context_revision_ids", []),
                )
            except (ValueError, TypeError):
                continue
            # Another in-flight/unknown call cannot be explained by this result.
            # Keep the ordinary reconciliation path instead of inventing its cost.
            if any(
                other.id != request.id and other.state in {"dispatched", "unknown"}
                for other in requests
            ):
                continue
            try:
                with session.begin_nested():
                    for other in requests:
                        if other.state == "reserved":
                            other.state = "released"
                            other.reason = "lease_expired_before_dispatch"
                    self.runtime.settle_request(
                        session,
                        {
                            "request_id": request.id,
                            "token": attempt.token,
                            "outcome": "spent",
                            "usage": dict(observation.usage),
                            "provider_request_id": observation.provider_request_id,
                            "reason": "recovered_complete_provider_observation",
                        },
                    )
                    execution = {
                        "attempt_id": attempt.id,
                        "token": attempt.token,
                        "result": result.model_dump(),
                    }
                    configuration = session.get(AgentRun, run.id)
                    if configuration and configuration.autonomous:
                        _, completion = self.apply_step(
                            session, {**execution, "request_id": request.id}
                        )
                    else:
                        _, completion = self.runtime.complete(
                            session, {**execution, "body": result.body}
                        )
                    attempt.checkpoint = {
                        **attempt.checkpoint,
                        "recovered_request_id": request.id,
                        "recovery": "saved_complete_response",
                    }
                    self.runtime._emit(
                        session,
                        run,
                        "attempt.saved_response_recovered",
                        {
                            "attempt_id": attempt.id,
                            "request_id": request.id,
                            "output_revision_id": completion["output_revision_id"],
                            "quarantined": completion["quarantined"],
                            "skipped_actions": len(result.actions),
                            "provider_redispatched": False,
                        },
                    )
                return True
            except DomainError:
                # Roll back settlement and partial writes if the stored response
                # fails a current invariant; the lease path retains unknown state.
                continue
        return False

    def observe(self, session, payload):
        request, attempt, run = self.runtime._request(session, payload)
        observation = payload["observation"]
        raw = observation.get("raw_text", "")
        if not isinstance(raw, str) or len(raw) > 200000:
            raise DomainError(422, "invalid_observation", "可见输出超出保存范围")
        config = observation.get("call_config", {})
        # Credentials are never accepted as configuration metadata.
        allowed = {"provider", "model", "parameters", "request_timeout_seconds", "prompt_template_version",
                   "prompt_template_sha256", "prompt_sha256", "simulated", "transport_timeout_seconds"}
        if set(config) - allowed or len(json.dumps(config, ensure_ascii=False)) > 30000:
            raise DomainError(422, "invalid_call_config", "调用元数据包含不允许的字段")
        row = session.get(ProviderCall, request.id)
        if row and row.call_config != config:
            raise DomainError(409, "call_config_changed", "已冻结的调用配置不能更改")
        if row and row.result is not None:
            if row.raw_text != raw or row.result != payload.get("result"):
                raise DomainError(409, "observation_conflict", "已有调用结果不可覆盖")
            return 200, {"request_id": row.request_id, "saved": True}
        if not row:
            row = ProviderCall(request_id=request.id, attempt_id=attempt.id, call_config=config)
            session.add(row)
        row.raw_text = raw
        row.raw_text_truncated = bool(observation.get("raw_text_truncated", False))
        row.raw_sha256 = hashlib.sha256(raw.encode()).hexdigest()
        row.finish_reason = observation.get("finish_reason")
        row.complete = bool(observation.get("complete", False))
        row.usage = {k: v for k, v in observation.get("usage", {}).items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
        row.provider_request_id = observation.get("provider_request_id")
        row.result = payload.get("result")
        self.runtime._emit(session, run, "request.observation_saved", {"request_id": request.id, "raw_sha256": row.raw_sha256, "complete": row.complete})
        return 200, {"request_id": row.request_id, "saved": True}

    def _spawn(self, session, parent_run, config, arguments, *, review=False):
        children = session.scalars(select(AgentRun).where(AgentRun.parent_run_id == parent_run.id)).all()
        root = session.get(AgentRun, config.root_run_id)
        if (len(children) >= config.options["max_children"]
                or config.depth >= min(config.options["max_depth"], root.options["max_depth"])
                or len(self.subtree_ids(session, root.run_id)) - 1 >= root.options["max_children"]):
            raise DomainError(409, "child_limit", "子任务数量或深度已达上限")
        branch_id = arguments.get("branch_id") or parent_run.branch_id
        branch = self.state.require_branch(session, branch_id)
        parent_branch = self.state.require_branch(session, parent_run.branch_id)
        if branch.project_id != parent_branch.project_id:
            raise DomainError(403, "cross_project_task", "不能创建跨项目子任务")
        target = None
        if review:
            reviews = [c for c in children if session.get(RunOptions, c.run_id).mode == "review"]
            if len(reviews) >= config.options["max_review_rounds"]:
                raise DomainError(409, "review_round_limit", "已达两轮以内的审查修订上限")
            target, obj = self.state.check_revision(session, branch, arguments["target_revision_id"], current=True)
            goal_id = obj.id
        else:
            goal_id = arguments["goal_object_id"]
        payload = {"branch_id": branch_id, "goal_object_id": goal_id, "provider": parent_run.provider,
                   "mode": "review" if review else "research", "instruction": arguments["instruction"],
                   "request_budget": 1 if review else arguments.get("request_budget", 3),
                   "autonomous": not review, **config.options}
        _, created = self.runtime.create(session, payload)
        child = session.get(AgentRun, created["run_id"])
        child.parent_run_id = parent_run.id
        child.root_run_id = config.root_run_id
        child.depth = config.depth + 1
        child.target_revision_id = target.id if target else None
        return created

    def execute_action(self, session, run, action):
        from mathagent.api.research_routes import FailureCreate, SourceCreate
        from mathagent.application.research_records import ResearchRecordsService

        branch = self.state.require_branch(session, run.branch_id)
        self.branch_allowed(session, run)
        kind, arguments = action.type, dict(action.arguments)
        if kind in {"revise_object", "propose_proof", "record_failure", "record_source"} and arguments.get("branch_id") not in {None, branch.id}:
            raise DomainError(403, "operation_outside_run_branch", "此任务只能修改自己的研究分支；可创建该分支上的子任务")
        project = session.get(Project, branch.project_id)
        permitted = project.policies.get("agent_operations", list(operation_schemas()))
        if kind not in permitted:
            raise DomainError(403, "operation_not_allowed", "项目未授权此研究操作")
        config = session.get(AgentRun, run.id)
        author = "agent:" + run.provider
        if kind in {"record_failure", "record_source"}:
            schema = FailureCreate if kind == "record_failure" else SourceCreate
            values = schema.model_validate({**arguments, "branch_id": branch.id}).model_dump(mode="json")
            records = ResearchRecordsService(self.state)
            _, response = (records.create_failure if kind == "record_failure" else records.create_source)(session, values, author=author)
            return response
        model = OPERATION_MODELS[kind]
        values = model.model_validate(arguments).model_dump(mode="json")
        if kind == "read_object":
            if values.get("branch_id"):
                requested_branch = self.state.require_branch(session, values["branch_id"])
                if requested_branch.project_id != branch.project_id:
                    raise DomainError(403, "cross_project_read", "不能读取其他项目的对象")
                branch = requested_branch
            head = session.get(Head, (branch.id, values["object_id"]))
            if not head:
                raise DomainError(422, "object_outside_branch", "对象不在当前分支")
            from mathagent.runtime.context import read_page
            item = self._input(session, branch, values.get("revision_id") or head.revision_id)
            if item["object_id"] != values["object_id"]:
                raise DomainError(422, "revision_object_mismatch", "版本不属于指定对象")
            if values.get("context_attempt_id"):
                context_attempt = session.get(Attempt, values["context_attempt_id"])
                if not context_attempt or context_attempt.run_id != run.id or values["section"] != "record":
                    raise DomainError(403, "invalid_context_attempt", "只能回读本任务指定步骤的执行上下文记录")
                if item["object_id"] != run.goal_object_id:
                    raise DomainError(422, "context_goal_required", "执行上下文必须通过任务目标读取")
                context = context_attempt.checkpoint
                # Full histories are read via immutable output records, never repeated in every page.
                item["run_context"] = {"attempt_id": context_attempt.id, "run_id": run.id,
                    "instruction": context.get("instruction", ""),
                    "input_revisions": [{"revision_id": rid, "object_id": rev.object_id,
                        "branch_id": source.branch_id, "section": "record"}
                        for rid in context.get("context_revision_ids", [])
                        if (rev := session.get(Revision, rid)) is not None
                        and (source := session.scalar(select(Head).where(Head.object_id == rev.object_id).order_by(Head.branch_id))) is not None],
                    "proof_plans": context.get("proof_plans", []),
                    "previous_steps": [{"number": step.number, "object_id": rev.object_id,
                        "revision_id": rev.id, "branch_id": run.branch_id, "section": "record"}
                        for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.number))
                        if step.attempt_id != context_attempt.id
                        and session.get(Attempt, step.attempt_id).number < context_attempt.number
                        and (rev := session.get(Revision, step.output_revision_id)) is not None],
                    "provider_observations": [{"request_id": call.request_id, "raw_text": call.raw_text,
                        "raw_sha256": call.raw_sha256, "raw_text_truncated": getattr(call, "raw_text_truncated", False)}
                        for call in session.scalars(select(ProviderCall).where(
                            ProviderCall.attempt_id == context_attempt.id).order_by(ProviderCall.request_id))]}
            try:
                page = read_page(item, values)
            except ValueError as exc:
                raise DomainError(422, "invalid_read_page", str(exc)) from exc
            if values.get("context_attempt_id"):
                page["read_ref"]["context_attempt_id"] = values["context_attempt_id"]
            return page
        if kind == "search_project":
            query = select(Revision).join(ResearchObject).where(ResearchObject.project_id == branch.project_id,
                      Revision.body.contains(values["query"], autoescape=True)).order_by(Revision.created_at.desc()).limit(values["limit"])
            items = []
            for rev in session.scalars(query):
                heads = session.scalars(select(Head).where(Head.object_id == rev.object_id).order_by(Head.branch_id)).all()
                if not heads:
                    continue
                source_head = next((h for h in heads if h.branch_id == branch.id), heads[0])
                source_branch = self.state.require_branch(session, source_head.branch_id)
                from mathagent.runtime.context import read_page
                item = read_page(self._input(session, source_branch, rev.id), {"max_chars": 4000})
                item.update(branch_ids=[h.branch_id for h in heads], is_current=item["current"])
                items.append(item)
            return {"items": items}
        if kind == "write_draft":
            _, response = self.state.create_object(session, {**values, "branch_id": branch.id}, author=author)
            return response
        if kind == "revise_object":
            values["branch_id"] = branch.id
            status, response = self.state.revise_object(session, values, author=author)
            return {"http_status": status, **response}
        if kind == "propose_proof":
            values["branch_id"] = branch.id
            _, response = self.state.create_proof(session, values, author=author)
            return response
        if kind == "create_branch":
            _, response = self.state.create_branch(session, {"source_branch_id": branch.id, "name": values["name"]}, author=author)
            return response
        if kind in {"spawn_task", "request_review"}:
            return self._spawn(session, run, config, values, review=kind == "request_review")
        if kind == "discuss":
            if values.get("recipient_run_id"):
                target = self.runtime._run(session, values["recipient_run_id"])
                if self.state.require_branch(session, target.branch_id).project_id != branch.project_id:
                    raise DomainError(403, "cross_project_discussion", "讨论不能越过项目")
            obj, rev = self.state.new_object(session, branch, "activity", values["body"],
                          {"artifact_type": "discussion", "run_id": run.id, "recipient_run_id": values.get("recipient_run_id")}, author)
            self.state._add_reference(session, branch.id, rev.id)
            return {"object_id": obj.id, "revision_id": rev.id}
        if kind == "calculate":
            from mathagent.tools.exact import execute_calculation
            return execute_calculation(self.state, session, branch, values, author=author, run_id=run.id)
        raise DomainError(422, "unknown_operation", "不支持的操作")

    def apply_step(self, session, payload):
        attempt, run = self.runtime._attempt(session, payload)
        existing = session.scalar(select(AgentStep).where(AgentStep.request_id == payload["request_id"]))
        if existing:
            if existing.attempt_id != attempt.id:
                raise DomainError(409, "wrong_step_attempt", "步骤归属不一致")
            digest = hashlib.sha256(json.dumps(payload["result"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if attempt.checkpoint.get("agent_result_sha256") != digest:
                raise DomainError(409, "step_result_conflict", "该请求已经保存了不同的研究步骤")
            return 200, existing.receipt
        request = session.get(ProviderRequest, payload["request_id"])
        if not request or request.attempt_id != attempt.id or request.state != "spent":
            raise DomainError(409, "step_request_unsettled", "保存操作前必须结算对应请求")
        config = session.get(AgentRun, run.id)
        if not config or not config.autonomous:
            raise DomainError(409, "not_autonomous", "此任务未启用自主操作")
        result = validate_result(payload["result"], mode=attempt.checkpoint["mode"],
                    read_set=attempt.read_set, context_revision_ids=attempt.checkpoint.get("context_revision_ids", []))
        attempt.checkpoint = {**attempt.checkpoint, "agent_result_sha256": hashlib.sha256(
            json.dumps(payload["result"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        number = (session.scalar(select(func.max(AgentStep.number)).where(AgentStep.run_id == run.id)) or 0) + 1
        # Save the model's immutable draft before any proposal changes its inputs.
        _, completion = self.runtime.complete(session, {"attempt_id": attempt.id, "token": payload["token"],
                                                        "body": result.body, "result": result.model_dump()})
        outcomes = []
        if not completion["quarantined"] and number <= config.options["max_steps"]:
            for action in result.actions:
                try:
                    with session.begin_nested():
                        value = self.execute_action(session, run, action)
                    outcomes.append({"type": action.type, "status": "completed", "result": value})
                except (DomainError, ValidationError, ValueError, KeyError) as error:
                    code = error.response.get("error") if isinstance(error, DomainError) else "invalid_operation_arguments"
                    rejected = {"type": action.type, "status": "rejected", "error": code}
                    if action.type == "calculate" and isinstance(error, ValidationError):
                        from mathagent.providers.actions import calculation_validation_feedback

                        rejected.update(calculation_validation_feedback(error, action.arguments.get("tool")))
                    outcomes.append(rejected)
            session.flush()
            children = session.scalars(select(Run).join(AgentRun, AgentRun.run_id == Run.id).where(AgentRun.parent_run_id == run.id)).all()
            waiting = any(child.state not in TERMINAL for child in children)
            if waiting:
                run.state = "waiting_children"
            elif result.next_action != "finish" and number < config.options["max_steps"]:
                run.state = "queued"
            elif result.next_action != "finish":
                run.state = "step_limit"
            else:
                run.state = "completed"
            if run.state == "queued":
                run.current_attempt_id = None
        receipt = {"run_id": run.id, "state": run.state, "continue": run.state == "queued", "number": number,
                   "output_revision_id": completion["output_revision_id"], "quarantined": completion["quarantined"]}
        row = AgentStep(run_id=run.id, attempt_id=attempt.id, request_id=request.id, number=number,
                        state=run.state, body=result.body, actions=outcomes, receipt=receipt,
                        output_revision_id=completion["output_revision_id"])
        session.add(row)
        session.flush()
        self.runtime._emit(session, run, "agent.step_saved", {"step_id": row.id, **receipt})
        return 201, receipt
