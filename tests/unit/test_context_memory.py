"""Bounded controller context and memory packet regression tests."""

from mathagent.providers.actions import ReadObject, RequestMemory
from mathagent.providers.protocol import AgentAction
from mathagent.runtime.context import compact_task


def test_large_search_context_preserves_route_identity_and_read_pointer():
    task = {
        "attempt_id": "attempt-1", "mode": "research", "goal_object_id": "goal",
        "inputs": [{"object_id": "goal", "revision_id": "goal-v1", "branch_id": "b",
                     "kind": "problem", "body": "goal", "payload": {}}],
        "instruction": "continue", "operation_schemas": {},
        "search_context": {"work_id": "w", "kind": "route", "route_id": "r1",
                            "controller": "bounded_search_v1", "budget": {"remaining": 3},
                            "original_goal_revision_id": "goal-v1", "selected_route_id": "r1",
                            "progress": "progress " * 10000, "gaps": ["gap " * 5000],
                            "repair_contract": {"body": "repair " * 5000}},
    }
    result = compact_task(task)
    context = result["search_context"]
    assert all(context[key] == task["search_context"][key] for key in (
        "work_id", "kind", "route_id", "controller", "budget",
        "original_goal_revision_id", "selected_route_id"))
    assert context["progress"]["omitted"] is True
    assert context["progress"]["read_ref"]["section"] == "record"
    ref = context["progress"]["read_ref"]
    ReadObject.model_validate(ref)
    AgentAction.model_validate({"type": "read_object", "arguments": {
        "object_id": ref["object_id"], "revision_id": ref["revision_id"],
        "context_attempt_id": ref["context_attempt_id"], "section": "record"}})


def test_large_memory_packet_uses_request_memory_pointer():
    task = {
        "attempt_id": "attempt-1", "mode": "research", "goal_object_id": "goal",
        "inputs": [{"object_id": "goal", "revision_id": "goal-v1", "branch_id": "b",
                     "kind": "problem", "body": "goal", "payload": {}}],
        "instruction": "continue", "operation_schemas": {},
        "memory_packet": {"metadata": {"work_id": "w"}, "entries":[{"text": "memory " * 10000}]},
    }
    result = compact_task(task)
    entries = result["memory_packet"]["entries"]
    assert entries["omitted"] is True
    assert entries["read_ref"]["type"] == "request_memory"
    assert entries["read_ref"]["arguments"]["offset"] == 0
    RequestMemory.model_validate(entries["read_ref"]["arguments"])
    AgentAction.model_validate(entries["read_ref"])
