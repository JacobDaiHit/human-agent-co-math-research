"""Resource and input tests only: synthetic providers do not evaluate mathematics."""
import asyncio
import json
from collections import defaultdict

import httpx
import pytest
from mathagent.persistence.agent_models import AgentRun
from mathagent.persistence.models import Revision
from mathagent.providers.remote import DeepSeekProvider, ProviderConfig
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_continuous_research import app as app
from test_continuous_research import exercise, output, read, setup, worker_write, write


async def create_peer(client, **options):
    project, run = await setup(client, **options)
    lead = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
    execution = {"token": lead["token"]}
    row = await worker_write(client, f"/attempts/{lead['attempt_id']}/requests",
        {**execution, "requested_output_tokens": 256})
    path = f"/requests/{row['request_id']}"
    await worker_write(client, path + "/start", execution)
    await worker_write(client, path + "/settle", {**execution, "outcome": "spent", "usage": {"completion_tokens": 8}})
    turn = await worker_write(client, f"/attempts/{lead['attempt_id']}/steps",
        {**execution, "request_id": row["request_id"], "result": output("Assign independent work.",
            ("assign_work", {"member": "peer", "goal": "Explore the original problem independently."}))["result"]})
    for tool in turn["pending_tools"]:
        await worker_write(client, f"/attempts/{lead['attempt_id']}/research-tool", {**execution, **tool})
    peer = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
    return project, run, lead, peer


def test_concurrent_members_cannot_over_reserve_the_root_output_budget(app):
    async def scenario(client):
        _, run, lead, peer = await create_peer(client, cumulative_output_token_budget=768)
        reservations = await asyncio.gather(*[worker_write(client,
            f"/attempts/{task['attempt_id']}/requests", {"token": task["token"], "requested_output_tokens": 400})
            for task in (lead, peer)])
        assert all(row["continue"] for row in reservations)
        assert sum(row["output_token_reservation"] for row in reservations) == 760
        budget = (await read(client, f"/runs/{run['run_id']}/budget"))["output_token_budget"]
        assert budget["occupied_output_tokens"] <= 768
        assert (await read(client, f"/runs/{run['run_id']}/research"))["session"]["state"] == "researching"
    exercise(app, scenario)


@pytest.mark.parametrize("cap", [1, 256, 393216])
def test_exhausted_output_budget_preserves_progress_without_an_extra_model_call(app, cap):
    calls = 0

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            assert task["max_output_tokens"] == cap
            result = output("Durable unfinished progress.", ("read_material", {"ref": "original"}))
            result["usage"] = {"completion_tokens": cap}
            return result

    async def scenario(client):
        _, run = await setup(client, max_output_tokens=cap, cumulative_output_token_budget=cap)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert calls == 1 and state["session"]["solution_revision_id"] is None
        assert state["session"]["state"] == "budget_exhausted"
        steps = (await read(client, f"/runs/{run['run_id']}/steps"))["steps"]
        assert steps[0]["body"] == "Durable unfinished progress."
    exercise(app, scenario)


