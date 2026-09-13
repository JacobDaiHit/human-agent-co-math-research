"""End-to-end autonomous research proposals over HTTP, using synthetic model outputs."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app
from mathagent.persistence.agent_models import AgentRun
from mathagent.persistence.models import Run
from mathagent.persistence.runtime_models import ProviderRequest
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "agent.sqlite3", token="human", worker_token="worker")
    app.state.database.migrate()
    yield app
    app.state.database.close()


def result(body, actions=(), *, next_action="continue", mode="research", verdict=None):
    return {"result": {"mode": mode, "body": body, "findings": ["仅为工程测试夹具"],
                       "cited_revision_ids": [], "scope": "检查指定版本的局部计算。" if mode == "review" else None,
                       "verdict": verdict, "actions": list(actions), "next_action": next_action},
            "usage": {"total_tokens": 10}, "provider_request_id": "synthetic-request"}


def action(name, **arguments):
    return {"type": name, "arguments": arguments}


class Client:
    def __init__(self, client):
        self.client = client

    async def write(self, path, payload=None, *, method="POST", worker=False, expect=None):
        r = await self.client.request(method, path, json=payload,
               headers={"Authorization": "Bearer worker" if worker else "Bearer human", "Idempotency-Key": str(uuid4())})
        assert r.status_code == expect if expect else r.is_success, r.text
        return r.json()

    async def get(self, path):
        r = await self.client.get(path, headers={"Authorization": "Bearer human"})
        assert r.is_success, r.text
        return r.json()


async def project_and_run(api, **overrides):
    p = await api.write("/projects", {"title": "自主流程夹具", "body": "对实数 $x$，研究 $x^2+1>0$。"})
    r = await api.write("/runs", {"branch_id": p["branch_id"], "goal_object_id": p["object_id"],
                                "autonomous": True, "request_budget": 12, **overrides})
    return p, r


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://127.0.0.1:8000",
                                    headers={"Authorization": "Bearer worker"}) as c:
            await scenario(Client(c), c)
    asyncio.run(run())


def test_agent_research_review_revision_cycle_uses_new_attempts_and_bounded_children(app):
    seen_attempts, review_targets = [], []

    class Script:
        async def generate(self, task):
            seen_attempts.append(task["attempt_id"])
            if task["mode"] == "review":
                review_targets.append(task["target_revision_id"])
                assert all(i["payload"].get("mode") != "review" for i in task["inputs"])
                return result("已检查此版推导；这是模拟审查。", mode="review", verdict="inconclusive", next_action="finish")
            history = task["previous_steps"]
            if len(history) == 0:
                return result("先登记候选命题。", [action("write_draft", kind="claim", body="对实数 $x$，有 $x^2+1>0$。")])
            claim = history[0]["actions"][0]["result"]
            if len(history) == 1:
                return result("读取命题并建立论证。", [action("read_object",object_id=claim["object_id"]),
                    action("propose_proof", conclusion_revision_id=claim["revision_id"],body="$x^2\\ge0$，故 $x^2+1>0$。")])
            proof = history[1]["actions"][1]["result"]
            if len(history) == 2:
                return result("请求独立会话审查此版。", [action("request_review",target_revision_id=proof["revision_id"])],next_action="wait")
            assert task["child_results"] and all(c["state"] == "completed" for c in task["child_results"])
            if len(history) == 3:
                return result("将不等式中间步骤写清。", [action("revise_object",object_id=proof["object_id"],
                    expected_revision_id=proof["revision_id"],body="$x^2\\ge0$，所以 $x^2+1\\ge1>0$。")])
            revised = history[3]["actions"][0]["result"]
            if len(history) == 4:
                return result("对新版本再次审查。", [action("request_review",target_revision_id=revised["revision_id"])],next_action="wait")
            return result("保存候选论证与两次不同版本的审查，等待人工采用。", next_action="finish")

    async def scenario(api, client):
        project, run = await project_and_run(api)
        await HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0).run(once=True)
        snap = await api.get(f"/projects/{project['project_id']}/snapshot")
        parent = next(r for r in snap["runs"] if r["id"] == run["run_id"])
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert parent["state"] == "completed", parent
        assert len(steps) == 6
        assert all(a["status"] == "completed" for s in steps for a in s["actions"]), steps
        assert len(seen_attempts) == 8 and len(set(seen_attempts)) == 8
        assert len(review_targets) == 2 and review_targets[0] != review_targets[1]
        assert len(parent["attempts"]) == 6
        assert parent["attempts"][0]["read_set"] == {project["object_id"]: project["revision_id"]}
        assert all(o["adoption_state"] == "draft" for o in snap["objects"])
        assert all(o["author"] != "human" for o in snap["objects"] if o["id"] != project["object_id"])
        assert (await api.get(f"/projects/{project['project_id']}/budget"))["spent"] == 8
        assert (await api.get(f"/runs/{run['run_id']}/budget"))["spent"] == 8
        calls = (await api.get(f"/runs/{run['run_id']}/calls"))["calls"]
        assert len(calls) == 6 and all(c["raw_text"] and c["raw_sha256"] for c in calls)
    exercise(app, scenario)


@pytest.mark.parametrize("root_limit,child_limit,expected_spent", [(12, 1, 3), (2, 4, 2)])
def test_descendant_requests_obey_every_ancestor_budget(app, root_limit, child_limit, expected_spent):
    class Tree:
        async def generate(self, task):
            if task["instruction"] == "grandchild":
                pytest.fail("A grandchild must not call after the ancestor budget is exhausted")
            if task["previous_steps"]:
                return result("子任务额度已耗尽，保存未解决总结。", next_action="finish")
            instruction = "grandchild" if task["instruction"] == "child" else "child"
            return result("请子任务研究此目标。", [action("spawn_task", goal_object_id=task["goal_object_id"],
                instruction=instruction, request_budget=child_limit)], next_action="wait")

    async def scenario(api, client):
        p, run = await project_and_run(api, request_budget=root_limit)
        await HTTPWorker(client, provider_factory=lambda _: Tree(), fake_delay_seconds=0).run(once=True)
        budget = await api.get(f"/runs/{run['run_id']}/budget")
        assert budget["occupied"] == budget["spent"] == expected_spent
        snap = await api.get(f"/projects/{p['project_id']}/snapshot")
        assert any(r["state"] == "budget_exhausted" for r in snap["runs"])
    exercise(app, scenario)


def test_cumulative_output_budget_caps_worker_before_a_second_provider_dispatch(app):
    seen_caps = []

    class Script:
        async def generate(self, task):
            seen_caps.append(task["max_output_tokens"])
            return {**result("记录一次有已知用量的推理。", next_action="continue"),
                    "usage": {"prompt_tokens": 11, "completion_tokens": 100}}

    async def scenario(api, client):
        project, run = await project_and_run(
            api, cumulative_output_token_budget=300, max_steps=3
        )
        await HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0).run(once=True)
        budget = await api.get(f"/runs/{run['run_id']}/budget")
        tokens = budget["output_token_budget"]
        assert seen_caps == [300]
        assert tokens == {
            "unit": "output_tokens", "enabled": True, "limit": 300,
            "reported_output_tokens": 100, "reported_input_tokens": 11,
            "reserved_output_tokens": 0, "occupied_output_tokens": 100,
            "remaining_output_tokens": 200, "unknown_usage_requests": 0,
            "unknown_outcome_requests": 0, "scope": "root_run_and_descendants",
            "includes_format_repairs": True,
        }
        snap = await api.get(f"/projects/{project['project_id']}/snapshot")
        assert next(item for item in snap["runs"] if item["id"] == run["run_id"])["state"] == "budget_exhausted"
    exercise(app, scenario)


def test_sibling_children_atomically_share_the_root_output_reservation(app):
    async def scenario(api, client):
        project, root = await project_and_run(
            api, cumulative_output_token_budget=300, max_steps=3
        )
        children = []
        for instruction in ("child-a", "child-b"):
            children.append(await api.write("/runs", {
                "branch_id": project["branch_id"], "goal_object_id": project["object_id"],
                "autonomous": True, "instruction": instruction, "request_budget": 3,
            }))
        # The public child-creation action is itself model-driven.  Set up the
        # durable family directly so this regression isolates concurrent budget
        # admission rather than proposal parsing.
        with app.state.database.sessions.begin() as session:
            for child in children:
                config = session.get(AgentRun, child["run_id"])
                config.parent_run_id = root["run_id"]
                config.root_run_id = root["run_id"]
        first = await api.write(f"/runs/{children[0]['run_id']}/claim", worker=True)
        second = await api.write(f"/runs/{children[1]['run_id']}/claim", worker=True)
        first_reservation = await api.write(
            f"/attempts/{first['attempt_id']}/requests",
            {"token": first["token"], "requested_output_tokens": 256}, worker=True,
        )
        assert first_reservation["continue"] is True
        started = await api.write(
            f"/requests/{first_reservation['request_id']}/start", {"token": first["token"]}, worker=True,
        )
        assert started["continue"] is True
        rejected = await api.write(
            f"/attempts/{second['attempt_id']}/requests",
            {"token": second["token"], "requested_output_tokens": 256}, worker=True,
        )
        assert rejected["continue"] is False and rejected["state"] == "budget_exhausted"
        with app.state.database.sessions() as session:
            requests = session.scalars(select(ProviderRequest).where(
                ProviderRequest.run_id.in_([child["run_id"] for child in children])
            )).all()
            assert [(item.run_id, item.state) for item in requests] == [(children[0]["run_id"], "dispatched")]
        budget = (await api.get(f"/runs/{root['run_id']}/budget"))["output_token_budget"]
        assert budget["occupied_output_tokens"] == 256 and budget["remaining_output_tokens"] == 44
    exercise(app, scenario)


def test_older_settings_client_cannot_silently_remove_output_budget(app):
    async def scenario(api, client):
        _, run = await project_and_run(api, cumulative_output_token_budget=1000)
        options = await api.get(f"/runs/{run['run_id']}/options")
        options.pop("run_id")
        options.pop("cumulative_output_token_budget")
        preserved = await api.write(f"/runs/{run['run_id']}/options", options, method="PUT")
        assert preserved["cumulative_output_token_budget"] == 1000
        removed = await api.write(f"/runs/{run['run_id']}/options",
            {**options, "cumulative_output_token_budget": None}, method="PUT")
        assert removed["cumulative_output_token_budget"] is None
    exercise(app, scenario)


def test_global_child_limit_cannot_be_evaded_by_recursion(app):
    class Tree:
        async def generate(self, task):
            return result("尝试派生子任务。", [action("spawn_task", goal_object_id=task["goal_object_id"],
                instruction="child")], next_action="finish")

    async def scenario(api, client):
        p, run = await project_and_run(api, max_children=1, max_steps=2)
        await HTTPWorker(client, provider_factory=lambda _: Tree(), fake_delay_seconds=0).run(once=True)
        snap = await api.get(f"/projects/{p['project_id']}/snapshot")
        assert len(snap["runs"]) == 2
        child = next(r for r in snap["runs"] if r["id"] != run["run_id"])
        steps = (await api.get(f"/runs/{child['id']}/steps"))["steps"]
        assert steps[0]["actions"][0]["error"] == "child_limit"
    exercise(app, scenario)


def test_discussion_is_delivered_across_branches_and_reads_stay_project_local(app):
    async def scenario(api, client):
        p, sender = await project_and_run(api)
        other = await api.write("/branches", {"source_branch_id": p["branch_id"], "name": "recipient"})
        obj = await api.write("/objects", {"branch_id": other["branch_id"], "kind": "context", "body": "条件为 $x>0$。"})
        receiver = await api.write("/runs", {"branch_id": other["branch_id"], "goal_object_id": p["object_id"], "autonomous": True})
        await api.write(f"/runs/{receiver['run_id']}/interventions", {"action": "pause"})
        foreign = await api.write("/projects", {"title": "另一个项目", "body": "$y>0$"})

        class Conversation:
            async def generate(self, task):
                if task["run_id"] == sender["run_id"]:
                    return result("读取同项目材料并发送讨论。", [
                        action("read_object", object_id=obj["object_id"], branch_id=other["branch_id"]),
                        action("read_object", object_id=foreign["object_id"], branch_id=foreign["branch_id"]),
                        action("discuss", body="请检查条件 $x>0$ 是否必要。", recipient_run_id=receiver["run_id"])], next_action="finish")
                assert task["discussions"][0]["body"] == "请检查条件 $x>0$ 是否必要。"
                assert task["discussions"][0]["revision_id"] in task["context_revision_ids"]
                return result("已收到来源明确的讨论。", next_action="finish")

        await HTTPWorker(client, provider_factory=lambda _: Conversation(), fake_delay_seconds=0).run(once=True)
        steps = (await api.get(f"/runs/{sender['run_id']}/steps"))["steps"]
        assert steps[0]["actions"][0]["result"]["body"] == "条件为 $x>0$。"
        assert steps[0]["actions"][1]["error"] == "cross_project_read"
        await api.write(f"/runs/{receiver['run_id']}/resume")
        await HTTPWorker(client, provider_factory=lambda _: Conversation(), fake_delay_seconds=0).run(once=True)
    exercise(app, scenario)


def test_two_workers_and_human_keep_both_late_results_as_candidates(app):
    async def scenario(api, client):
        p, first = await project_and_run(api)
        second = await api.write("/runs", {"branch_id": p["branch_id"], "goal_object_id": p["object_id"], "autonomous": True})
        entered = []
        both_entered = asyncio.Event()
        changed = asyncio.Event()

        class Concurrent:
            async def generate(self, task):
                entered.append(task["attempt_id"])
                if len(entered) == 2:
                    both_entered.set()
                await changed.wait()
                return result("依据旧输入生成的候选。", [action("write_draft", body="不能落入新版的旧操作")], next_action="finish")

        workers = [asyncio.create_task(HTTPWorker(client, concurrency=1,
            provider_factory=lambda _: Concurrent(), fake_delay_seconds=0).run(once=True)) for _ in range(2)]
        await asyncio.wait_for(both_entered.wait(), timeout=10)
        await api.write(f"/objects/{p['object_id']}/revisions", {"branch_id": p["branch_id"],
            "expected_revision_id": p["revision_id"], "body": "人类修改为 $x^2+2>0$。"})
        changed.set()
        await asyncio.gather(*workers)
        for run in (first, second):
            step = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"][0]
            assert step["receipt"]["quarantined"] and step["actions"] == []
        snap = await api.get(f"/projects/{p['project_id']}/snapshot")
        assert len(snap["objects"]) == 1
        assert (await api.get(f"/projects/{p['project_id']}/budget"))["spent"] == 2
    exercise(app, scenario)


def test_agent_operation_permissions_and_cross_project_reads_leave_draft_and_feedback(app):
    class Script:
        async def generate(self, task):
            return result("保存被拒绝操作的回执。", [action("read_object",object_id="foreign-object"),
                action("write_draft",body="无权限的草稿")],next_action="finish")

    async def scenario(api, client):
        p, r = await project_and_run(api)
        await api.write(f"/projects/{p['project_id']}/agent-policy",{"allowed_operations":["read_object"]},method="PUT")
        await HTTPWorker(client, provider_factory=lambda _: Script(),fake_delay_seconds=0).run(once=True)
        steps = (await api.get(f"/runs/{r['run_id']}/steps"))["steps"]
        assert [a["status"] for a in steps[0]["actions"]] == ["rejected", "rejected"]
        assert steps[0]["actions"][1]["error"] == "operation_not_allowed"
        snapshot = await api.get(f"/projects/{p['project_id']}/snapshot")
        assert len(snapshot["objects"]) == 2
    exercise(app, scenario)


def test_step_limit_and_branch_pause_resume_do_not_freeze_other_branch(app):
    class NeverFinish:
        async def generate(self, task):
            return result("保存当前未解决结论。")

    async def scenario(api, client):
        p, r = await project_and_run(api,max_steps=2)
        other = await api.write("/branches",{"source_branch_id":p["branch_id"],"name":"independent"})
        await api.write(f"/branches/{p['branch_id']}/interventions",{"action":"pause"})
        await api.write("/runs",{"branch_id":other["branch_id"],"goal_object_id":p["object_id"],"autonomous":False})
        await HTTPWorker(client, fake_delay_seconds=0).run(once=True)
        snap = await api.get(f"/projects/{p['project_id']}/snapshot?branch_id={other['branch_id']}")
        assert any(run["state"] == "completed" for run in snap["runs"])
        assert (await api.get(f"/runs/{r['run_id']}/steps"))["steps"] == []
        await api.write(f"/branches/{p['branch_id']}/interventions",{"action":"resume"})
        await HTTPWorker(client, provider_factory=lambda _: NeverFinish(),fake_delay_seconds=0).run(once=True)
        with app.state.database.sessions() as s:
            assert s.get(Run,r["run_id"]).state == "step_limit"
        assert len((await api.get(f"/runs/{r['run_id']}/steps"))["steps"]) == 2
    exercise(app, scenario)


def test_autonomous_output_after_user_edit_is_quarantined_without_executing_actions(app):
    async def scenario(api, client):
        p, r = await project_and_run(api)
        class Late:
            async def generate(self, task):
                await api.write(f"/objects/{p['object_id']}/revisions",{"branch_id":p["branch_id"],
                    "expected_revision_id":p["revision_id"],"body":"已改成新的问题 $x^2+2>0$。"})
                return result("这是旧版候选。",[action("write_draft",body="不应执行的旧操作")],next_action="finish")
        await HTTPWorker(client,provider_factory=lambda _: Late(),fake_delay_seconds=0).run(once=True)
        rows = (await api.get(f"/runs/{r['run_id']}/steps"))["steps"]
        assert len(rows) == 1 and rows[0]["receipt"]["quarantined"]
        assert rows[0]["actions"] == []
        snap = await api.get(f"/projects/{p['project_id']}/snapshot")
        assert len(snap["objects"]) == 1
    exercise(app, scenario)
