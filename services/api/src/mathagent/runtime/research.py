"""Continuous mathematical work, with persistent peers and model-directed tasks.

This module schedules and stores research. It never evaluates whether a proof is
correct, whether a reviewer agreed, or whether the prose looks complete.
"""

import copy
import json
from datetime import UTC, datetime

from mathagent.application.code_execution import permitted_operations
from mathagent.application.errors import DomainError
from mathagent.application.state import record
from mathagent.persistence.agent_models import AgentRun, AgentStep, ProviderCall
from mathagent.persistence.models import (
    Attempt,
    Head,
    Project,
    ResearchObject,
    Revision,
    RevisionParent,
    Run,
    uid,
)
from mathagent.persistence.runtime_models import ProviderRequest, RunOptions
from mathagent.persistence.solver_models import (
    ResearchMember,
    ResearchMessage,
    ResearchSession,
    ResearchWork,
)
from mathagent.providers import research
from mathagent.providers.observability import usage_summary
from mathagent.tools.code_sandbox import CodeSandbox
from sqlalchemy import func, or_, select

OPEN_WORK = {"queued", "running", "waiting"}


class ResearchRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
        self.state = runtime.service

    def session_for(self, session, run_id):
        member = session.get(ResearchMember, run_id)
        return session.get(ResearchSession, member.root_run_id) if member else None

    def inputs_stale(self, session, run, attempt):
        root = self.session_for(session, run.id)
        if not root:
            return False
        lead = session.get(Run, root.root_run_id)
        member = session.get(ResearchMember, run.id)
        original = session.get(Revision, root.goal_revision_id)
        head = session.get(Head, (lead.branch_id, original.object_id))
        backgrounds_stale = any(
            not (given_head := session.get(Head, (lead.branch_id, session.get(Revision, given["revision_id"]).object_id)))
            or given_head.revision_id != given["revision_id"] for given in root.config.get("background", []))
        return (backgrounds_stale or root.state != "researching" or (member.name != "lead" and not root.config["discussion"])
                or not head or head.revision_id != original.id
                or lead.control_epoch != attempt.checkpoint.get("research_root_epoch", lead.control_epoch))

    def initialize(self, session, run, payload):
        if not payload.get("autonomous") or payload.get("mode", "research") != "research":
            return
        if payload.get("solver_controller") != "continuous_research":
            return
        goal = session.get(Head, (run.branch_id, run.goal_object_id)).revision_id
        prior = session.scalar(select(ResearchSession).join(Run, Run.id == ResearchSession.root_run_id)
            .where(ResearchSession.goal_revision_id == goal, Run.branch_id == run.branch_id)
            .order_by(ResearchSession.created_at.desc()).limit(1))
        contexts = session.scalars(select(Revision).join(Head, Head.revision_id == Revision.id)
            .join(ResearchObject, ResearchObject.id == Revision.object_id).where(
                Head.branch_id == run.branch_id, ResearchObject.kind == "context",
                ResearchObject.id != run.goal_object_id).order_by(ResearchObject.created_at)).all()
        background = [{"ref": "background-" + str(index + 1), "revision_id": revision.id}
                      for index, revision in enumerate(contexts) if not revision.payload.get("deleted")]
        row = ResearchSession(root_run_id=run.id, goal_revision_id=goal,
            config={"discussion": payload.get("discussion", True),
                    "deadline_seconds": payload.get("research_deadline_seconds", 86400),
                    "max_researchers": payload.get("max_researchers", 4),
                    "literature": payload.get("literature", True),
                    "background": background})
        session.add(row)
        session.flush()
        session.add(ResearchMember(run_id=run.id, root_run_id=run.id, name="lead"))
        session.flush()
        if prior:
            prior_lead = session.get(ResearchMember, prior.root_run_id)
            for scope, object_id in (("shared", prior.shared_note_id),
                                     ("personal", prior_lead.personal_note_id)):
                head = session.get(Head, (run.branch_id, object_id)) if object_id else None
                old = session.get(Revision, head.revision_id) if head else None
                if old and not old.payload.get("deleted"):
                    copied = self._save(session, run, old.body, kind=scope, title="接续先前研究工作稿")
                    if scope == "shared":
                        row.shared_note_id = copied.object_id
                    else:
                        session.get(ResearchMember, run.id).personal_note_id = copied.object_id
        self._work(session, row, run.id,
            "研究原题，建立完整推导。需要时计算、查阅材料、调整方法或邀请同伴；"
            "有值得单独研究的困难部分时再安排局部任务。")

    def _work(self, session, root, member_id, goal, *, independent=False, materials=()):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("A work item needs a concrete goal")
        run = session.get(Run, member_id)
        self._clear_wait(root, member_id)
        for previous in session.scalars(select(ResearchWork).where(
            ResearchWork.member_run_id == member_id, ResearchWork.state.in_(OPEN_WORK))):
            if previous.state == "waiting" or run.state == "failed":
                previous.state = "replaced"
        work = ResearchWork(root_run_id=root.root_run_id, member_run_id=member_id,
            goal=goal, independent=bool(independent), materials=list(materials))
        session.add(work)
        session.flush()
        if run.state in {"idle", "waiting_discussion", "completed", "failed"}:
            run.state, run.current_attempt_id = "queued", None
        self.runtime._emit(session, run, "research.work_assigned", record(work))
        return work

    def work_for(self, session, run_id):
        return session.scalar(select(ResearchWork).where(
            ResearchWork.member_run_id == run_id, ResearchWork.state.in_(OPEN_WORK))
            .order_by(ResearchWork.created_at, ResearchWork.id))

    def _peer(self, session, root, name="peer"):
        if not root.config["discussion"]:
            raise ValueError("Discussion is disabled for this run")
        peer = session.scalar(select(ResearchMember).where(
            ResearchMember.root_run_id == root.root_run_id, ResearchMember.name == name))
        if peer:
            return peer
        count = session.scalar(select(func.count()).select_from(ResearchMember).where(
            ResearchMember.root_run_id == root.root_run_id))
        if count >= root.config.get("max_researchers", 4):
            raise ValueError("The configured research team is full; reuse an existing member")
        lead = session.get(Run, root.root_run_id)
        config = session.get(AgentRun, lead.id)
        run = Run(branch_id=lead.branch_id, goal_object_id=lead.goal_object_id,
                  provider=lead.provider, instruction="研究交给你的局部问题。", state="idle")
        session.add(run)
        session.flush()
        session.add(RunOptions(run_id=run.id, mode="research",
                               request_budget=1000))
        self.runtime.agent.register(session, run.id,
            {**config.options, "autonomous": True}, parent=config)
        peer = ResearchMember(run_id=run.id, root_run_id=lead.id, name=name)
        session.add(peer)
        session.flush()
        self.runtime._emit(session, lead, "research.member_created", record(peer))
        return peer

    def _target(self, session, root, member, name, *, create=False):
        name = name.strip()
        if name == "self":
            return member
        # Keep the original two-member shorthand usable in saved conversations.
        if name == "peer" and member.name != "lead":
            name = "lead"
        if not name:
            raise ValueError("Give the research member a name")
        target = session.scalar(select(ResearchMember).where(
            ResearchMember.root_run_id == root.root_run_id, ResearchMember.name == name))
        if target:
            return target
        if create:
            return self._peer(session, root, name)
        raise ValueError("Assign work to this member before sending a message")

    def _save(self, session, run, body, *, kind="material", title="", object_id=None):
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Material needs a nonempty body")
        if not isinstance(title, str):
            raise ValueError("A material title must be text")
        member = session.get(ResearchMember, run.id)
        branch = self.state.require_branch(session, run.branch_id)
        payload = {"artifact_type": "research_" + kind, "research_root_id": member.root_run_id,
                   "research_member_id": run.id, "title": title, "run_id": run.id,
                   "goal_revision_id": self.session_for(session, run.id).goal_revision_id}
        if object_id:
            head = session.get(Head, (branch.id, object_id))
            old = session.get(Revision, head.revision_id)
            payload["ref"] = old.payload["ref"]
            revision = Revision(object_id=object_id, body=body, payload=payload, author=run.provider)
            session.add(revision)
            session.flush()
            session.add(RevisionParent(revision_id=revision.id, parent_id=old.id))
            head.revision_id = revision.id
        else:
            count = session.scalar(select(func.count()).select_from(Revision).where(
                Revision.payload["research_root_id"].as_string() == member.root_run_id)) or 0
            payload["ref"] = "material-" + str(count + 1)
            _, revision = self.state.new_object(session, branch, "artifact", body, payload, run.provider)
        self.state._add_reference(session, branch.id, revision.id)
        self.runtime._emit(session, run, "research.material_saved", {
            "ref": payload["ref"], "revision_id": revision.id, "kind": kind, "title": title})
        return revision

    def _list_materials(self, session, run, work, values):
        root = self.session_for(session, run.id)
        query = select(Revision).join(Head, Head.revision_id == Revision.id).where(
            Head.branch_id == run.branch_id,
            Revision.payload["goal_revision_id"].as_string() == root.goal_revision_id)
        if not values["include_previous"]:
            query = query.where(Revision.payload["research_root_id"].as_string() == root.root_run_id)
        if work.independent:
            query = query.where(Revision.payload["research_member_id"].as_string() == run.id)
        if values["query"]:
            query = query.where(or_(Revision.body.contains(values["query"], autoescape=True),
                Revision.payload["title"].as_string().contains(values["query"], autoescape=True)))
        query = query.where(or_(Revision.payload["deleted"].as_boolean().is_(None),
                               Revision.payload["deleted"].as_boolean().is_(False)))
        rows = session.scalars(query.order_by(Revision.created_at.desc(), Revision.id)
            .offset(values["offset"]).limit(values["limit"] + 1)).all()
        return {"materials": [{"ref": "revision:" + rev.id, "title": rev.payload.get("title", ""),
                "kind": rev.payload.get("artifact_type"), "characters": len(rev.body),
                "previous_research": rev.payload.get("research_root_id") != root.root_run_id}
                for rev in rows[:values["limit"]]],
                "next_offset": values["offset"] + values["limit"] if len(rows) > values["limit"] else None}

    def _material(self, session, run, work, ref):
        root = self.session_for(session, run.id)
        member = session.get(ResearchMember, run.id)
        if ref == "original":
            return session.get(Revision, root.goal_revision_id)
        given = next((given for given in root.config.get("background", []) if given["ref"] == ref), None)
        if given:
            revision = session.get(Revision, given["revision_id"])
            if revision.payload.get("deleted"):
                raise ValueError("Material is unavailable")
            return revision
        note_id = member.personal_note_id if ref == "personal_note" else root.shared_note_id if ref == "shared_note" else None
        if note_id:
            revision = session.get(Revision, session.get(Head, (run.branch_id, note_id)).revision_id)
        elif ref.startswith("revision:"):
            revision = session.scalar(select(Revision).join(
                ResearchObject, ResearchObject.id == Revision.object_id).where(
                Revision.id == ref.removeprefix("revision:"),
                ResearchObject.project_id == self.state.require_branch(session, run.branch_id).project_id,
                Revision.payload["goal_revision_id"].as_string() == root.goal_revision_id))
            head = session.get(Head, (run.branch_id, revision.object_id)) if revision else None
            if not head or session.get(Revision, head.revision_id).payload.get("deleted"):
                raise ValueError("Material is unavailable on this branch")
        else:
            revision = session.scalar(select(Revision).join(
                ResearchObject, ResearchObject.id == Revision.object_id).where(
                ResearchObject.project_id == self.state.require_branch(session, run.branch_id).project_id,
                Revision.payload["research_root_id"].as_string() == root.root_run_id,
                Revision.payload["ref"].as_string() == ref).order_by(Revision.created_at.desc()))
        if not revision or revision.payload.get("deleted"):
            raise ValueError("Material is unavailable")
        if work.independent and revision.payload.get("research_member_id") != run.id:
            raise ValueError("This independent work receives the original problem and your own material first")
        return revision

    def _opening(self, session, run, work):
        root = self.session_for(session, run.id)
        original = session.get(Revision, root.goal_revision_id)
        parts = ["原题：\n" + original.body, "当前任务：\n" + work.goal]
        balance = self._balance(session, run)
        parts.append("资源余额（这份工作稿建立时）：\n" + json.dumps(balance, ensure_ascii=False))
        if run.id == root.root_run_id and run.instruction:
            parts.append("用户的研究要求：\n" + run.instruction)
        if root.config.get("background"):
            parts.append("原题所在分支已有的上下文，可按名称读取，不是其他研究者的审查意见：\n" +
                "\n".join(given["ref"] + "：" + session.get(Revision, given["revision_id"]).body[:160]
                          for given in root.config["background"]))
        try:
            parts.append("你的工作稿：\n" + self._material(session, run, work, "personal_note").body)
        except ValueError:
            pass
        if work.independent:
            parts.append("这是独立探索。先形成自己的推导或尝试，再结束任务交换材料。")
        else:
            for ref, label in (("shared_note", "公共工作稿"),):
                try:
                    parts.append(label + "：\n" + self._material(session, run, work, ref).body)
                except ValueError:
                    pass
            for ref in work.materials:
                try:
                    parts.append("任务材料 " + ref + "：\n" + self._material(session, run, work, ref).body)
                except ValueError:
                    parts.append("任务材料 " + ref + " 不存在，可先读取材料目录再继续。")
        directory = session.scalars(select(Revision).join(Head, Head.revision_id == Revision.id).where(
            Head.branch_id == run.branch_id,
            Revision.payload["research_root_id"].as_string() == root.root_run_id)
            .order_by(Revision.created_at.desc()).limit(100)).all()
        visible = [rev for rev in directory if not work.independent
                   or rev.payload.get("research_member_id") == run.id]
        visible = [rev for rev in visible if not rev.payload.get("deleted")]
        if visible:
            parts.append("可按名称读取完整材料：\n" + "\n".join(
                rev.payload["ref"] + "：" + rev.payload.get("title", "") for rev in visible))
        parts.append("你是" + ("主研究者，负责组织研究与交付。" if run.id == root.root_run_id
                                else "研究同伴，可以提出推导、问题和不同方法。"))
        if not work.independent and root.config["discussion"]:
            parts.append(self._roster_text(session, root))
        parts.append("材料目录支持查找本题先前研究的成果；完整材料按需读取，不必重做所有探索。")
        return {"role": "user", "content": "\n\n".join(parts)}

    @staticmethod
    def _roster(session, root):
        return {"names": list(session.scalars(select(ResearchMember.name).where(
            ResearchMember.root_run_id == root.root_run_id).order_by(ResearchMember.name))),
            "maximum": root.config.get("max_researchers", 4)}

    def _roster_text(self, session, root):
        roster = self._roster(session, root)
        return ("研究成员：" + "、".join(roster["names"]) + "。可给新成员取名并安排任务；总人数最多 "
                + str(roster["maximum"]) + "（含主研究者）。")

    def enrich(self, session, task):
        run = session.get(Run, task["run_id"])
        attempt = session.get(Attempt, task["attempt_id"])
        root = self.session_for(session, run.id)
        work = self.work_for(session, run.id)
        if not work:
            raise DomainError(409, "no_research_work", "当前成员没有待研究任务。")
        if work.state == "queued":
            work.state = "running"
        checkpoint = copy.deepcopy(attempt.checkpoint)
        checkpoint.setdefault("research_root_epoch", session.get(Run, root.root_run_id).control_epoch)
        if "research_dialogue" not in checkpoint:
            prior = session.scalar(select(Attempt).where(
                Attempt.run_id == run.id, Attempt.id != attempt.id).order_by(Attempt.number.desc()).limit(1))
            if prior and prior.checkpoint.get("research_dialogue"):
                checkpoint["research_dialogue"] = copy.deepcopy(prior.checkpoint["research_dialogue"])
            elif prior and prior.checkpoint.get("research_work_id") == work.id:
                for key in ("research_work_id", "research_opening", "research_deliveries"):
                    if key in prior.checkpoint:
                        checkpoint[key] = copy.deepcopy(prior.checkpoint[key])
        project = session.get(Project, self.state.require_branch(session, run.branch_id).project_id)
        settings = project.policies.get("code_sandbox", {})
        options = self.runtime.agent.options(session, run.id)
        member = session.get(ResearchMember, run.id)
        tools = research.tools_for(
            compute=settings.get("enabled", False) and "run_code" in permitted_operations(project),
            discussion=root.config["discussion"] and not work.independent,
            lead=member.name == "lead", literature=root.config.get("literature", True))
        fixed = {"tools": tools, "independent": work.independent,
                 "thinking_mode": options["thinking_mode"], "reasoning_effort": options["reasoning_effort"]}
        dialogue = checkpoint.get("research_dialogue")
        reopen = checkpoint.pop("research_reopen_reason", None)
        if dialogue and dialogue["fixed"] != fixed:
            reopen = "information_scope_changed" if dialogue["fixed"]["independent"] != work.independent else "configuration_changed"
        if not dialogue or reopen:
            known_capacity = dialogue.get("capacity") if dialogue else None
            dialogue = {"id": uid(), "opening": self._opening(session, run, work),
                "system": research.INSTRUCTION, "prompt_version": research.PROMPT_VERSION,
                "fixed": fixed, "work_id": work.id, "roster": self._roster(session, root),
                "events": [], "reason": reopen or "initial", "capacity": known_capacity,
                "balance": self._balance(session, run), "balance_step": 0}
            if reopen == "context_capacity":
                latest = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id,
                    AgentStep.output_revision_id.is_not(None)).order_by(AgentStep.number.desc()).limit(1))
                if latest:
                    ref = session.get(Revision, latest.output_revision_id).payload["ref"]
                    dialogue["opening"]["content"] += ("\n\n上一段对话已接近提供方容量，完整历史没有删除。"
                        "最近推导已保存为 " + ref + "，需要时读回；利用工作稿和已有推导接续，不要从头重做原题。")
            # Read old saved research without rewriting the old steps or calls.
            if not reopen and checkpoint.get("research_work_id") == work.id and checkpoint.get("research_opening"):
                dialogue.update(opening=checkpoint["research_opening"], legacy_work_id=work.id,
                    events=[{"message_id": item["id"], "after_step": item["after_step"]}
                            for item in checkpoint.get("research_deliveries", [])], reason="saved_history_upgrade")
        rows = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)
                               .order_by(AgentStep.number)).all()
        rows = [row for row in rows if row.receipt.get("research_dialogue_id") == dialogue["id"]
                or (not row.receipt.get("research_dialogue_id") and dialogue.get("legacy_work_id")
                    and row.receipt.get("research_work_id") == dialogue["legacy_work_id"])]
        after = rows[-1].number if rows else 0
        if dialogue["work_id"] != work.id:
            content = "当前任务更新：\n" + work.goal
            if not work.independent:
                for ref in work.materials:
                    try:
                        content += "\n任务材料 " + ref + "：\n" + self._material(session, run, work, ref).body
                    except ValueError:
                        content += "\n任务材料 " + ref + " 不存在，可先读取材料目录。"
            dialogue["events"].append({"after_step": after, "content": content})
            dialogue["work_id"] = work.id
        roster = self._roster(session, root)
        if not work.independent and root.config["discussion"] and dialogue["roster"] != roster:
            dialogue["events"].append({"after_step": after, "content": self._roster_text(session, root)})
            dialogue["roster"] = roster
        if not work.independent:
            for message in session.scalars(select(ResearchMessage).where(
                ResearchMessage.recipient_run_id == run.id, ResearchMessage.delivered_work_id.is_(None))
                .order_by(ResearchMessage.created_at, ResearchMessage.id)):
                dialogue["events"].append({"message_id": message.id, "after_step": after})
                message.delivered_work_id = work.id
        if dialogue.get("balance_step") != after and rows:
            receipts = list(rows[-1].receipt.get("tool_results", {}).values())
            if receipts:
                dialogue["balance"] = receipts[-1].get("resource_balance_at_receipt", dialogue.get("balance"))
        balance = self._balance(session, run)
        if dialogue.get("balance") != balance:
            dialogue["events"].append({"after_step": after,
                "content": "当前共享资源余额：\n" + json.dumps(balance, ensure_ascii=False)})
        dialogue.update(balance=balance, balance_step=after)
        conversation = [copy.deepcopy(dialogue["opening"])]

        def deliver(after):
            for item in dialogue["events"]:
                if item["after_step"] != after:
                    continue
                if "content" in item:
                    conversation.append({"role": "user", "content": item["content"]})
                else:
                    message = session.get(ResearchMessage, item["message_id"])
                    revision = session.get(Revision, message.revision_id)
                    body = revision.body
                    conversation.append({"role": "user", "content":
                        "话题：" + message.topic +
                        ("\n运行状态说明：\n" if revision.author == "runtime" else "\n对方的推导或问题：\n") + body})

        deliver(0)
        for row in rows:
            conversation.append(copy.deepcopy(row.receipt["message"]))
            for call in row.receipt["message"].get("tool_calls", []):
                value = row.receipt.get("tool_results", {}).get(call["id"])
                if value is not None:
                    conversation.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(value, ensure_ascii=False)})
            if row.receipt.get("truncated"):
                conversation.append({"role": "user", "content":
                    "上一次输出达到单次长度上限。已经保留其中的推导；未完成的工具请求没有执行。"
                    "从已有工作继续当前局部任务，无需从头研究整题。及时保存阶段成果、"
                    "安排下一任务或提交解答；不要把这段未完成的输出当作完整解答。"})
            elif not row.receipt["message"].get("tool_calls"):
                conversation.append({"role": "user", "content":
                    "阶段推导已保存。继续研究，按需使用工具；完成原题请明确提交，"
                    "完成同伴任务请明确结束，尚未完成则继续推导。"})
            deliver(row.number)
        previous = session.get(ProviderCall, rows[-1].request_id) if rows else None
        capacity = ((previous.call_config or {}).get("research_context") or {}).get("capacity") if previous else dialogue.get("capacity")
        estimate = None
        if capacity and previous and all(type(previous.usage.get(key)) is int for key in ("prompt_tokens", "completion_tokens")):
            count = previous.call_config["research_context"].get("input_messages", 0)
            # Reported tokens cover the previous input and response. New tool
            # results/notices use a conservative byte estimate, not a tokenizer.
            estimate = previous.usage["prompt_tokens"] + previous.usage["completion_tokens"] + sum(
                len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in conversation[count:])
            if estimate + options["max_output_tokens"] >= capacity and not dialogue.get("capacity_notice"):
                notice = ("上下文容量说明：提供方容量为 " + str(capacity) + " 个词元；当前输入粗估约 " + str(estimate)
                    + "，新增材料按字节保守估算，不能当作精确计数。请及时保存接续工作稿，必要时整理对话，"
                    "为继续生成留出空间。这不是整题资源用尽，不必结束研究。")
                dialogue["events"].append({"after_step": after, "content": notice})
                dialogue["capacity_notice"] = True
                conversation.append({"role": "user", "content": notice})
                estimate += len(notice.encode("utf-8"))
        elif capacity:
            estimate = len(json.dumps([dialogue["system"], tools, conversation], ensure_ascii=False).encode("utf-8"))
        dialogue["capacity"] = capacity
        if capacity and estimate is not None and estimate >= capacity and rows:
            # A physical provider boundary, not a fixed research/round limit.
            # Reopen with existing worknotes and a pointer to the latest complete
            # material; no extra summarizer call, and no mathematics is deleted.
            checkpoint.update(research_work_id=work.id, research_dialogue=dialogue,
                              research_reopen_reason="context_capacity")
            attempt.checkpoint = checkpoint
            return self.enrich(session, task)
        checkpoint.update(research_work_id=work.id, research_dialogue=dialogue)
        checkpoint["autonomous"] = True
        attempt.checkpoint = checkpoint
        budget = self.runtime.agent.output_token_budget_status(session, run)
        cap = min(options["max_output_tokens"], budget["remaining_output_tokens"]) if budget["enabled"] else options["max_output_tokens"]
        if capacity and estimate is not None:
            cap = min(cap, max(1, capacity - estimate))
        return {**task, **options, "research_protocol": True, "research_work_id": work.id,
                "research_member": member.name, "research_dialogue_id": dialogue["id"],
                "research_reset_reason": dialogue["reason"] if not rows else None,
                "research_system": dialogue["system"], "research_prompt_version": dialogue["prompt_version"],
                "previous_research_call": previous.call_config if previous else None,
                "context_capacity": capacity, "context_input_estimate": estimate,
                "conversation": conversation, "tools": copy.deepcopy(dialogue["fixed"]["tools"]),
                "max_output_tokens": cap, "request_budget_status": self.runtime.agent.request_budget_status(session, run),
                "pending_research_tools": self.pending_tools(session, run.id)}

    def pending_tools(self, session, run_id):
        result = []
        for row in session.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.number)):
            for call in row.receipt.get("message", {}).get("tool_calls", []):
                if call["id"] not in row.receipt.get("tool_results", {}):
                    result.append({"request_id": row.request_id, "call_id": call["id"]})
        return result

    def apply_turn(self, session, payload):
        attempt, run = self.runtime._attempt(session, payload)
        old = session.scalar(select(AgentStep).where(AgentStep.request_id == payload["request_id"]))
        if old:
            return 200, {"step_id": old.id, "pending_tools": self.pending_tools(session, run.id)}
        request = session.get(ProviderRequest, payload["request_id"])
        if not request or request.attempt_id != attempt.id or request.state != "spent":
            raise DomainError(409, "request_unsettled", "先结算本次模型请求再保存输出。")
        truncated = payload["result"].get("truncated") is True
        message = research.parse_message(payload["result"]["message"], allow_reasoning_only=truncated)
        work = session.get(ResearchWork, attempt.checkpoint["research_work_id"])
        body = message["content"]
        if truncated and message.get("reasoning_content"):
            body = "未完成的推理片段：\n" + message["reasoning_content"] + ("\n\n已写出的正文：\n" + body if body else "")
        revision = self._save(session, run, body, kind="draft", title=work.goal) if body.strip() else None
        number = (session.scalar(select(func.max(AgentStep.number)).where(AgentStep.run_id == run.id)) or 0) + 1
        row = AgentStep(run_id=run.id, attempt_id=attempt.id, request_id=request.id, number=number,
            state="interrupted" if truncated else "running", body=body, actions=[], output_revision_id=revision.id if revision else None,
            receipt={"research_work_id": work.id,
                     "research_dialogue_id": attempt.checkpoint["research_dialogue"]["id"],
                     "message": message, "tool_results": {}, "truncated": truncated})
        session.add(row)
        session.flush()
        if revision:
            work.output_revision_id = revision.id
            attempt.checkpoint = {**attempt.checkpoint, "last_research_output_id": revision.id}
        self.runtime._emit(session, run, "research.turn_saved", {"step_id": row.id,
            "research_work_id": work.id, "output_revision_id": row.output_revision_id})
        return 201, {"step_id": row.id, "pending_tools": self.pending_tools(session, run.id)}

    def _message(self, session, sender, recipient_id, topic, body, *, runtime_notice=False):
        revision = self._save(session, sender, body, kind="discussion", title=topic)
        if runtime_notice:
            revision.author = "runtime"
        root = self.session_for(session, sender.id)
        message = ResearchMessage(root_run_id=root.root_run_id, sender_run_id=sender.id,
            recipient_run_id=recipient_id, topic=topic, revision_id=revision.id)
        session.add(message)
        session.flush()
        recipient = session.get(Run, recipient_id)
        if root.state != "researching":
            return {"ref": revision.payload["ref"], "topic": topic}
        if recipient.state == "waiting_discussion":
            self._clear_wait(root, recipient_id)
            recipient.state, recipient.current_attempt_id = "queued", None
            current = self.work_for(session, recipient_id)
            if current:
                current.state = "queued"
        if not self.work_for(session, recipient_id):
            self._work(session, root, recipient_id, "围绕话题继续研究并回复：" + topic)
        self.runtime._emit(session, sender, "research.message_sent", record(message))
        return {"ref": revision.payload["ref"], "topic": topic}

    @staticmethod
    def _clear_wait(root, run_id):
        root.config = {**root.config, "waiting_for": {
            key: value for key, value in root.config.get("waiting_for", {}).items() if key != run_id}}

    def member_failed(self, session, run, reason):
        root = self.session_for(session, run.id)
        if root and root.state == "researching" and run.id == root.root_run_id:
            self.stop(session, run, "paused")
            return
        if root and root.state == "researching" and root.config["discussion"] and run.id != root.root_run_id:
            recipients = {root.root_run_id, *(key for key, value in root.config.get("waiting_for", {}).items()
                                            if value["recipient"] == run.id)}
            for recipient in recipients:
                self._message(session, run, recipient, "同伴执行状态",
                    "同伴这次调用未完成，原因：" + reason + "。这不是对数学结论的判断，可调整任务或自行继续。",
                    runtime_notice=True)

    def discussion_closed(self, session, root):
        lead = session.get(Run, root.root_run_id)
        if root.state == "researching" and lead.state == "waiting_discussion":
            self._message(session, lead, lead.id, "研究设置",
                "用户已关闭同伴讨论，请独立继续当前局部任务，无需再等待同伴。",
                runtime_notice=True)

    def _finish_work(self, session, run, work, summary=""):
        root = self.session_for(session, run.id)
        current = session.get(Revision, work.output_revision_id) if work.output_revision_id else None
        if summary and (not current or current.body != summary):
            work.output_revision_id = self._save(session, run, summary, title=work.goal).id
        member = session.get(ResearchMember, run.id)
        if work.independent and not member.personal_note_id and work.output_revision_id:
            revision = self._save(session, run, session.get(Revision, work.output_revision_id).body,
                                  kind="personal", title="独立探索工作稿")
            member.personal_note_id = revision.object_id
        work.state = "completed"
        if run.id != root.root_run_id:
            recipients = {root.root_run_id, *(key for key, value in root.config.get("waiting_for", {}).items()
                                            if value["recipient"] == run.id)}
            for recipient in recipients:
                self._message(session, run, recipient, work.goal,
                    session.get(Revision, work.output_revision_id).body if work.output_revision_id
                    else "局部任务已结束，没有保存研究正文。")
        self._clear_wait(root, run.id)
        self.runtime._emit(session, run, "research.work_completed", record(work))

    def _tool_row(self, session, run, request_id, call_id):
        row = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id,
                                                   AgentStep.request_id == request_id))
        if not row:
            raise DomainError(404, "research_turn_not_found", "工具请求对应的输出不存在。")
        call = next((call for call in row.receipt["message"].get("tool_calls", []) if call["id"] == call_id), None)
        if not call:
            raise DomainError(404, "tool_call_not_found", "工具请求不存在。")
        return row, call

    def _balance(self, session, run):
        output = self.runtime.agent.output_token_budget_status(session, run)
        return {"remaining_requests": self.runtime.agent.request_budget_status(session, run)["remaining"],
                "remaining_output_tokens": output["remaining_output_tokens"] if output["enabled"] else None}

    def _record_tool(self, session, run, row, call, value):
        value = {**value, "resource_balance_at_receipt": self._balance(session, run)}
        row.receipt = {**row.receipt, "tool_results": {**row.receipt["tool_results"], call["id"]: value}}
        row.actions = [*row.actions, {"type": call["function"]["name"],
                      "status": "rejected" if value.get("error") else "completed", "result": value}]
        if len(row.receipt["tool_results"]) == len(row.receipt["message"].get("tool_calls", [])):
            row.state = "completed"
        self.runtime._emit(session, run, "research.tool_completed", {
            "request_id": row.request_id, "call_id": call["id"], "result": value})
        return 200, {"result": value}

    def execute_tool(self, session, payload):
        attempt, run = self.runtime._attempt(session, payload, active=True)
        row, call = self._tool_row(session, run, payload["request_id"], payload["call_id"])
        if call["id"] in row.receipt["tool_results"]:
            return 200, {"result": row.receipt["tool_results"][call["id"]]}
        root = self.session_for(session, run.id)
        if root.state != "researching" or self.runtime._boundary(session, attempt, run):
            return self._record_tool(session, run, row, call, {"error": "research_stopped"})
        work = session.get(ResearchWork, row.receipt["research_work_id"])
        member = session.get(ResearchMember, run.id)
        try:
            values = json.loads(call["function"]["arguments"])
            if not isinstance(values, dict):
                raise ValueError("Tool arguments must be an object")
            kind = call["function"]["name"]
            values = research.arguments_for(kind, values)
            if kind in {"compute", "start_computation", "poll_computation", "cancel_computation"}:
                project = session.get(Project, self.state.require_branch(session, run.branch_id).project_id)
                settings = project.policies.get("code_sandbox", {})
                if not settings.get("enabled") or "run_code" not in permitted_operations(project):
                    raise ValueError("The project has not enabled isolated computation")
                spec = {"project_id": project.id, "image_id": settings.get("image_id"),
                        "execution_id": row.request_id + ":" + call["id"], "operation": kind,
                        "research_root_id": root.root_run_id, **values}
                return 200, {"computation" if kind == "compute" else "external": spec}
            if kind in {"search_literature", "read_literature"}:
                if not root.config.get("literature", True):
                    raise ValueError("Literature access is disabled for this research")
                return 200, {"external": {"operation": kind, **values}}
            if kind == "read_material":
                revision = self._material(session, run, work, values["ref"])
                offset, length = max(0, int(values.get("offset", 0))), min(50000, max(1, int(values.get("length", 50000))))
                value = {"ref": values["ref"], "body": revision.body[offset:offset + length],
                         "next_offset": offset + length if offset + length < len(revision.body) else None}
            elif kind == "list_materials":
                value = self._list_materials(session, run, work, values)
            elif kind == "compact_context":
                revision = self._save(session, run, values["summary"], kind="personal",
                    title="接续研究工作稿", object_id=member.personal_note_id)
                member.personal_note_id = revision.object_id
                for previous in session.scalars(select(ResearchWork).where(
                    ResearchWork.member_run_id == run.id, ResearchWork.state.in_(OPEN_WORK))):
                    previous.state = "replaced"
                self._work(session, root, run.id, work.goal, independent=work.independent,
                           materials=work.materials)
                attempt.checkpoint = {**attempt.checkpoint, "research_reopen_reason": "researcher_compacted"}
                value = {"ref": revision.payload["ref"], "context_reopened": True,
                         "full_history_preserved": True}
            elif kind == "save_note":
                scope = values["scope"]
                if scope not in {"personal", "shared", "material"}:
                    raise ValueError("Unknown note scope")
                if scope == "shared" and member.name != "lead":
                    raise ValueError("Save your contribution as material and send it to the lead to organize the shared note")
                object_id = member.personal_note_id if scope == "personal" else root.shared_note_id if scope == "shared" else None
                revision = self._save(session, run, values["body"], kind=scope, title=values.get("title", scope), object_id=object_id)
                if scope == "personal":
                    member.personal_note_id = revision.object_id
                elif scope == "shared":
                    root.shared_note_id = revision.object_id
                value = {"ref": revision.payload["ref"], "revision_id": revision.id}
            elif kind == "assign_work":
                if not values["goal"].strip():
                    raise ValueError("A work item needs a concrete goal")
                if values["member"] != "self" and (work.independent or not root.config["discussion"]):
                    raise ValueError("Exchange work after finishing the independent attempt")
                if values["member"] == "self":
                    target = member
                    for previous in session.scalars(select(ResearchWork).where(
                        ResearchWork.member_run_id == run.id, ResearchWork.state.in_(OPEN_WORK))):
                        previous.state = "replaced"
                else:
                    target = self._target(session, root, member, values["member"], create=True)
                    if target.run_id == run.id:
                        raise ValueError("Use self when changing your own task")
                independent = values.get("independent", work.independent if target.run_id == run.id else False)
                assigned = self._work(session, root, target.run_id, values["goal"],
                    independent=independent, materials=values.get("materials", []))
                value = {"member": target.name, "task": assigned.goal,
                         "independent": assigned.independent, "materials": assigned.materials}
            elif kind == "send_message":
                if not root.config["discussion"] or work.independent:
                    raise ValueError("Discussion is disabled")
                recipient = self._target(session, root, member, values["recipient"]).run_id
                if recipient == run.id:
                    raise ValueError("Send the message to the other member")
                value = self._message(session, run, recipient, values["topic"], values["body"])
                if values.get("wait"):
                    work.state = "waiting"
                    root.config = {**root.config, "waiting_for": {**root.config.get("waiting_for", {}),
                        run.id: {"recipient": recipient, "topic": values["topic"], "work_id": work.id}}}
            elif kind == "finish_work":
                self._finish_work(session, run, work, values.get("summary", ""))
                value = {"task_finished": True}
            elif kind == "submit_solution":
                if member.name != "lead":
                    raise ValueError("Finish your work and send its result to the lead")
                revision = self._material(session, run, work, values["body_ref"]) if values.get("body_ref") else session.get(Revision, work.output_revision_id)
                if not revision:
                    raise ValueError("Write the solution body or provide a saved body_ref")
                if values["outcome"] not in {"solved", "unresolved"}:
                    raise ValueError("Outcome must be solved or unresolved")
                if values.get("answer") is not None and not isinstance(values["answer"], str):
                    raise ValueError("The submitted answer must be text")
                root.solution_revision_id, root.answer = revision.id, values.get("answer")
                root.outcome, root.state = values["outcome"], "completed"
                work.state = "completed"
                run.state = "completed"
                for other in session.scalars(select(ResearchMember).where(
                    ResearchMember.root_run_id == root.root_run_id, ResearchMember.run_id != run.id)):
                    other_run = session.get(Run, other.run_id)
                    other_run.state = "cancel_requested" if other_run.state == "running" else "completed"
                for unfinished in session.scalars(select(ResearchWork).where(
                    ResearchWork.root_run_id == root.root_run_id, ResearchWork.state.in_(OPEN_WORK))):
                    unfinished.state = "cancelled"
                root.config = {**root.config, "waiting_for": {}}
                value = {"submitted": True, "revision_id": revision.id, "answer": root.answer,
                         "outcome": root.outcome}
            else:
                raise ValueError("Unknown research tool")
        except (ValueError, KeyError, TypeError) as error:
            value = {"error": "tool_arguments", "detail": str(error)[:1000]}
        return self._record_tool(session, run, row, call, value)

    def compute(self, spec):
        """Deliberately outside the database write transaction."""
        return CodeSandbox(self.state.db.path, image_id=spec["image_id"]).execute(
            spec["project_id"], spec["execution_id"], spec["code"], spec["timeout_seconds"])

    def external(self, spec):
        """Slow computation and literature I/O never hold the write transaction."""
        kind = spec["operation"]
        if kind in {"search_literature", "read_literature"}:
            from mathagent.tools.literature import read_paper, search_papers

            return search_papers(spec["query"], spec["limit"]) if kind == "search_literature" else read_paper(spec["url"])
        sandbox = CodeSandbox(self.state.db.path, image_id=spec["image_id"])
        if kind == "start_computation":
            result = sandbox.start(spec["project_id"], spec["execution_id"], spec["code"], spec["timeout_seconds"],
                                   research_root_id=spec["research_root_id"])
            # Human Stop can arrive while Docker is creating the container,
            # outside the write transaction. Do not leave that new job running.
            with self.state.db.sessions() as session:
                root = session.get(ResearchSession, spec["research_root_id"])
                cancelled = not root or root.state == "cancelled"
            if cancelled and result.get("job_id"):
                return sandbox.cancel(spec["project_id"], result["job_id"])
            return result
        if kind == "poll_computation":
            return sandbox.poll(spec["project_id"], spec["job_id"])
        return sandbox.cancel(spec["project_id"], spec["job_id"])

    def cancel_computations(self, *, run_id=None, branch_id=None):
        with self.state.db.sessions() as session:
            if run_id:
                root = self.session_for(session, run_id)
                roots = [root.root_run_id] if root else []
            else:
                roots = session.scalars(select(ResearchSession.root_run_id).join(
                    Run, Run.id == ResearchSession.root_run_id).where(
                    Run.branch_id == branch_id, ResearchSession.state == "cancelled")).all()
            scopes = [(self.state.require_branch(session, session.get(Run, root_id).branch_id).project_id,
                       root_id) for root_id in roots]
        sandbox = CodeSandbox(self.state.db.path)
        return [result for project_id, root_id in scopes
                for result in sandbox.cancel_research(project_id, root_id)]

    def redirect(self, session, run, instruction):
        root = self.session_for(session, run.id)
        if not root:
            return
        self._clear_wait(root, run.id)
        for work in session.scalars(select(ResearchWork).where(
            ResearchWork.member_run_id == run.id, ResearchWork.state.in_(OPEN_WORK))):
            work.state = "replaced"
        self._work(session, root, run.id, instruction)

    def stop(self, session, run, state):
        root = self.session_for(session, run.id)
        if not root or root.state == "completed":
            return
        root.state = state
        for member in session.scalars(select(ResearchMember).where(
            ResearchMember.root_run_id == root.root_run_id)):
            other = session.get(Run, member.run_id)
            if other.id == run.id or other.state in {"completed", "cancelled"}:
                continue
            if other.state == "running":
                other.state = "cancel_requested" if state == "cancelled" else "pause_requested"
            else:
                other.state = "cancelled" if state == "cancelled" else state
        self.runtime._emit(session, session.get(Run, root.root_run_id),
                           "research.stopped", {"reason": state})

    def resume(self, session, run):
        root = self.session_for(session, run.id)
        if not root or run.id != root.root_run_id:
            return
        root.state = "researching"
        root.config = {**root.config, "resumed_at": datetime.now(UTC).isoformat()}
        for member in session.scalars(select(ResearchMember).where(
            ResearchMember.root_run_id == root.root_run_id)):
            other = session.get(Run, member.run_id)
            if other.id != run.id and other.state in {"paused", "interrupted", "budget_exhausted"}:
                other.control_epoch += 1
                other.state = "queued" if self.work_for(session, other.id) else "idle"
                other.current_attempt_id = None

    def recover_saved_response(self, session, run, attempt):
        if not self.runtime._expired(attempt):
            return False
        rows = session.execute(select(ProviderCall, ProviderRequest).join(
            ProviderRequest, ProviderRequest.id == ProviderCall.request_id).where(
                ProviderCall.attempt_id == attempt.id,
                or_(ProviderCall.complete.is_(True), ProviderCall.finish_reason == "length"),
                ProviderRequest.state.in_(["dispatched", "spent"]))).all()
        for call, request in rows:
            if not call.result or "message" not in call.result:
                continue
            if session.scalar(select(AgentStep.id).where(AgentStep.request_id == request.id)):
                continue
            if request.state == "dispatched":
                request.state, request.usage = "spent", call.usage
                request.provider_request_id = call.provider_request_id
            self.apply_turn(session, {"attempt_id": attempt.id, "token": attempt.token,
                                     "request_id": request.id, "result": call.result})
        # Normal lease reconciliation still releases the slot. Resume can replay
        # these saved turns and execute pending tools without buying the response again.
        return False

    def finish_computation(self, session, payload):
        attempt, run = self.runtime._attempt(session, payload)
        row, call = self._tool_row(session, run, payload["request_id"], payload["call_id"])
        if call["id"] in row.receipt["tool_results"]:
            return 200, {"result": row.receipt["tool_results"][call["id"]]}
        kind = call["function"]["name"]
        if kind in {"poll_computation", "cancel_computation"} and payload["result"].get("status") == "running":
            return self._record_tool(session, run, row, call, payload["result"])
        title = "文献材料" if kind in {"search_literature", "read_literature"} else "计算材料"
        revision = self._save(session, run, json.dumps(payload["result"], ensure_ascii=False, indent=2),
                              kind="literature" if title == "文献材料" else "computation", title=title)
        visible = dict(payload["result"])
        for field in ("body", "code", "stdout", "stderr"):
            if isinstance(visible.get(field), str) and len(visible[field]) > 12000:
                visible[field] = visible[field][:12000]
                visible[field + "_truncated"] = True
        # Full source/output is in the material; a long result does not have to
        # occupy every subsequent model prompt.
        return self._record_tool(session, run, row, call,
                                 {"ref": revision.payload["ref"], **visible})

    def continuation(self, session, payload):
        attempt, run = self.runtime._attempt(session, payload)
        root = self.session_for(session, run.id)
        if root.state != "completed" and attempt.state == "running":
            self.runtime._boundary(session, attempt, run)
        work = self.work_for(session, run.id)
        pending = self.pending_tools(session, run.id)
        if root.state == "researching" and attempt.state == "running" and run.state == "running":
            if pending or (work and work.state != "waiting"):
                task = {"run_id": run.id, "attempt_id": attempt.id, "token": attempt.token,
                        "lease_seconds": self.runtime.lease_seconds, "provider": run.provider,
                        "mode": "research", "read_set": attempt.read_set, "instruction": run.instruction,
                        "goal_object_id": run.goal_object_id}
                if not work:
                    # A terminal tool can have sibling calls to acknowledge, but
                    # no further model request is needed after those are stored.
                    return 200, {"continue": False, "pending_tools": pending}
                return 200, {"continue": True, "task": self.enrich(session, task)}
        attempt.state = "completed" if root.state == "completed" or (root.state == "researching" and not pending) else "interrupted"
        attempt.output_revision_id = attempt.checkpoint.get("last_research_output_id")
        if root.state == "completed":
            run.state = "completed"
        elif run.state == "running":
            run.state = "waiting_discussion" if work and work.state == "waiting" else "idle"
        run.current_attempt_id = None
        self.runtime._emit(session, run, "research.yielded", {"state": run.state, "attempt_id": attempt.id})
        return 200, {"continue": False, "state": run.state}

    def tick(self, session):
        for root in session.scalars(select(ResearchSession).where(ResearchSession.state == "researching")):
            if (datetime.now(UTC) - datetime.fromisoformat(root.config.get("resumed_at", root.created_at))).total_seconds() > root.config["deadline_seconds"]:
                lead = session.get(Run, root.root_run_id)
                lead.state = "pause_requested" if lead.state == "running" else "paused"
                self.stop(session, lead, "paused")
                continue
            for member in session.scalars(select(ResearchMember).where(ResearchMember.root_run_id == root.root_run_id)):
                run = session.get(Run, member.run_id)
                current = self.work_for(session, run.id)
                if run.state == "waiting_budget":
                    budget = self.runtime.agent.output_token_budget_status(session, run)
                    if not budget["enabled"] or budget["remaining_output_tokens"] >= 1:
                        run.state = "queued"
                    elif not session.scalar(select(ProviderRequest.id).join(
                        AgentRun, AgentRun.run_id == ProviderRequest.run_id).where(
                        AgentRun.root_run_id == root.root_run_id,
                        ProviderRequest.state.in_(["reserved", "dispatched"])).limit(1)):
                        run.state = "budget_exhausted"
                        self.stop(session, run, "budget_exhausted")
                    continue
                unread = session.scalar(select(ResearchMessage.id).where(
                    ResearchMessage.recipient_run_id == run.id,
                    ResearchMessage.delivered_work_id.is_(None)).limit(1))
                if run.state == "idle" and unread and not current:
                    self._work(session, root, run.id, "阅读新到的话题材料，研究其中的问题并回复。")
                elif run.id == root.root_run_id and run.state == "idle" and not current:
                    self._work(session, root, run.id, "接续原题研究，利用工作稿和已有材料推进证明。")
                if run.state == "waiting_discussion" and unread and current and not current.independent:
                    current.state, run.state = "queued", "queued"
                    self._clear_wait(root, run.id)
            members = session.scalars(select(Run).join(ResearchMember, ResearchMember.run_id == Run.id).where(
                ResearchMember.root_run_id == root.root_run_id)).all()
            if (root.state == "researching" and root.config["discussion"]
                    and any(row.state == "waiting_discussion" for row in members)
                    and not any(row.state in {"queued", "running", "waiting_budget"} for row in members)
                    and not session.scalar(select(ProviderRequest.id).join(AgentRun, AgentRun.run_id == ProviderRequest.run_id)
                        .where(AgentRun.root_run_id == root.root_run_id,
                               ProviderRequest.state.in_(["reserved", "dispatched"])).limit(1))):
                lead = session.get(Run, root.root_run_id)
                self._message(session, lead, lead.id, "等待状态",
                    "当前成员都在等待，已没有成员执行能够产生回复的研究。"
                    "请重新安排任务或自行继续；若仍有后台计算，请查询其结果。", runtime_notice=True)

    def snapshot(self, session, run_id):
        root = self.session_for(session, run_id)
        if not root:
            return None
        def note(object_id):
            head = session.get(Head, (session.get(Run, root.root_run_id).branch_id, object_id)) if object_id else None
            return record(session.get(Revision, head.revision_id)) if head else None

        lead = session.get(Run, root.root_run_id)
        calls = [{"usage": row.usage, "call_config": row.call_config} for row in session.scalars(
            select(ProviderCall).join(Attempt, Attempt.id == ProviderCall.attempt_id)
            .join(ResearchMember, ResearchMember.run_id == Attempt.run_id)
            .where(ResearchMember.root_run_id == root.root_run_id)
            .order_by(ProviderCall.created_at, ProviderCall.request_id))]
        return {"session": record(root), "shared_note": note(root.shared_note_id),
                "solution": record(session.get(Revision, root.solution_revision_id)) if root.solution_revision_id else None,
                "members": [{**record(row), "state": session.get(Run, row.run_id).state,
                             "personal_note": note(row.personal_note_id)} for row in session.scalars(
                    select(ResearchMember).where(ResearchMember.root_run_id == root.root_run_id))],
                "work": [record(row) for row in session.scalars(select(ResearchWork).where(
                    ResearchWork.root_run_id == root.root_run_id).order_by(ResearchWork.created_at))],
                "messages": [{**record(row), "body": session.get(Revision, row.revision_id).body,
                              "runtime_notice": session.get(Revision, row.revision_id).author == "runtime"}
                    for row in session.scalars(select(ResearchMessage).where(
                        ResearchMessage.root_run_id == root.root_run_id).order_by(ResearchMessage.created_at))],
                "budget": self.runtime.agent.request_budget_status(session, lead),
                "output_budget": self.runtime.agent.output_token_budget_status(session, lead),
                "usage_summary": usage_summary(calls)}