def test_historical_autonomy_is_readable_but_never_redispatched(app):
    async def scenario(client):
        _, run = await setup(client, autonomous=False)
        with app.state.database.sessions.begin() as session:
            old = session.get(AgentRun, run["run_id"])
            old.autonomous = True
            old.options = {**old.options, "solver_controller": "legacy"}
        assert (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"] is None
        assert (await read(client, f"/runs/{run['run_id']}/budget"))["occupied"] == 0
        assert (await read(client, f"/runs/{run['run_id']}/options"))["solver_controller"] == "legacy"
        response = await client.post(f"/runs/{run['run_id']}/resume",
            headers={"Authorization": "Bearer human", "Idempotency-Key": "historical-resume"})
        assert response.status_code == 409
        assert (await read(client, f"/runs/{run['run_id']}/steps"))["steps"] == []
    exercise(app, scenario)


def test_independent_goal_changes_preserve_own_notes_and_given_context_but_hide_lead(app):
    counts = {"lead": 0, "peer": 0}

    class Model:
        async def generate(self, task):
            member = task["research_member"]
            counts[member] += 1
            visible = json.dumps(task["conversation"], ensure_ascii=False)
            if member == "lead":
                if counts[member] == 1:
                    return output("Lead's private preliminary work.",
                        ("save_note", {"scope": "shared", "body": "LEAD_PRIVATE_RESULT"}),
                        ("assign_work", {"member": "peer", "goal": "Study the problem independently.", "independent": True}),
                        ("send_message", {"recipient": "peer", "topic": "Compare later", "body": "LEAD_PRIVATE_RESULT", "wait": True}))
                return output("Selected ordinary solution.", ("submit_solution", {"outcome": "solved"}))
            if counts[member] > 2:
                assert "LEAD_PRIVATE_RESULT" in visible
                return output("Continue the requested discussion.", ("finish_work", {}))
            assert "LEAD_PRIVATE_RESULT" not in visible
            assert "send_message" not in [tool["function"]["name"] for tool in task["tools"]]
            if counts[member] == 1:
                return output("Begin from supplied definitions.",
                    ("read_material", {"ref": "background-1"}),
                    ("save_note", {"scope": "personal", "body": "OWN_INDEPENDENT_PROGRESS"}),
                    ("assign_work", {"member": "self", "goal": "Continue the local calculation."}))
            assert "OWN_INDEPENDENT_PROGRESS" in visible and "GIVEN_DEFINITION" in visible
            return output("Independent result.", ("finish_work", {"summary": "Independent result."}))

    async def scenario(client):
        project = await write(client, "/projects", {"title": "Given definitions", "body": "Use the given definitions."})
        await write(client, "/objects", {"branch_id": project["branch_id"], "kind": "context",
            "body": "GIVEN_DEFINITION: the symbol denotes the real domain."})
        run = await write(client, "/runs", {"branch_id": project["branch_id"],
            "goal_object_id": project["object_id"], "autonomous": True, "request_budget": 12})
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert counts["lead"] == 2 and counts["peer"] >= 2
        peer_work = [work for work in state["work"] if work["member_run_id"] != run["run_id"]]
        assert len(peer_work) >= 2 and all(work["independent"] for work in peer_work[:2])
        assert state["session"]["state"] == "completed"
    exercise(app, scenario)


async def save_turn(client, task, body, *actions):
    execution = {"token": task["token"]}
    request = await worker_write(client, f"/attempts/{task['attempt_id']}/requests",
        {**execution, "requested_output_tokens": 256})
    request_id = request["request_id"]
    await worker_write(client, f"/requests/{request_id}/start", execution)
    await worker_write(client, f"/requests/{request_id}/settle",
        {**execution, "outcome": "spent", "usage": {"completion_tokens": 8}})
    step = await worker_write(client, f"/attempts/{task['attempt_id']}/steps",
        {**execution, "request_id": request_id, "result": output(body, *actions)["result"]})
    return [await worker_write(client, f"/attempts/{task['attempt_id']}/research-tool",
        {**execution, **tool}) for tool in step["pending_tools"]]


def test_named_team_members_first_work_independently_then_message_each_other(app):
    async def scenario(client):
        _, run = await setup(client, max_researchers=3)
        lead = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        await save_turn(client, lead, "LEAD_PRIVATE_ARGUMENT",
            ("save_note", {"scope": "shared", "body": "LEAD_PRIVATE_ARGUMENT"}),
            ("assign_work", {"member": "alpha", "goal": "Explore a neutral local question.", "independent": True}),
            ("assign_work", {"member": "beta", "goal": "Try another method independently.", "independent": True}))
        peers = {}
        for _ in range(2):
            task = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
            peers[task["research_member"]] = task
            assert "LEAD_PRIVATE_ARGUMENT" not in json.dumps(task["conversation"])
            assert "send_message" not in {tool["function"]["name"] for tool in task["tools"]}
        for name, task in peers.items():
            results = await save_turn(client, task, name + " independent derivation",
                ("list_materials", {"query": "LEAD_PRIVATE_ARGUMENT"}),
                ("finish_work", {}))
            assert results[0]["result"]["materials"] == []
            await worker_write(client, f"/attempts/{task['attempt_id']}/research-context", {"token": task["token"]})
        results = await save_turn(client, lead, "Organize a concrete discussion.",
            ("assign_work", {"member": "gamma", "goal": "Over the chosen team size."}),
            ("assign_work", {"member": "alpha", "goal": "Discuss your derivation with beta."}))
        assert results[0]["result"]["error"] == "tool_arguments"
        alpha = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        assert alpha["research_member"] == "alpha"
        assert "send_message" in {tool["function"]["name"] for tool in alpha["tools"]}
        await save_turn(client, alpha, "A concrete new step.",
            ("send_message", {"recipient": "beta", "topic": "A local inference", "body": "DIRECT_PEER_ARGUMENT"}),
            ("finish_work", {}))
        beta = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        assert beta["research_member"] == "beta" and "DIRECT_PEER_ARGUMENT" in json.dumps(beta["conversation"])
        await save_turn(client, beta, "A reply with actual mathematics.", ("finish_work", {}))
        await save_turn(client, lead, "Organized result.", ("submit_solution", {"outcome": "unresolved"}))
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert {member["name"] for member in state["members"]} == {"lead", "alpha", "beta"}
        assert any(message["sender_run_id"] == peers["alpha"]["run_id"]
                   and message["recipient_run_id"] == peers["beta"]["run_id"]
                   for message in state["messages"])
    exercise(app, scenario)


def test_inflight_reservations_wait_and_actual_unused_capacity_restarts_research(app):
    async def scenario(client):
        _, run, lead, peer = await create_peer(client, cumulative_output_token_budget=1024)
        reserved = await worker_write(client, f"/attempts/{lead['attempt_id']}/requests",
            {"token": lead["token"], "requested_output_tokens": 1016})
        waiting = await worker_write(client, f"/attempts/{peer['attempt_id']}/requests",
            {"token": peer["token"], "requested_output_tokens": 256})
        assert waiting == {"continue": False, "state": "waiting_budget"}
        assert (await read(client, f"/runs/{run['run_id']}/research"))["session"]["state"] == "researching"
        await worker_write(client, f"/requests/{reserved['request_id']}/start", {"token": lead["token"]})
        await worker_write(client, f"/requests/{reserved['request_id']}/settle", {
            "token": lead["token"], "outcome": "spent", "usage": {"completion_tokens": 16}})
        resumed = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        assert resumed["run_id"] == peer["run_id"] and resumed["attempt_id"] != peer["attempt_id"]
        assert resumed["max_output_tokens"] == 1000
        status = (await read(client, f"/runs/{run['run_id']}/research"))["output_budget"]
        assert status["reported_output_tokens"] == 24 and status["remaining_output_tokens"] == 1000
    exercise(app, scenario)


def test_actual_wire_input_is_append_only_across_goals_roster_notes_and_peer_reply(app):
    sent = defaultdict(list)

    def transport(request):
        payload = json.loads(request.content)
        member = "lead" if "你是主研究者" in payload["messages"][1]["content"] else "alpha"
        sent[member].append(payload)
        number = len(sent[member])
        if member == "alpha":
            assert "TARGETED_LOCAL_GOAL" in payload["messages"][1]["content"]
            assert "SHARED_NEW" in payload["messages"][1]["content"]
            response = output("PEER_TARGETED_RESULT", ("finish_work", {}))
        elif number == 1:
            response = output("FIRST_DERIVATION",
                ("save_note", {"scope": "personal", "body": "OWN_WORKNOTE"}),
                ("save_note", {"scope": "shared", "body": "SHARED_OLD"}),
                ("read_material", {"ref": "shared_note"}),
                ("assign_work", {"member": "self", "goal": "SECOND_LOCAL_GOAL", "materials": ["shared_note"]}))
        elif number == 2:
            assert "SHARED_OLD" in payload["messages"][-1]["content"]
            response = output("SECOND_DERIVATION",
                ("save_note", {"scope": "shared", "body": "SHARED_NEW"}),
                ("assign_work", {"member": "alpha", "goal": "TARGETED_LOCAL_GOAL", "materials": ["shared_note"]}))
        elif number == 3:
            response = output("READ_CURRENT_NOTE", ("read_material", {"ref": "shared_note"}))
        elif number == 4:
            replies = [json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"]
            assert any(reply.get("body") == "SHARED_OLD" for reply in replies)
            assert replies[-1]["body"] == "SHARED_NEW"
            response = output("WAIT_FOR_LOCAL_RESULT", ("send_message", {
                "recipient": "alpha", "topic": "local step", "body": "Please return your derivation.", "wait": True}))
        else:
            assert "PEER_TARGETED_RESULT" in json.dumps(payload, ensure_ascii=False)
            response = output("PROOF_WITHOUT_SHORT_ANSWER", ("submit_solution", {"outcome": "solved"}))
        message = response["result"]["message"]
        message["reasoning_content"] = "ORIGINAL_REASONING_" + str(number)
        # Whitespace in tool arguments must survive the provider/receipt replay.
        message["tool_calls"][0]["function"]["arguments"] = json.dumps(
            json.loads(message["tool_calls"][0]["function"]["arguments"]), ensure_ascii=False, indent=2)
        return httpx.Response(200, json={"id": member + str(number), "choices": [{
            "finish_reason": "tool_calls", "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108,
                      "prompt_cache_hit_tokens": 32, "prompt_cache_miss_tokens": 68}})

    async def scenario(client):
        _, run = await setup(client)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as local:
            provider = DeepSeekProvider(ProviderConfig("deepseek", "synthetic-secret", "fixture-model",
                                                       "https://fixture.invalid", True), local)
            await HTTPWorker(client, provider_factory=lambda _: provider, fake_delay_seconds=0,
                             concurrency=1).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert state["solution"]["body"] == "PROOF_WITHOUT_SHORT_ANSWER" and state["session"]["answer"] is None
        calls = (await read(client, f"/runs/{run['run_id']}/calls"))["calls"]
        assert calls[0]["call_config"]["research_context"]["previous_input_preserved"] is None
        assert all(call["call_config"]["research_context"]["previous_input_preserved"] is True for call in calls[1:]), [
            (call["call_config"]["research_context"]["input_messages"],
             call["call_config"]["research_context"]["previous_input_preserved"]) for call in calls]
        assert state["usage_summary"]["cache"]["calls_with_cache_usage"] == 6
        assert set(state["usage_summary"]["by_researcher"]) == {"lead", "alpha"}
    exercise(app, scenario)
    assert len(sent["lead"]) == 5 and len(sent["alpha"]) == 1
    for previous, following in zip(sent["lead"], sent["lead"][1:]):
        assert following["messages"][:len(previous["messages"])] == previous["messages"]
        assert following["tools"] == previous["tools"]


def test_waiting_researcher_keeps_dialogue_after_pause_and_resume(app):
    async def scenario(client):
        _, run = await setup(client)
        task = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        opening, dialogue = task["conversation"][0], task["research_dialogue_id"]
        await save_turn(client, task, "SAVED_PROGRESS", ("read_material", {"ref": "original"}))
        await write(client, f"/runs/{run['run_id']}/interventions", {"action": "pause"})
        await worker_write(client, f"/attempts/{task['attempt_id']}/research-context", {"token": task["token"]})
        await write(client, f"/runs/{run['run_id']}/resume", {})
        resumed = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        assert resumed["attempt_id"] != task["attempt_id"]
        assert resumed["research_dialogue_id"] == dialogue and resumed["conversation"][0] == opening
        assert "SAVED_PROGRESS" in json.dumps(resumed["conversation"])
        assert (await read(client, f"/runs/{run['run_id']}/budget"))["spent"] == 1
    exercise(app, scenario)


def test_failed_peer_notifies_every_researcher_waiting_for_it(app):
    async def scenario(client):
        _, run = await setup(client, max_researchers=3)
        lead = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        await save_turn(client, lead, "Arrange local work.",
            ("assign_work", {"member": "alpha", "goal": "A local proof."}),
            ("assign_work", {"member": "beta", "goal": "Another local proof."}),
            ("send_message", {"recipient": "alpha", "topic": "local", "body": "Please reply.", "wait": True}))
        await worker_write(client, f"/attempts/{lead['attempt_id']}/research-context", {"token": lead["token"]})
        alpha = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        beta = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        await save_turn(client, beta, "Wait for alpha.", ("send_message", {
            "recipient": "alpha", "topic": "local", "body": "Need that computation.", "wait": True}))
        await worker_write(client, f"/attempts/{beta['attempt_id']}/research-context", {"token": beta["token"]})
        await worker_write(client, f"/attempts/{alpha['attempt_id']}/fail", {
            "token": alpha["token"], "reason": "synthetic_failure"})
        state = await read(client, f"/runs/{run['run_id']}/research")
        notified = {item["recipient_run_id"] for item in state["messages"] if item["runtime_notice"]}
        assert notified == {lead["run_id"], beta["run_id"]}
        assert all(item["state"] == "queued" for item in state["members"] if item["run_id"] in notified)
    exercise(app, scenario)


def test_late_peers_still_have_parallel_execution_slots(app):
    counts = defaultdict(int)

    async def scenario(client):
        both_peers_started = asyncio.Event()
        started = set()

        class Model:
            async def generate(self, task):
                member = task["research_member"]
                counts[member] += 1
                if member == "lead":
                    if counts[member] == 1:
                        # Allow the other slots to poll while only the lead exists.
                        await asyncio.sleep(0.03)
                        return output("An initial calculation.", ("read_material", {"ref": "original"}))
                    if counts[member] == 2:
                        return output("Invite two local collaborators later.",
                            ("assign_work", {"member": "alpha", "goal": "Local question A."}),
                            ("assign_work", {"member": "beta", "goal": "Local question B."}),
                            ("send_message", {"recipient": "alpha", "topic": "A", "body": "Return your derivation.", "wait": True}))
                    return output("Save the combined proof.", ("submit_solution", {"outcome": "solved"}))
                started.add(member)
                if started == {"alpha", "beta"}:
                    both_peers_started.set()
                await asyncio.wait_for(both_peers_started.wait(), 5)
                return output(member + " finished local derivation", ("finish_work", {}))

        _, run = await setup(client, max_researchers=3)
        worker = HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0,
                            concurrency=3, poll_seconds=0.005)
        try:
            await asyncio.wait_for(worker.run(once=True), 10)
        except TimeoutError:
            state = await read(client, f"/runs/{run['run_id']}/research")
            pytest.fail(str({"calls": dict(counts), "started": started,
                "members": [(item["name"], item["state"]) for item in state["members"]],
                "work": [(item["goal"], item["state"]) for item in state["work"]]}))
        assert started == {"alpha", "beta"}
        assert (await read(client, f"/runs/{run['run_id']}/research"))["session"]["state"] == "completed"
    exercise(app, scenario)


def test_budget_settings_append_notice_without_rewriting_saved_opening(app):
    async def scenario(client):
        _, run = await setup(client, cumulative_output_token_budget=4096)
        task = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        await save_turn(client, task, "Retain this derivation.", ("read_material", {"ref": "original"}))
        previous = (await worker_write(client, f"/attempts/{task['attempt_id']}/research-context",
            {"token": task["token"]}))["task"]
        options = await read(client, f"/runs/{run['run_id']}/options")
        options.pop("run_id")
        response = await client.put(f"/runs/{run['run_id']}/options", json={
            **options, "cumulative_output_token_budget": 8192}, headers={
                "Authorization": "Bearer human", "Idempotency-Key": "change-output-budget"})
        assert response.is_success, response.text
        following = (await worker_write(client, f"/attempts/{task['attempt_id']}/research-context",
            {"token": task["token"]}))["task"]
        assert following["research_dialogue_id"] == previous["research_dialogue_id"]
        assert following["conversation"][:len(previous["conversation"])] == previous["conversation"]
        assert "8184" in following["conversation"][-1]["content"]
    exercise(app, scenario)


def test_context_uses_known_provider_capacity_not_a_fixed_research_cutoff(app):
    sent = []

    def transport(request):
        payload = json.loads(request.content)
        sent.append(payload)
        number = len(sent)
        if number == 1:
            message = output("FIRST_ARCHIVED_DERIVATION",
                ("save_note", {"scope": "personal", "body": "CAPACITY_WORKNOTE"}))['result']['message']
            prompt, completion = 100000, 90000
        elif number == 2:
            assert payload["messages"][:len(sent[0]["messages"])] == sent[0]["messages"]
            assert "上下文容量说明" in payload["messages"][-1]["content"]
            assert 1 <= payload["max_tokens"] < 10000
            message = output("RECENT_ARCHIVED_DERIVATION", ("read_material", {"ref": "original"}))['result']['message']
            prompt, completion = 199900, 300
        else:
            assert number == 3
            opening = payload["messages"][1]["content"]
            assert "CAPACITY_WORKNOTE" in opening and "最近推导已保存为" in opening
            assert "FIRST_ARCHIVED_DERIVATION" not in json.dumps(payload["messages"])
            message = output("PROOF_AFTER_CAPACITY_CONTINUATION", ("submit_solution", {"outcome": "solved"}))['result']['message']
            prompt, completion = 1000, 10
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": message}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}})

    async def scenario(client):
        _, run = await setup(client)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as local:
            provider = DeepSeekProvider(ProviderConfig("deepseek", "synthetic-secret", "fixture",
                "https://fixture.invalid", True, context_capacity=200000), local)
            await HTTPWorker(client, provider_factory=lambda _: provider, fake_delay_seconds=0).run(once=True)
        calls = (await read(client, f"/runs/{run['run_id']}/calls"))["calls"]
        contexts = [item["call_config"]["research_context"] for item in calls]
        assert contexts[0]["dialogue_id"] == contexts[1]["dialogue_id"]
        assert contexts[1]["input_estimate"] > 100000 and contexts[1]["previous_input_preserved"] is True
        assert contexts[2]["dialogue_id"] != contexts[1]["dialogue_id"]
        assert contexts[2]["reset_reason"] == "context_capacity"
        with app.state.database.sessions() as session:
            assert set(session.scalars(select(Revision.body).where(Revision.body.in_(
                ["FIRST_ARCHIVED_DERIVATION", "RECENT_ARCHIVED_DERIVATION"])))) == {
                    "FIRST_ARCHIVED_DERIVATION", "RECENT_ARCHIVED_DERIVATION"}
    exercise(app, scenario)
    assert len(sent) == 3


def test_disabling_discussion_changes_tools_without_reusing_the_old_catalog(app):
    async def scenario(client):
        _, run = await setup(client)
        task = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        await save_turn(client, task, "Existing mathematical progress.",
            ("save_note", {"scope": "personal", "body": "CONTINUE_OWN_WORK"}))
        options = await read(client, f"/runs/{run['run_id']}/options")
        options.pop("run_id")
        response = await client.put(f"/runs/{run['run_id']}/options", json={**options, "discussion": False},
            headers={"Authorization": "Bearer human", "Idempotency-Key": "disable-discussion"})
        assert response.is_success, response.text
        updated = (await worker_write(client, f"/attempts/{task['attempt_id']}/research-context",
            {"token": task["token"]}))["task"]
        assert updated["research_dialogue_id"] != task["research_dialogue_id"]
        assert updated["research_reset_reason"] == "configuration_changed"
        assert "send_message" not in {item["function"]["name"] for item in updated["tools"]}
        assert "CONTINUE_OWN_WORK" in updated["conversation"][0]["content"]
    exercise(app, scenario)


def test_assigning_a_waiting_peer_a_new_goal_releases_its_old_wait_and_continues_dialogue(app):
    async def scenario(client):
        _, run, lead, peer = await create_peer(client)
        await save_turn(client, peer, "OWN_WAITING_DERIVATION", ("send_message", {
            "recipient": "lead", "topic": "old question", "body": "Can you clarify?", "wait": True}))
        await worker_write(client, f"/attempts/{peer['attempt_id']}/research-context", {"token": peer["token"]})
        await save_turn(client, lead, "Revise the local target.",
            ("save_note", {"scope": "shared", "body": "REASSIGNED_MATERIAL"}),
            ("assign_work", {"member": "peer", "goal": "NEW_LOCAL_GOAL", "materials": ["shared_note"]}))
        updated = (await worker_write(client, "/worker/claim-next", {"providers": ["fake"]}))["task"]
        assert updated["research_member"] == "peer"
        assert updated["research_dialogue_id"] == peer["research_dialogue_id"]
        assert "OWN_WAITING_DERIVATION" in json.dumps(updated["conversation"])
        notice = next(message["content"] for message in updated["conversation"]
                      if message.get("content", "").startswith("当前任务更新："))
        assert "NEW_LOCAL_GOAL" in notice and "REASSIGNED_MATERIAL" in notice
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert peer["run_id"] not in state["session"]["config"]["waiting_for"]
        old = next(work for work in state["work"] if work["id"] == peer["research_work_id"])
        assert old["state"] == "replaced"
    exercise(app, scenario)
