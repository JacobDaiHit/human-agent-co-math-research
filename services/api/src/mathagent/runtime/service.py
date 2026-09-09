"""Persisted worker lifecycle with leases, input fencing, and late-result isolation.

The caller owns the transaction and command idempotency. No method performs network
requests or commits. Unknown external outcomes retain their budget occupancy.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from mathagent.application.errors import DomainError
from mathagent.persistence.agent_models import AgentRun, ProviderCall
from mathagent.persistence.models import (
    Attempt,
    Branch,
    Dependency,
    Head,
    Project,
    ProofPlan,
    Review,
    Run,
)
from mathagent.persistence.runtime_models import ProviderRequest, RunOptions, RuntimeSettings
from mathagent.providers.fake import FakeProvider
from mathagent.providers.protocol import validate_result
from mathagent.providers.remote import ProviderConfig
from mathagent.runtime.agent import AgentRuntime
from mathagent.runtime.recovery import UNKNOWN_TRANSPORT_FAILURES
from sqlalchemy import func, select


class Runtime:
    def __init__(self, service, *, lease_seconds: int = 60, max_active_attempts: int = 2):
        if lease_seconds < 1 or max_active_attempts < 1:
            raise ValueError("Lease duration and active-attempt limit must be positive")
        self.service = service
        self.lease_seconds = lease_seconds
        self.max_active_attempts = max_active_attempts
        self.provider = FakeProvider()
        self.agent = AgentRuntime(self)

    def _settings(self, session, project_id):
        if session.get(Project, project_id) is None:
            raise DomainError(404, "project_not_found", "研究项目不存在")
        row = session.get(RuntimeSettings, project_id)
        return row or RuntimeSettings(
            project_id=project_id,
            request_budget=100,
            allow_real_api=False,
            allowed_providers=["fake"],
        )

    def _check_permission(self, session, project_id, provider):
        settings = self._settings(session, project_id)
        if provider not in settings.allowed_providers:
            raise DomainError(403, "provider_not_allowed", "项目未授权此提供方")
        if provider != "fake" and (
            not settings.allow_real_api or not ProviderConfig.from_env(provider).ready()
        ):
            raise DomainError(403, "real_api_disabled", "真实 API 需要环境配置和项目显式授权")

    def get_settings(self, project_id):
        with self.service.db.sessions() as session:
            row = self._settings(session, project_id)
            return {
                "project_id": project_id,
                "request_budget": row.request_budget,
                "allow_real_api": row.allow_real_api,
                "allowed_providers": row.allowed_providers,
            }

    def update_settings(self, session, payload):
        row = self._settings(session, payload["project_id"])
        row.request_budget = payload["request_budget"]
        row.allow_real_api = payload["allow_real_api"]
        row.allowed_providers = list(dict.fromkeys(payload["allowed_providers"]))
        session.add(row)
        response = {
            "project_id": row.project_id,
            "request_budget": row.request_budget,
            "allow_real_api": row.allow_real_api,
            "allowed_providers": row.allowed_providers,
        }
        self.service.emit(session, row.project_id, None, "runtime.settings_changed", response)
        return 200, response

    @staticmethod
    def _counts(requests):
        counts = dict.fromkeys(("reserved", "dispatched", "spent", "unknown", "released"), 0)
        for request in requests:
            counts[request.state] += 1
        counts["occupied"] = sum(
            counts[state] for state in ("reserved", "dispatched", "spent", "unknown")
        )
        return counts

    def get_budget(self, *, project_id=None, run_id=None):
        with self.service.db.sessions() as session:
            if run_id:
                run = self._run(session, run_id)
                project_id = self.service.require_branch(session, run.branch_id).project_id
                options = session.get(RunOptions, run_id)
                limit = options.request_budget if options else 5
                query = select(ProviderRequest).where(ProviderRequest.run_id.in_(self.agent.subtree_ids(session, run_id)))
            else:
                limit = self._settings(session, project_id).request_budget
                query = select(ProviderRequest).where(ProviderRequest.project_id == project_id)
            requests = session.scalars(query.order_by(ProviderRequest.created_at)).all()
            counts = self._counts(requests)
            return {
                "project_id": project_id,
                "run_id": run_id,
                "request_budget": limit,
                **counts,
                "remaining": max(0, limit - counts["occupied"]),
                "unit": "requests",
                "requests": [self._request_record(r) for r in requests],
            }

    @staticmethod
    def _request_record(row):
        return {
            "request_id": row.id,
            "run_id": row.run_id,
            "attempt_id": row.attempt_id,
            "provider": row.provider,
            "state": row.state,
            "usage": row.usage,
            "reason": row.reason,
            "provider_request_id": row.provider_request_id,
            "created_at": row.created_at,
            "simulated": row.provider == "fake",
        }

    def _attempt(self, session, payload, *, active=False):
        attempt = session.get(Attempt, payload["attempt_id"])
        if attempt is None:
            raise DomainError(404, "attempt_not_found", "执行记录不存在")
        if not secrets.compare_digest(attempt.token, payload["token"]):
            raise DomainError(409, "invalid_execution_token", "执行令牌已失效或不匹配")
        run = self._run(session, attempt.run_id)
        if active and (
            attempt.state != "running"
            or self._expired(attempt)
            or run.current_attempt_id != attempt.id
        ):
            raise DomainError(409, "attempt_not_active", "执行租约已结束")
        return attempt, run

    def _inputs_stale(self, session, attempt, run):
        branch = self.service.require_branch(session, run.branch_id)
        current = self.service.read_set(session, branch.id)
        return (
            attempt.control_epoch != run.control_epoch
            or branch.control_epoch != attempt.checkpoint.get("branch_control_epoch")
            or any(current.get(k) != v for k, v in attempt.read_set.items())
            or any((head := session.get(Head, (item["branch_id"], item["object_id"]))) is None
                   or head.revision_id != item["revision_id"] for item in attempt.checkpoint.get("external_read_set", []))
        )

    @staticmethod
    def _continued_unknown(row, attempt):
        return row.state == "unknown" and row.id in attempt.checkpoint.get("continued_unknown_request_ids", [])

    def _blocking_unknown(self, session, row):
        attempt = session.get(Attempt, row.attempt_id)
        # Later steps may proceed only after the authorized retry delivered an
        # output. A crash/failure before delivery still requires reconciliation.
        return row.state == "unknown" and not (
            attempt.output_revision_id and self._continued_unknown(row, attempt)
        )

    def _boundary(self, session, attempt, run):
        stopped = self._inputs_stale(session, attempt, run) or run.state != "running"
        if stopped:
            requests = session.scalars(
                select(ProviderRequest).where(ProviderRequest.attempt_id == attempt.id)
            ).all()
            if any(
                row.state == "dispatched"
                or (row.state == "unknown" and not self._continued_unknown(row, attempt))
                for row in requests
            ):
                return False
            for row in requests:
                if row.state == "reserved":
                    row.state, row.reason = "released", "stopped_before_dispatch"
            attempt.state = "interrupted"
            run.state = {
                "pause_requested": "paused",
                "cancel_requested": "cancelled",
                "steer_requested": "queued",
            }.get(run.state, "interrupted")
            attempt.checkpoint = {
                **attempt.checkpoint,
                "boundary": "stopped",
                "end_reason": "control_or_inputs_changed",
            }
            self._emit(
                session, run, "attempt.stopped", {"attempt_id": attempt.id, "state": run.state}
            )
        return stopped

    def heartbeat(self, session, payload):
        attempt, run = self._attempt(session, payload, active=True)
        if payload.get("boundary") and self._boundary(session, attempt, run):
            return 200, {"attempt_id": attempt.id, "state": run.state, "continue": False}
        attempt.lease_until = (
            datetime.now(UTC) + timedelta(seconds=self.lease_seconds)
        ).isoformat()
        return 200, {
            "attempt_id": attempt.id,
            "state": run.state,
            "continue": True,
            "control_pending": run.state != "running",
            "lease_until": attempt.lease_until,
        }

    def claim_next(self, session, payload):
        self.agent.wake_waiting(session)
        # Reconcile every expired lease first, including tasks for offline providers.
        for run in session.scalars(
            select(Run).where(
                Run.state.in_(["running", "pause_requested", "cancel_requested", "steer_requested"])
            )
        ):
            self._reconcile_expired(session, run)
        session.flush()
        active = session.scalars(select(Attempt).where(Attempt.state == "running")).all()
        if sum(not self._expired(attempt) for attempt in active) >= self.max_active_attempts:
            return 200, {"task": None}
        for run in session.scalars(
            select(Run)
            .where(Run.state == "queued", Run.provider.in_(payload["providers"]))
            .order_by(Run.created_at)
        ):
            try:
                _, task = self.claim(session, {"run_id": run.id})
                if "attempt_id" in task:
                    return 200, {"task": task}
            except DomainError as error:
                if error.status != 403:
                    raise
        return 200, {"task": None}

    def reserve_request(self, session, payload):
        attempt, run = self._attempt(session, payload, active=True)
        if self._boundary(session, attempt, run):
            return 200, {"continue": False, "state": run.state}
        branch = self.service.require_branch(session, run.branch_id)
        self.agent.branch_allowed(session, run)
        self._check_permission(session, branch.project_id, run.provider)
        outstanding = session.scalars(
            select(ProviderRequest).where(
                ProviderRequest.attempt_id == attempt.id,
                ProviderRequest.state.in_(["reserved", "dispatched", "unknown"]),
            )
        )
        if any(not self._continued_unknown(row, attempt) for row in outstanding):
            raise DomainError(409, "request_unsettled", "当前执行已有未结算请求")
        settings = self._settings(session, branch.project_id)
        requests = session.scalars(
            select(ProviderRequest).where(ProviderRequest.project_id == branch.project_id)
        ).all()
        options = session.get(RunOptions, run.id)
        if self._counts(requests)["occupied"] >= settings.request_budget or self._counts(
            [r for r in requests if r.run_id == run.id]
        )["occupied"] >= (options.request_budget if options else 5) or self.agent.additional_budget_exhausted(session, run, requests):
            attempt.state = "failed"
            run.state = "budget_exhausted"
            attempt.checkpoint = {**attempt.checkpoint, "end_reason": "request_budget_exhausted"}
            self._emit(session, run, "run.budget_exhausted", {"attempt_id": attempt.id})
            return 200, {"continue": False, "state": run.state}
        row = ProviderRequest(
            project_id=branch.project_id,
            run_id=run.id,
            attempt_id=attempt.id,
            provider=run.provider,
            state="reserved",
        )
        session.add(row)
        session.flush()
        self._emit(session, run, "request.reserved", self._request_record(row))
        return 201, {"continue": True, **self._request_record(row)}

    def _request(self, session, payload, *, active=False):
        row = session.get(ProviderRequest, payload["request_id"])
        if row is None:
            raise DomainError(404, "request_not_found", "请求记录不存在")
        attempt, run = self._attempt(
            session, {"attempt_id": row.attempt_id, "token": payload["token"]}, active=active
        )
        return row, attempt, run

    def start_request(self, session, payload):
        row, attempt, run = self._request(session, payload, active=True)
        if row.state != "reserved":
            raise DomainError(409, "request_not_reserved", "请求只能从预留状态登记发送")
        if self._boundary(session, attempt, run):
            return 200, {"continue": False, "state": run.state}
        branch = self.service.require_branch(session, run.branch_id)
        self._check_permission(session, branch.project_id, run.provider)
        row.state = "dispatched"
        self._emit(session, run, "request.dispatched", self._request_record(row))
        return 200, {"continue": True, **self._request_record(row)}

    def _authorize_unknown_retry(self, session, row, attempt, run, payload):
        if (not payload.get("retry_unknown") or row.reason not in UNKNOWN_TRANSPORT_FAILURES
                or run.provider == "fake" or run.state != "running" or attempt.state != "running"
                or run.current_attempt_id != attempt.id or self._expired(attempt)
                or self._inputs_stale(session, attempt, run)):
            return False
        config = session.get(AgentRun, run.id)
        root = session.get(AgentRun, config.root_run_id) if config else None
        if (not root or root.options.get("unknown_recovery", "stop") != "once"
                or config.options.get("unknown_recovery", "stop") != "once"):
            return False
        root_run = session.get(Run, root.run_id)
        if root_run.state not in {"running", "waiting_children", "queued"}:
            return False
        observation = session.get(ProviderCall, row.id)
        if not observation or observation.complete or observation.result is not None:
            return False
        try:
            self.agent.branch_allowed(session, run)
            branch = self.service.require_branch(session, run.branch_id)
            self._check_permission(session, branch.project_id, run.provider)
        except DomainError:
            return False
        session.flush()
        if self.agent.request_budget_status(session, run)["remaining"] <= 0:
            return False
        family = select(AgentRun.run_id).where(AgentRun.root_run_id == root.run_id)
        attempts = session.scalars(select(Attempt).where(Attempt.run_id.in_(family)))
        if any(item.checkpoint.get("continued_unknown_request_ids") for item in attempts):
            return False
        # The command transaction serializes this root-wide, durable allowance.
        # Reconciliation, options updates and worker restarts never refund it.
        attempt.checkpoint = {**attempt.checkpoint, "continued_unknown_request_ids": [row.id]}
        self._emit(session, run, "request.unknown_retry_authorized", {
            "request_id": row.id, "attempt_id": attempt.id, "root_run_id": root.run_id,
            "unknown_remains_occupied": True, "retry_limit": 1,
        })
        return True

    def settle_request(self, session, payload):
        row, attempt, run = self._request(session, payload)
        state = {"spent": "spent", "unaccepted": "released", "unknown": "unknown"}[
            payload["outcome"]
        ]
        if row.state == state:
            return 200, self._request_record(row)
        if row.state != "dispatched":
            raise DomainError(409, "request_not_dispatched", "请求已结算，未知结果须由人工对账")
        row.state = state
        row.reason = payload.get("reason", "")
        row.usage = payload.get("usage", {})
        row.provider_request_id = payload.get("provider_request_id")
        retry_allowed = state == "unknown" and self._authorize_unknown_retry(session, row, attempt, run, payload)
        if state == "unknown" and not retry_allowed:
            attempt.checkpoint = {
                **attempt.checkpoint,
                "reconciliation_resume_state": "cancelled"
                if run.state in {"cancel_requested", "cancelled"}
                else "paused",
            }
            attempt.state = "reconciliation_required"
            run.state = "reconciliation_required"
        self._emit(session, run, "request.settled", self._request_record(row))
        return 200, {**self._request_record(row), "unknown_retry_allowed": retry_allowed,
                     "request_budget_status": self.agent.request_budget_status(session, run) if retry_allowed else None}

    def fail(self, session, payload):
        attempt, run = self._attempt(session, payload)
        if attempt.output_revision_id or run.current_attempt_id != attempt.id:
            return 200, {"state": run.state, "attempt_id": attempt.id}
        requests = session.scalars(
            select(ProviderRequest).where(ProviderRequest.attempt_id == attempt.id)
        ).all()
        for row in requests:
            if row.state == "reserved":
                row.state, row.reason = "released", "attempt_failed_before_dispatch"
            elif row.state == "dispatched":
                row.state = "released" if run.provider == "fake" else "unknown"
                row.reason = (
                    "simulation_interrupted"
                    if run.provider == "fake"
                    else "attempt_failed_after_dispatch"
                )
        unknown = any(row.state == "unknown" for row in requests)
        if unknown:
            if "reconciliation_resume_state" not in attempt.checkpoint:
                attempt.checkpoint = {
                    **attempt.checkpoint,
                    "reconciliation_resume_state": "cancelled"
                    if run.state in {"cancel_requested", "cancelled"}
                    else "paused",
                }
            run.state = attempt.state = "reconciliation_required"
        elif run.state in {"running", "pause_requested", "cancel_requested", "steer_requested"}:
            run.state = {
                "pause_requested": "paused",
                "cancel_requested": "cancelled",
                "steer_requested": "queued",
            }.get(run.state, "failed")
            attempt.state = "failed"
        attempt.checkpoint = {
            **attempt.checkpoint,
            "end_reason": payload["reason"],
            "boundary": "failed",
        }
        self._emit(
            session,
            run,
            "attempt.failed",
            {"attempt_id": attempt.id, "state": run.state, "reason": payload["reason"]},
        )
        return 200, {"attempt_id": attempt.id, "state": run.state}

    def reconcile_request(self, session, payload):
        row = session.get(ProviderRequest, payload["request_id"])
        if row is None:
            raise DomainError(404, "request_not_found", "请求记录不存在")
        if row.state != "unknown":
            raise DomainError(409, "request_not_unknown", "仅未知结果需要人工对账")
        row.state = "spent" if payload["outcome"] == "spent" else "released"
        row.reason = payload["reason"]
        run = self._run(session, row.run_id)
        session.flush()
        if not session.scalar(
            select(ProviderRequest.id).where(
                ProviderRequest.run_id == run.id,
                ProviderRequest.state.in_(["dispatched", "unknown"]),
            )
        ):
            if run.state == "reconciliation_required":
                attempt = session.get(Attempt, row.attempt_id)
                run.state = attempt.checkpoint.get("reconciliation_resume_state", "paused")
        self._emit(session, run, "request.reconciled", self._request_record(row), "human")
        return 200, {**self._request_record(row), "run_state": run.state}

    @staticmethod
    def _run(session, run_id):
        run = session.get(Run, run_id)
        if run is None:
            raise DomainError(404, "run_not_found", "运行不存在", run_id=run_id)
        return run

    @staticmethod
    def _expired(attempt):
        # Database timestamps are UTC. Accept naive historical timestamps as UTC.
        until = datetime.fromisoformat(attempt.lease_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return until <= datetime.now(UTC)

    def _emit(self, session, run, event_type, payload, author="system"):
        branch = self.service.require_branch(session, run.branch_id)
        return self.service.emit(
            session,
            branch.project_id,
            branch.id,
            event_type,
            {"run_id": run.id, **payload},
            author=author,
        )

    def create(self, session, payload):
        branch = self.service.require_branch(session, payload["branch_id"])
        goal = self.service.require_object(session, payload["goal_object_id"])
        if goal.project_id != branch.project_id or session.get(Head, (branch.id, goal.id)) is None:
            raise DomainError(422, "goal_not_in_branch", "运行目标必须属于当前分支")
        provider = payload.get("provider", "fake")
        if provider not in {"fake", "deepseek", "glm"}:
            raise DomainError(422, "provider_not_available", "不支持的提供方")
        self._check_permission(session, branch.project_id, provider)
        mode = payload.get("mode", "research")
        if mode not in {"research", "review"}:
            raise DomainError(422, "invalid_mode", "运行类型必须是 research 或 review")
        budget = payload.get("request_budget", 5)
        if not 1 <= budget <= 1000:
            raise DomainError(422, "invalid_budget", "运行请求上限必须在 1 至 1000 之间")
        run = Run(
            branch_id=branch.id,
            goal_object_id=goal.id,
            provider=provider,
            state="queued",
            instruction=payload.get("instruction", ""),
            control_epoch=0,
        )
        session.add(run)
        session.flush()
        session.add(RunOptions(run_id=run.id, mode=mode, request_budget=budget))
        self.agent.register(session, run.id, payload)
        self.agent.branch_allowed(session, run)
        response = {
            "run_id": run.id,
            "state": run.state,
            "provider": provider,
            "simulated": provider == "fake",
            "mode": mode,
            "request_budget": budget,
        }
        self._emit(session, run, "run.created", response, "human")
        return 201, response

    def _reconcile_expired(self, session, run):
        attempt = session.get(Attempt, run.current_attempt_id) if run.current_attempt_id else None
        if attempt is None or attempt.state != "running" or not self._expired(attempt):
            return
        if self.agent.recover_saved_response(session, run, attempt):
            return
        requests = session.scalars(
            select(ProviderRequest).where(ProviderRequest.attempt_id == attempt.id)
        ).all()
        uncertain = False
        for request in requests:
            if request.state == "reserved":
                request.state = "released"
                request.reason = "lease_expired_before_dispatch"
            elif request.state == "dispatched":
                request.state = "released" if run.provider == "fake" else "unknown"
                request.reason = "lease_expired_during_dispatch"
            uncertain |= request.state == "unknown" and run.provider != "fake"
        attempt.state = "expired"
        attempt.checkpoint = {
            **attempt.checkpoint,
            "end_reason": "lease_expired",
            "reconciled": True,
            "reconciliation": "external_outcome_requires_review"
            if uncertain
            else "fake_has_no_external_request"
            if run.provider == "fake"
            else "no_external_request",
        }
        run.state = {
            "running": "queued",
            "steer_requested": "queued",
            "pause_requested": "paused",
            "cancel_requested": "cancelled",
        }.get(run.state, run.state)
        if uncertain:
            attempt.checkpoint = {
                **attempt.checkpoint,
                "reconciliation_resume_state": "cancelled"
                if run.state == "cancelled"
                else "paused",
            }
            run.state = "reconciliation_required"
            attempt.state = "reconciliation_required"
        elif run.provider != "fake" and any(r.state == "spent" for r in requests):
            # A paid call completed but its output may have been lost. A restart must
            # not silently purchase it again; explicit resume is required.
            if run.state == "queued":
                run.state = "paused"
        self._emit(
            session,
            run,
            "attempt.reconciled",
            {
                "attempt_id": attempt.id,
                "state": run.state,
                "reason": attempt.checkpoint["reconciliation"],
            },
        )

    def claim(self, session, payload):
        run = self._run(session, payload["run_id"])
        self.agent.branch_allowed(session, run)
        before = run.state
        self._reconcile_expired(session, run)
        if any(self._blocking_unknown(session, row) for row in session.scalars(
            select(ProviderRequest).where(
                ProviderRequest.run_id == run.id, ProviderRequest.state == "unknown"
            )
        )):
            run.state = "reconciliation_required"
        if run.state == "reconciliation_required":
            return 200, {"run_id": run.id, "state": run.state, "effect": "blocked"}
        if before in {"pause_requested", "cancel_requested"} and run.state in {
            "paused",
            "cancelled",
        }:
            return 200, {"run_id": run.id, "state": run.state, "effect": "applied"}
        if run.state != "queued":
            raise DomainError(409, "run_not_queued", "当前运行不能领取", state=run.state)
        from mathagent.persistence.agent_models import AgentRun, AgentStep
        config = session.get(AgentRun, run.id)
        if config and config.autonomous and session.scalar(select(func.count()).select_from(AgentStep).where(
                AgentStep.run_id == run.id)) >= config.options["max_steps"]:
            run.state = "step_limit"
            self._emit(session, run, "run.step_limit", {"run_id": run.id})
            return 200, {"run_id": run.id, "state": run.state, "effect": "blocked"}
        branch = self.service.require_branch(session, run.branch_id)
        self._check_permission(session, branch.project_id, run.provider)
        active = session.scalars(select(Attempt).where(Attempt.state == "running")).all()
        # Pending stop/pause requests still hold a slot until a boundary or expiry.
        if sum(not self._expired(attempt) for attempt in active) >= self.max_active_attempts:
            raise DomainError(409, "concurrency_limit", "本地正在执行的任务已达并发上限")
        read_set = self.service.read_set(session, branch.id)
        read_set = self.agent.initial_read_set(session, run, read_set)
        if run.goal_object_id not in read_set:
            raise DomainError(409, "goal_not_in_branch", "目标已不在当前分支中")
        goal_revision = self.service.require_revision(session, read_set[run.goal_object_id])
        output = (
            self.provider.generate(
                goal=goal_revision.body,
                instruction=run.instruction,
                read_set=read_set,
            )
            if run.provider == "fake"
            else None
        )
        options = session.get(RunOptions, run.id)
        mode = options.mode if options else "research"
        inputs = []
        for object_id, revision_id in read_set.items():
            revision = self.service.require_revision(session, revision_id)
            obj = self.service.require_object(session, object_id)
            inputs.append(
                {
                    "object_id": object_id,
                    "revision_id": revision_id,
                    "kind": obj.kind,
                    "body": revision.body,
                    "payload": revision.payload,
                }
            )
        # Include pinned proof dependencies even when a newer branch head exists.
        # These immutable historical inputs are distinct from the head fence.
        context_revisions = set(read_set.values())
        pending = list(inputs)
        proof_plans = []
        while pending:
            item = pending.pop()
            plan = session.get(ProofPlan, item["revision_id"])
            if plan is None:
                continue
            dependencies = session.scalars(
                select(Dependency).where(Dependency.plan_revision_id == plan.revision_id)
            ).all()
            proof_plans.append(
                {
                    "revision_id": plan.revision_id,
                    "conclusion_revision_id": plan.conclusion_revision_id,
                    "rule": plan.rule,
                    "gaps": plan.gaps,
                    "dependencies": [
                        {
                            "revision_id": d.revision_id,
                            "role": d.role,
                            "origin": d.origin,
                            "scope": d.scope,
                        }
                        for d in dependencies
                    ],
                }
            )
            for revision_id in [
                plan.conclusion_revision_id,
                *(d.revision_id for d in dependencies),
            ]:
                if revision_id in context_revisions:
                    continue
                context_revisions.add(revision_id)
                revision = self.service.require_revision(session, revision_id)
                obj = self.service.require_object(session, revision.object_id)
                extra = {
                    "object_id": obj.id,
                    "revision_id": revision.id,
                    "kind": obj.kind,
                    "body": revision.body,
                    "payload": revision.payload,
                    "historical": True,
                }
                inputs.append(extra)
                pending.append(extra)
        number = (
            session.scalar(select(func.max(Attempt.number)).where(Attempt.run_id == run.id)) or 0
        )
        attempt = Attempt(
            run_id=run.id,
            number=number + 1,
            state="running",
            read_set=read_set,
            control_epoch=run.control_epoch,
            token=secrets.token_urlsafe(32),
            lease_until=(datetime.now(UTC) + timedelta(seconds=self.lease_seconds)).isoformat(),
            checkpoint={
                "boundary": "claimed",
                "instruction": run.instruction,
                "branch_control_epoch": branch.control_epoch,
                "provider": run.provider,
                "mode": mode,
                "simulated": run.provider == "fake",
                "scripted_output": output,
                "context_revision_ids": sorted(context_revisions),
                "target_revision_id": goal_revision.id,
                "proof_plans": proof_plans,
            },
        )
        session.add(attempt)
        session.flush()
        run.current_attempt_id = attempt.id
        run.state = "running"
        self._emit(
            session,
            run,
            "attempt.claimed",
            {
                "attempt_id": attempt.id,
                "number": attempt.number,
                "lease_until": attempt.lease_until,
                "read_set": read_set,
                "control_epoch": attempt.control_epoch,
                "simulated": run.provider == "fake",
            },
        )
        task = {
            "run_id": run.id,
            "state": run.state,
            "attempt_id": attempt.id,
            "attempt_number": attempt.number,
            "token": attempt.token,
            "lease_until": attempt.lease_until,
            "read_set": read_set,
            "control_epoch": attempt.control_epoch,
            "provider": run.provider,
            "simulated": run.provider == "fake",
            "mode": mode,
            "inputs": inputs,
            "goal_object_id": run.goal_object_id,
            "target_revision_id": goal_revision.id,
            "context_revision_ids": sorted(context_revisions),
            "proof_plans": proof_plans,
            "instruction": run.instruction,
            "lease_seconds": self.lease_seconds,
            "scripted_output": output,
        }
        return 201, self.agent.enrich_task(session, task)

    def _candidate_branch(self, session, branch, attempt):
        candidate = Branch(
            project_id=branch.project_id,
            parent_id=branch.id,
            name=f"candidate-{attempt.id}",
            control_epoch=0,
        )
        session.add(candidate)
        session.flush()
        for object_id, revision_id in attempt.read_set.items():
            session.add(Head(branch_id=candidate.id, object_id=object_id, revision_id=revision_id))
        self.service.emit(
            session,
            branch.project_id,
            candidate.id,
            "branch.created",
            {
                "branch_id": candidate.id,
                "parent_id": branch.id,
                "source": "late_attempt",
                "attempt_id": attempt.id,
            },
            author="system",
        )
        return candidate

    def complete(self, session, payload, *, agent_step=False):
        attempt = session.get(Attempt, payload["attempt_id"])
        if attempt is None:
            raise DomainError(404, "attempt_not_found", "执行记录不存在")
        if not secrets.compare_digest(attempt.token, payload["token"]):
            raise DomainError(409, "invalid_execution_token", "执行令牌已失效或不匹配")
        config = session.get(AgentRun, attempt.run_id)
        if config and config.autonomous and config.options.get("completion_policy") == "reviewed_answer" and not agent_step:
            raise DomainError(422, "autonomous_step_required", "此任务必须经研究步骤端点检查完成条件")
        body = payload["body"]
        if not isinstance(body, str) or not body.strip() or len(body) > 200_000:
            raise DomainError(422, "invalid_output", "执行产物应为非空文本，且不超过 200000 字符")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if attempt.output_revision_id:
            if attempt.checkpoint.get("submitted_body_sha256") != digest:
                raise DomainError(409, "attempt_already_completed", "该执行已保存另一份产物")
            return 200, dict(attempt.checkpoint["completion"])
        run = self._run(session, attempt.run_id)
        result = payload.get("result")
        if result is not None:
            try:
                result = validate_result(
                    result,
                    mode=attempt.checkpoint.get("mode", "research"),
                    read_set=attempt.read_set,
                    context_revision_ids=attempt.checkpoint.get("context_revision_ids", []),
                    autonomous=bool(attempt.checkpoint.get("autonomous", False)),
                ).model_dump()
            except (ValueError, TypeError):
                raise DomainError(
                    422, "invalid_structured_output", "产物不符合结构化协议"
                ) from None
            if result["body"] != body:
                raise DomainError(422, "output_body_mismatch", "产物正文与结构化正文不一致")
        requests = session.scalars(
            select(ProviderRequest).where(ProviderRequest.attempt_id == attempt.id)
        ).all()
        if any(r.state in {"reserved", "dispatched", "unknown"} and not self._continued_unknown(r, attempt) for r in requests):
            raise DomainError(409, "request_unsettled", "先结算或对账请求后才能保存产物")
        if run.provider != "fake":
            if result is None:
                raise DomainError(422, "structured_output_required", "真实模型必须提交结构化产物")
            if not any(r.state == "spent" for r in requests):
                raise DomainError(409, "request_unsettled", "先结算或对账外部请求后才能保存产物")
        branch = self.service.require_branch(session, run.branch_id)
        current_read_set = self.service.read_set(session, branch.id)
        changes = {
            object_id: {"expected": revision_id, "current": current_read_set.get(object_id)}
            for object_id, revision_id in attempt.read_set.items()
            if current_read_set.get(object_id) != revision_id
        }
        reasons = []
        if self._expired(attempt):
            reasons.append("lease_expired")
        if run.current_attempt_id != attempt.id:
            reasons.append("superseded_attempt")
        if run.control_epoch != attempt.control_epoch:
            reasons.append("control_epoch_changed")
        if branch.control_epoch != attempt.checkpoint.get(
            "branch_control_epoch", branch.control_epoch
        ):
            reasons.append("branch_control_epoch_changed")
        if changes:
            reasons.append("input_revision_changed")
        if any((head := session.get(Head, (item["branch_id"], item["object_id"]))) is None
               or head.revision_id != item["revision_id"] for item in attempt.checkpoint.get("external_read_set", [])):
            reasons.append("external_input_revision_changed")
        if run.state != "running":
            reasons.append(f"run_{run.state}")
        if attempt.state != "running":
            reasons.append(f"attempt_{attempt.state}")
        quarantined = bool(reasons)
        output_branch = self._candidate_branch(session, branch, attempt) if quarantined else branch
        _, revision = self.service.new_object(
            session,
            output_branch,
            "artifact",
            body,
            {
                "artifact_type": "draft",
                "provider": run.provider,
                "simulated": run.provider == "fake",
                "evidence_kind": "simulation" if run.provider == "fake" else "model_output",
                "mode": attempt.checkpoint.get("mode", "research"),
                "structured_result": result,
                "run_id": run.id,
                "attempt_id": attempt.id,
                "input_revisions": attempt.read_set,
                "target_revision_id": attempt.checkpoint.get("target_revision_id"),
                "proof_plans": attempt.checkpoint.get("proof_plans", []),
                "candidate": True,
                "quarantined": quarantined,
                "quarantine_reasons": reasons,
            },
            author=run.provider,
        )
        self.service._add_reference(session, output_branch.id, revision.id)
        review_id = None
        if run.provider != "fake" and result and result["mode"] == "review" and not quarantined:
            target_revision_id = attempt.checkpoint["target_revision_id"]
            refs = {**attempt.read_set, run.goal_object_id: target_revision_id}
            for plan in attempt.checkpoint.get("proof_plans", []):
                if plan["revision_id"] != target_revision_id:
                    continue
                for ref_id in [
                    plan["conclusion_revision_id"],
                    *(d["revision_id"] for d in plan["dependencies"]),
                ]:
                    ref = self.service.require_revision(session, ref_id)
                    refs[ref.object_id] = ref.id
            review = Review(
                project_id=branch.project_id,
                target_revision_id=target_revision_id,
                kind="llm_review",
                verdict=result["verdict"],
                coverage="partial",
                scope=result["scope"],
                findings=result["findings"],
                dependency_snapshot=refs,
                author=run.provider,
            )
            session.add(review)
            session.flush()
            review_id = review.id
            self._emit(
                session,
                run,
                "review.recorded",
                {
                    "review_id": review.id,
                    "target_revision_id": target_revision_id,
                    "attempt_id": attempt.id,
                    "artifact_revision_id": revision.id,
                    "coverage": "partial",
                    "provenance": "model_output",
                    "dependency_snapshot": refs,
                },
                run.provider,
            )
        attempt.output_revision_id = revision.id
        attempt.state = "quarantined" if quarantined else "completed"
        if run.current_attempt_id == attempt.id and run.state in {
            "running",
            "pause_requested",
            "cancel_requested",
            "steer_requested",
        }:
            run.state = {
                "pause_requested": "paused",
                "cancel_requested": "cancelled",
                "steer_requested": "queued",
            }.get(run.state, "interrupted" if quarantined else "completed")
        response = {
            "run_id": run.id,
            "state": run.state,
            "attempt_id": attempt.id,
            "attempt_state": attempt.state,
            "output_revision_id": revision.id,
            "review_id": review_id,
            "output_branch_id": output_branch.id,
            "quarantined": quarantined,
            "reasons": reasons,
            "changed_inputs": changes,
            "simulated": run.provider == "fake",
        }
        attempt.checkpoint = {
            **attempt.checkpoint,
            "boundary": "completed",
            "completion": response,
            "submitted_body_sha256": digest,
            "end_reason": reasons or ["completed"],
        }
        self._emit(session, run, "attempt.completed", response, run.provider)
        return 200, response

    def intervene(self, session, payload):
        run = self._run(session, payload["run_id"])
        action = payload["action"]
        if action not in {"pause", "cancel", "steer"}:
            raise DomainError(422, "invalid_intervention", "不支持的干预动作")
        if run.state in {"completed", "cancelled"}:
            raise DomainError(409, "run_terminal", "已结束的运行不能干预", state=run.state)
        if run.state == "cancel_requested" and action != "cancel":
            raise DomainError(409, "cancel_pending", "停止请求已登记，等待当前执行结束")
        if action == "steer" and not payload.get("instruction", "").strip():
            raise DomainError(422, "instruction_required", "引导操作必须提供新指令")
        self._reconcile_expired(session, run)
        if run.state == "cancelled":
            return 200, {"run_id": run.id, "state": run.state, "effect": "applied"}
        active = session.get(Attempt, run.current_attempt_id) if run.current_attempt_id else None
        in_flight = active is not None and active.state == "running" and not self._expired(active)
        run.control_epoch += 1
        if action == "steer":
            run.instruction = payload["instruction"]
            # Steering a paused run changes its next inputs but does not resume it.
            if in_flight and run.state != "pause_requested":
                run.state = "steer_requested"
        elif action == "pause":
            run.state = "pause_requested" if in_flight else "paused"
        else:
            run.state = "cancel_requested" if in_flight else "cancelled"
        response = {
            "run_id": run.id,
            "state": run.state,
            "action": action,
            "effect": "pending" if in_flight else "applied",
            "effective_at": "attempt_boundary" if in_flight else "immediate",
            "control_epoch": run.control_epoch,
            "in_flight": in_flight,
            "external_request_in_flight": bool(
                session.scalar(
                    select(ProviderRequest.id).where(
                        ProviderRequest.run_id == run.id, ProviderRequest.state == "dispatched"
                    )
                )
            ),
        }
        event = self._emit(session, run, "run.intervention", response, "human")
        return 202 if in_flight else 200, {**response, "intervention_id": event.id}

    def resume(self, session, payload):
        run = self._run(session, payload["run_id"])
        self._reconcile_expired(session, run)
        unsettled = session.scalars(
            select(ProviderRequest).where(
                ProviderRequest.run_id == run.id,
                ProviderRequest.state.in_(["unknown", "dispatched"]),
            )
        )
        if any(row.state == "dispatched" or self._blocking_unknown(session, row) for row in unsettled):
            return 200, {"run_id": run.id, "state": "reconciliation_required", "effect": "blocked"}
        if run.state == "queued":
            return 200, {"run_id": run.id, "state": run.state}
        if run.state not in {"paused", "interrupted", "failed", "budget_exhausted", "step_limit"}:
            raise DomainError(409, "run_not_resumable", "当前运行不能恢复", state=run.state)
        run.state = "queued"
        run.control_epoch += 1
        run.current_attempt_id = None
        response = {"run_id": run.id, "state": run.state, "control_epoch": run.control_epoch}
        self._emit(session, run, "run.resumed", response, "human")
        return 200, response
