"""Resource and input tests only: synthetic providers do not evaluate mathematics."""
import asyncio
import json

from mathagent.persistence.agent_models import AgentRun
from mathagent.runtime.worker import HTTPWorker
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
        assert sum(row["continue"] for row in reservations) == 1
        budget = (await read(client, f"/runs/{run['run_id']}/budget"))["output_token_budget"]
        assert budget["occupied_output_tokens"] <= 768
        assert (await read(client, f"/runs/{run['run_id']}/research"))["session"]["state"] == "budget_exhausted"
    exercise(app, scenario)


def test_exhausted_output_budget_preserves_progress_without_an_extra_model_call(app):
    calls = 0

    class Model:
        async def generate(self, task):
            nonlocal calls
            calls += 1
            assert task["max_output_tokens"] == 256
            result = output("Durable unfinished progress.", ("read_material", {"ref": "original"}))
            result["usage"] = {"completion_tokens": 256}
            return result

    async def scenario(client):
        _, run = await setup(client, max_output_tokens=256, cumulative_output_token_budget=256)
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
                        ("assign_work", {"member": "peer", "goal": "Study the problem independently."}),
                        ("send_message", {"recipient": "peer", "topic": "Compare later", "body": "LEAD_PRIVATE_RESULT", "wait": True}))
                return output("Selected ordinary solution.", ("submit_solution", {"outcome": "solved"}))
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
        assert counts == {"lead": 2, "peer": 2}
        peer_work = [work for work in state["work"] if work["member_run_id"] != run["run_id"]]
        assert len(peer_work) >= 2 and all(work["independent"] for work in peer_work[:2])
        assert state["session"]["state"] == "completed"
    exercise(app, scenario)
