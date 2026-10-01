"""Operational notices wake waiting researchers; they do not evaluate mathematics."""
import asyncio
import json
from uuid import uuid4

from mathagent.providers.remote import ProviderFailure
from mathagent.runtime.worker import HTTPWorker
from test_continuous_research import app as app
from test_continuous_research import exercise, output, read, setup


def test_known_peer_call_failure_notifies_lead_instead_of_waiting_forever(app):
    calls = {"lead": 0, "peer": 0}

    class Model:
        async def generate(self, task):
            role = task["research_member"]
            calls[role] += 1
            if role == "peer":
                raise ProviderFailure("incomplete_output", outcome="spent", observation={
                    "complete": False, "raw_text": "unfinished", "usage": {"completion_tokens": 8}})
            if calls[role] == 1:
                return output("Request independent work.",
                    ("assign_work", {"member": "peer", "goal": "Explore independently."}),
                    ("send_message", {"recipient": "peer", "topic": "Results", "body": "Report your work.", "wait": True}))
            visible = json.dumps(task["conversation"], ensure_ascii=False)
            assert "运行状态说明" in visible and "incomplete_output" in visible
            return output("Continue with own reasoning.", ("submit_solution", {"outcome": "unresolved"}))

    async def scenario(client):
        _, run = await setup(client)
        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        assert calls == {"lead": 2, "peer": 1}
        assert (await read(client, f"/runs/{run['run_id']}/research"))["session"]["state"] == "completed"
    exercise(app, scenario)


def test_disabling_discussion_wakes_lead_but_does_not_abort_paid_peer_response(app):
    counts = {"lead": 0, "peer": 0}
    peer_returned = False

    async def scenario(client):
        _, run = await setup(client)

        class Model:
            async def generate(self, task):
                nonlocal peer_returned
                role = task["research_member"]
                counts[role] += 1
                if role == "peer":
                    options = await read(client, f"/runs/{run['run_id']}/options")
                    options.pop("run_id")
                    response = await client.put(f"/runs/{run['run_id']}/options",
                        json={**options, "discussion": False}, headers={
                            "Authorization": "Bearer human", "Idempotency-Key": str(uuid4())})
                    assert response.is_success, response.text
                    await asyncio.sleep(0)
                    peer_returned = True
                    return output("Already-paid peer body is retained.", ("finish_work", {}))
                if counts[role] == 1:
                    return output("Request independent work.",
                        ("assign_work", {"member": "peer", "goal": "Explore independently."}),
                        ("send_message", {"recipient": "peer", "topic": "Results", "body": "Report your work.", "wait": True}))
                assert "用户已关闭" in json.dumps(task["conversation"], ensure_ascii=False)
                assert "send_message" not in [tool["function"]["name"] for tool in task["tools"]]
                return output("Continue independently.", ("submit_solution", {"outcome": "unresolved"}))

        await HTTPWorker(client, provider_factory=lambda _: Model(), fake_delay_seconds=0).run(once=True)
        state = await read(client, f"/runs/{run['run_id']}/research")
        assert peer_returned and counts["lead"] == 2 and counts["peer"] == 1
        assert state["session"]["state"] == "completed"
        peer = next(member for member in state["members"] if member["name"] == "peer")
        steps = (await read(client, f"/runs/{peer['run_id']}/steps"))["steps"]
        assert steps[0]["body"] == "Already-paid peer body is retained."
    exercise(app, scenario)
