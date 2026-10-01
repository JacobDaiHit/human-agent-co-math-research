"""Shared resource accounting, observations, and manual/historical records."""

import hashlib
import json

from mathagent.application.code_execution import permitted_operations
from mathagent.application.errors import DomainError
from mathagent.persistence.agent_models import AgentRun, AgentStep, BranchRuntime, ProviderCall
from mathagent.persistence.models import (
    Adoption,
    Attempt,
    Head,
    Project,
    Review,
    Revision,
    Run,
)
from mathagent.persistence.runtime_models import ProviderRequest, RunOptions
from mathagent.providers.actions import operation_schemas
from mathagent.providers.protocol import validate_result
from sqlalchemy import func, select

DEFAULT_OPTIONS = {
    "max_steps": 8, "max_review_rounds": 2, "max_children": 4, "max_depth": 2,
    "max_output_tokens": 4096, "request_timeout_seconds": 180,
    "cumulative_output_token_budget": None,
    "thinking_mode": "provider_default", "reasoning_effort": "provider_default",
    "completion_policy": "draft",
    "length_recovery": "none",
    "answer_submission_recovery": False,
    "answer_requires_exhaustiveness": False,
    "unknown_recovery": "stop",
    "solver_controller": "continuous_research", "search_config": {},
    "discussion": True, "research_deadline_seconds": 1800,
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
        research = self.runtime.research.session_for(session, run_id)
        settings_id = research.root_run_id if research else run_id
        row = session.get(AgentRun, settings_id)
        budget = session.get(RunOptions, settings_id)
        values = {"run_id": run_id, "request_budget": budget.request_budget if budget else 5,
                "autonomous": bool(row and row.autonomous), **DEFAULT_OPTIONS,
                **(row.options if row else {})}
        if row and row.autonomous and not research and values["solver_controller"] == "continuous_research":
            values["solver_controller"] = "legacy"
        return values

    def update_options(self, session, payload):
        run = self.runtime._run(session, payload["run_id"])
        research = self.runtime.research.session_for(session, run.id)
        if research:
            run = session.get(Run, research.root_run_id)
        if self.runtime.search.session_for(session, run.id):
            raise DomainError(409, "search_config_frozen", "受控会话配置已冻结；请新建会话比较另一配置。")
        expected = self.options(session, run.id)["solver_controller"]
        if payload.get("solver_controller", expected) != expected:
            raise DomainError(409, "solver_new_session_required", "求解过程不在途中切换；请新建研究任务。")
        if research and payload.get("autonomous") is False:
            raise DomainError(409, "research_mode_fixed", "连续任务请使用暂停或停止，不在途中改成单轮草稿。")
        if not research and payload.get("autonomous") is True:
            raise DomainError(409, "solver_new_session_required", "请新建连续研究任务，不重写历史执行过程。")
        if any(value is None for key, value in payload.items() if key != "cumulative_output_token_budget"):
            raise DomainError(422, "null_run_option", "此运行选项不能设为 null。")
        row = session.get(AgentRun, run.id) or self.register(session, run.id, payload)
        row.autonomous = payload.get("autonomous", row.autonomous)
        row.options = {k: payload.get(k, row.options.get(k, default)) for k, default in DEFAULT_OPTIONS.items()}
        if research:
            research.config = {**research.config,
                "discussion": payload.get("discussion", research.config["discussion"]),
                "deadline_seconds": payload.get("research_deadline_seconds", research.config["deadline_seconds"])}
            if not research.config["discussion"]:
                self.runtime.research.discussion_closed(session, research)
        if "request_budget" in payload:
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
        return self.request_budget_status(session, run, requests)["remaining"] <= 0

    @staticmethod
    def _nonnegative_int(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def output_token_budget_status(self, session, run, requests=None):
        """Return conservative root-tree output accounting.

        Vendor usage is not assumed to be complete.  A request with no usable
        completion count continues to occupy its pre-dispatch reservation,
        including unknown outcomes.
        """
        config = session.get(AgentRun, run.id)
        root_id = config.root_run_id if config else run.id
        root = session.get(AgentRun, root_id)
        limit = (root.options if root else {}).get("cumulative_output_token_budget")
        family = set(session.scalars(select(AgentRun.run_id).where(AgentRun.root_run_id == root_id)))
        family.add(root_id)
        if requests is None:
            requests = session.scalars(select(ProviderRequest).where(ProviderRequest.run_id.in_(family))).all()
        else:
            requests = [item for item in requests if item.run_id in family]
        reported_output = reported_input = reserved = unknown_requests = incomplete_usage = 0
        for request in requests:
            usage = request.usage if isinstance(request.usage, dict) else {}
            completion = self._nonnegative_int(usage.get("completion_tokens"))
            prompt = self._nonnegative_int(usage.get("prompt_tokens"))
            if prompt is not None:
                reported_input += prompt
            if request.state == "spent" and completion is not None:
                reported_output += completion
            elif request.state in {"reserved", "dispatched", "unknown", "spent"}:
                reservation = request.output_token_reservation
                if reservation == 0:
                    # Rows created before 0007 have no durable reservation.
                    # Recover a saved provider cap when possible; otherwise use
                    # the API's maximum rather than falsely treating unknown
                    # historical usage as free after a limit is enabled.
                    call = session.get(ProviderCall, request.id)
                    parameters = (call.call_config or {}).get("parameters", {}) if call else {}
                    configured = parameters.get("max_tokens", parameters.get("max_output_tokens")) \
                        if isinstance(parameters, dict) else None
                    reservation = configured if self._nonnegative_int(configured) is not None and 256 <= configured <= 65_536 else 65_536
                reserved += reservation
                incomplete_usage += 1
            if request.state == "unknown":
                unknown_requests += 1
        occupied = reported_output + reserved
        enabled = isinstance(limit, int) and not isinstance(limit, bool)
        remaining = max(0, limit - occupied) if enabled else None
        return {
            "unit": "output_tokens", "enabled": enabled, "limit": limit if enabled else None,
            "reported_output_tokens": reported_output,
            "reported_input_tokens": reported_input,
            "reserved_output_tokens": reserved,
            "occupied_output_tokens": occupied,
            "remaining_output_tokens": remaining,
            "unknown_usage_requests": incomplete_usage,
            "unknown_outcome_requests": unknown_requests,
            "scope": "root_run_and_descendants",
            "includes_format_repairs": True,
        }

    def request_budget_status(self, session, run, requests=None):
        project_id = self.state.require_branch(session, run.branch_id).project_id
        if requests is None:
            requests = session.scalars(select(ProviderRequest).where(ProviderRequest.project_id == project_id)).all()
        scopes = []

        def scope(kind, limit, selected):
            counts = self.runtime._counts(selected)
            scopes.append({"scope": kind, "limit": limit, "occupied": counts["occupied"],
                           "unknown": counts["unknown"], "remaining": max(0, limit - counts["occupied"])})

        scope("project", self.runtime._settings(session, project_id).request_budget, requests)
        own = session.get(RunOptions, run.id)
        scope("run", own.request_budget if own else 5, [r for r in requests if r.run_id == run.id])
        row = session.get(AgentRun, run.id)
        root = session.get(Run, row.root_run_id) if row else run
        for branch_id in sorted({run.branch_id, root.branch_id}):
            branch_roots = set(session.scalars(select(Run.id).where(Run.branch_id == branch_id)))
            branch_ids = branch_roots | set(session.scalars(select(AgentRun.run_id).where(AgentRun.root_run_id.in_(branch_roots))))
            limit = self.branch_settings(session, branch_id)["request_budget"]
            scope("branch", limit, [r for r in requests if r.run_id in branch_ids])
        while row:
            family = self.subtree_ids(session, row.run_id)
            options = session.get(RunOptions, row.run_id)
            scope("subtree", options.request_budget, [r for r in requests if r.run_id in family])
            row = session.get(AgentRun, row.parent_run_id) if row.parent_run_id else None
        remaining = min(s["remaining"] for s in scopes)
        return {"unit": "requests", "remaining": remaining, "after_this_request": max(0, remaining - 1),
                "snapshot": "before_request_reservation", "scopes": scopes,
                "shared_with_descendants": True, "includes_format_repairs": True}

    def initial_read_set(self, session, run, all_heads):
        if self.runtime.research.session_for(session, run.id):
            return {run.goal_object_id: all_heads[run.goal_object_id]} if run.goal_object_id in all_heads else {}
        return all_heads

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

        from mathagent.runtime.context import compact_task, read_ref

        if self.runtime.research.session_for(session, task["run_id"]):
            return self.runtime.research.enrich(session, task)
        options = self.options(session, task["run_id"])
        task.update(options)
        run = self.runtime._run(session, task["run_id"])
        output_budget = self.output_token_budget_status(session, run)
        task["output_token_budget_status"] = output_budget
        if output_budget["enabled"]:
            # The service repeats this check atomically at reservation time.
            # This is an effective provider cap, never an assertion about an
            # exact prompt-token tokenizer bound.
            task["max_output_tokens"] = min(task["max_output_tokens"], output_budget["remaining_output_tokens"])
        branch = self.state.require_branch(session, self.runtime._run(session, task["run_id"]).branch_id)
        permitted = permitted_operations(session.get(Project, branch.project_id))
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
        task["autonomous"] = False
        task["operation_schemas"] = {}
        task["request_budget_status"] = self.request_budget_status(session, run)
        return compact_task(task)

    def recover_saved_response(self, session, run, attempt):
        """Recover an expired attempt's durable output without another provider call.

        Complete observations are input evidence, not permission to revive a lease.
        Runtime.complete keeps its normal version/control fencing, and autonomous
        apply_step skips actions whenever that completion is quarantined.
        """
        if self.runtime.research.session_for(session, run.id):
            return self.runtime.research.recover_saved_response(session, run, attempt)
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
                    autonomous=bool(attempt.checkpoint.get("autonomous", False)),
                )
            except (ValueError, TypeError):
                continue
            # Only an explicitly authorized unknown may coexist with this output.
            # Recovery still quarantines it and preserves the unknown cost.
            if any(
                other.id != request.id and other.state in {"dispatched", "unknown"}
                and not self.runtime._continued_unknown(other, attempt)
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
        if attempt.checkpoint.get("material_erased"):
            raise DomainError(410, "material_deleted", "相关材料已永久删除，迟到输出不能写回。")
        if request.state == "unknown":
            raise DomainError(409, "request_outcome_unknown", "未知请求的观察记录已冻结，不能作为后续产物提交")
        observation = payload["observation"]
        raw = observation.get("raw_text", "")
        if not isinstance(raw, str) or len(raw) > 200000:
            raise DomainError(422, "invalid_observation", "可见输出超出保存范围")
        config = observation.get("call_config", {})
        # Credentials are never accepted as configuration metadata.
        allowed = {"provider", "model", "parameters", "request_timeout_seconds", "prompt_template_version",
                   "prompt_template_sha256", "prompt_sha256", "simulated", "transport_timeout_seconds", "review_input_receipt"}
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

    def apply_step(self, session, payload):
        attempt = session.get(Attempt, payload["attempt_id"])
        if attempt and self.runtime.research.session_for(session, attempt.run_id):
            return self.runtime.research.apply_turn(session, payload)
        # Read compatibility for a response already purchased by the retired
        # workflow. Store its ordinary body; never replay its operations or buy
        # another planning, review, repair or finalization request.
        attempt, run = self.runtime._attempt(session, payload)
        existing = session.scalar(select(AgentStep).where(AgentStep.request_id == payload["request_id"]))
        if existing:
            return 200, existing.receipt
        request = session.get(ProviderRequest, payload["request_id"])
        if not request or request.attempt_id != attempt.id or request.state != "spent":
            raise DomainError(409, "step_request_unsettled", "先结算已发出的历史请求，再保存输出。")
        result = validate_result(payload["result"], mode=attempt.checkpoint["mode"],
            read_set=attempt.read_set, context_revision_ids=attempt.checkpoint.get("context_revision_ids", []),
            autonomous=True)
        _, completion = self.runtime.complete(session, {
            "attempt_id": attempt.id, "token": payload["token"],
            "body": result.body, "result": result.model_dump()}, agent_step=True)
        number = (session.scalar(select(func.max(AgentStep.number)).where(AgentStep.run_id == run.id)) or 0) + 1
        receipt = {**completion, "continue": False, "number": number, "legacy_workflow_retired": True}
        row = AgentStep(run_id=run.id, attempt_id=attempt.id, request_id=request.id, number=number,
            state=run.state, body=result.body, actions=[], receipt=receipt,
            output_revision_id=completion["output_revision_id"])
        session.add(row)
        session.flush()
        self.runtime._emit(session, run, "agent.step_saved", {"step_id": row.id, **receipt})
        return 201, receipt
