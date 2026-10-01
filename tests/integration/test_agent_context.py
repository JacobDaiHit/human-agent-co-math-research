"""Offline acceptance for bounded, rereadable and version-fenced agent context."""


import pytest
from mathagent.persistence.models import Review
from mathagent.providers.protocol import AgentAction, messages_for
from mathagent.runtime.context import compact_task, digest, read_page, serialized
from test_autonomous_agent import app as app
from test_autonomous_agent import exercise, project_and_run


def test_context_budget_counts_json_escaping_and_repair_without_losing_source_cursors():
    text = '\x00\\"\n' * 30000
    inputs = [
        {
            "object_id": f"obj-{i}",
            "revision_id": f"rev-{i}",
            "branch_id": "branch",
            "kind": "problem" if i == 0 else "context",
            "body": text,
            "payload": {"premises": [{"formula": text} for _ in range(3)]},
            "evidence": [{"scope": text, "findings": [text]}],
        }
        for i in range(12)
    ]
    task = {
        "attempt_id": "attempt",
        "mode": "research",
        "goal_object_id": "obj-0",
        "inputs": inputs,
        "instruction": text,
        "autonomous": True,
        "operation_schemas": {},
        "previous_steps": [
            {
                "revision_id": "rev-0",
                "object_id": "obj-0",
                "branch_id": "branch",
                "body": text,
                "actions": [{"type": "read_object", "status": "completed", "result": inputs[0]}],
            }
        ]
        * 6,
        "proof_plans": [{"revision_id": "rev-0", "object_id": "obj-0", "gaps": [text] * 12}],
    }
    compact = compact_task(task)
    repaired = compact_task({**compact, "repair_output": "\x00" * 30000})
    for value in (compact, repaired):
        assert sum(len(m["content"]) for m in messages_for(value)) < 190000
        assert value["context_summary"]["omitted_count"] > 0
        goal = next(i for i in value["inputs"] if i["object_id"] == "obj-0")
        assert goal["body_sha256"] == digest(text)
        assert goal["total_chars"] == len(text)
        assert goal["next_offset"] == len(goal["body"])
        assert goal["excerpted"]
        assert value["context_summary"]["run_context_read_ref"]["context_attempt_id"] == "attempt"
    assert inputs[0]["body"] == text and inputs[0]["payload"]["premises"][0]["formula"] == text


@pytest.mark.parametrize("section", ["body", "payload", "record"])
def test_all_record_sections_page_losslessly_with_hash_and_progress(section):
    item = {
        "object_id": "o",
        "revision_id": "r",
        "branch_id": "b",
        "kind": "argument",
        "body": ("$$\\sum_{i=1}^n x_i^2>0$$\n\n" * 1200),
        "payload": {"premises": ["必须保留的前提 $x>0$" * 500]},
        "proof_plan": {
            "gaps": ["需要完整回读" * 1000],
            "dependencies": [{"revision_id": "original"}],
        },
        "operation_receipts": [{"result": {"body": "长回执" * 1000}}],
    }
    expected = (
        item["body"]
        if section == "body"
        else serialized(item["payload"] if section == "payload" else item)
    )
    parts, offset = [], 0
    while True:
        page = read_page(item, {"section": section, "offset": offset, "max_chars": 1000})
        assert page["body_sha256"] == digest(expected)
        assert len(page["body"]) <= 1000
        parts.append(page["body"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(parts) == expected
    with pytest.raises(ValueError):
        read_page(item, {"section": section, "offset": len(expected) + 1})


def read(runtime, session, run, **arguments):
    return runtime.agent.execute_action(
        session, run, AgentAction(type="read_object", arguments=arguments)
    )


def test_independent_review_context_excludes_prior_llm_verdict(app):
    async def scenario(api, client):
        p, _ = await project_and_run(api)
        claim = await api.write(
            "/objects", {"branch_id": p["branch_id"], "kind": "claim", "body": "$x^2>0$"}
        )
        with app.state.database.sessions.begin() as session:
            session.add(
                Review(
                    project_id=p["project_id"],
                    target_revision_id=claim["revision_id"],
                    kind="llm_review",
                    verdict="passed",
                    scope="PRIOR_MODEL_VERDICT_MUST_NOT_APPEAR",
                    findings=["Synthetic prior model opinion."],
                    dependency_snapshot={claim["object_id"]: claim["revision_id"]},
                    author="test:synthetic-model",
                )
            )
        review = await api.write(
            "/runs",
            {
                "branch_id": p["branch_id"],
                "goal_object_id": claim["object_id"],
                "mode": "review",
                "autonomous": False,
            },
        )
        task = await api.write(f"/runs/{review['run_id']}/claim", worker=True)
        assert "PRIOR_MODEL_VERDICT_MUST_NOT_APPEAR" not in serialized(messages_for(task))
        assert all(e["kind"] != "llm_review" for i in task["inputs"] for e in i["evidence"])
        assert all("support" not in i for i in task["inputs"])

    exercise(app, scenario)
