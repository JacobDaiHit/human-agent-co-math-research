"""Offline acceptance for bounded, rereadable and version-fenced agent context."""

import json

import pytest
from mathagent.application.errors import DomainError
from mathagent.persistence.models import Attempt, Review, Revision, Run
from mathagent.providers.protocol import AgentAction, messages_for
from mathagent.runtime.context import compact_task, digest, read_page, serialized
from mathagent.runtime.service import Runtime
from mathagent.runtime.worker import HTTPWorker
from test_autonomous_agent import action, exercise, project_and_run, result
from test_autonomous_agent import app as app


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


def test_historical_proof_metadata_and_wrong_object_are_checked(app):
    async def scenario(api, client):
        p, r = await project_and_run(api)
        assumption = await api.write(
            "/objects", {"branch_id": p["branch_id"], "kind": "context", "body": "前提 $x>0$"}
        )
        claim = await api.write(
            "/objects", {"branch_id": p["branch_id"], "kind": "claim", "body": "结论 $x^2>0$"}
        )
        proof = await api.write(
            "/proof-plans",
            {
                "branch_id": p["branch_id"],
                "conclusion_revision_id": claim["revision_id"],
                "body": "旧证明 " * 3000,
                "assumption_revision_ids": [assumption["revision_id"]],
                "gaps": ["必须解决的缺口 $x=0$ " * 1000],
            },
        )
        await api.write(
            f"/objects/{proof['object_id']}/revisions",
            {
                "branch_id": p["branch_id"],
                "expected_revision_id": proof["revision_id"],
                "body": "新证明",
            },
        )
        runtime = Runtime(app.state.service)
        with app.state.database.sessions() as session:
            run = session.get(Run, r["run_id"])
            args = {
                "object_id": proof["object_id"],
                "revision_id": proof["revision_id"],
                "section": "record",
                "max_chars": 20000,
            }
            chunks, offset, source_hash = [], 0, None
            while True:
                page = read(runtime, session, run, **args, offset=offset)
                source_hash = source_hash or page["body_sha256"]
                assert page["body_sha256"] == source_hash and not page["current"]
                chunks.append(page["body"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            record = json.loads("".join(chunks))
            assert (
                record["proof_plan"]["dependencies"][0]["revision_id"] == assumption["revision_id"]
            )
            assert record["proof_plan"]["gaps"] == ["必须解决的缺口 $x=0$ " * 1000]
            with pytest.raises(DomainError) as error:
                read(
                    runtime,
                    session,
                    run,
                    object_id=p["object_id"],
                    revision_id=proof["revision_id"],
                )
            assert error.value.response["error"] == "revision_object_mismatch"

    exercise(app, scenario)


def test_cross_branch_reread_refreshes_history_and_fences_new_current_version(app):
    async def scenario(api, client):
        p, r = await project_and_run(api)
        other = await api.write("/branches", {"source_branch_id": p["branch_id"], "name": "other"})
        source = await api.write(
            "/objects", {"branch_id": other["branch_id"], "kind": "claim", "body": "旧条件 $x>0$"}
        )
        newer = None

        class Script:
            async def generate(self, task):
                if not task["previous_steps"]:
                    return result(
                        "读取跨分支前提",
                        [
                            action(
                                "read_object",
                                object_id=source["object_id"],
                                branch_id=other["branch_id"],
                            )
                        ],
                    )
                old = task["previous_steps"][0]["actions"][0]["result"]
                assert old["revision_id"] == source["revision_id"]
                assert (
                    old["body"] == "旧条件 $x>0$"
                    and old["current"] is False
                    and old["stale"] is True
                )
                assert old["current_revision_id"] == newer["revision_id"]
                latest = next(i for i in task["inputs"] if i["object_id"] == source["object_id"])
                assert latest["revision_id"] == newer["revision_id"] and latest["current"]
                with app.state.database.sessions() as session:
                    attempt = session.get(Attempt, task["attempt_id"])
                    assert attempt.checkpoint["external_read_set"] == [
                        {
                            "branch_id": other["branch_id"],
                            "object_id": source["object_id"],
                            "revision_id": newer["revision_id"],
                        }
                    ]
                await api.write(
                    f"/objects/{source['object_id']}/revisions",
                    {
                        "branch_id": other["branch_id"],
                        "expected_revision_id": newer["revision_id"],
                        "body": "再次修改 $x>2$",
                    },
                )
                return result(
                    "在途旧依据必须隔离",
                    [action("write_draft", body="不应直接应用")],
                    next_action="finish",
                )

        worker = HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0)
        first = await api.write(f"/runs/{r['run_id']}/claim", worker=True)
        await worker.execute(first)
        newer = await api.write(
            f"/objects/{source['object_id']}/revisions",
            {
                "branch_id": other["branch_id"],
                "expected_revision_id": source["revision_id"],
                "body": "新条件 $x>1$",
            },
        )
        second = await api.write(f"/runs/{r['run_id']}/claim", worker=True)
        await worker.execute(second)
        steps = (await api.get(f"/runs/{r['run_id']}/steps"))["steps"]
        assert steps[0]["actions"][0]["result"]["current"] is True  # immutable original receipt
        assert steps[1]["receipt"]["quarantined"] and steps[1]["actions"] == []
        runtime = Runtime(app.state.service)
        with app.state.database.sessions() as session:
            run = session.get(Run, r["run_id"])
            output = session.get(Revision, steps[0]["output_revision_id"])
            page = read(
                runtime,
                session,
                run,
                object_id=output.object_id,
                revision_id=output.id,
                section="record",
                max_chars=20000,
            )
            record = json.loads(page["body"])
            assert record["operation_receipts"][0]["actions"] == steps[0]["actions"]

    exercise(app, scenario)


def test_long_instruction_is_readable_from_fixed_attempt_and_search_has_historical_branch(app):
    async def scenario(api, client):
        instruction = "保持完整的条件 $\\alpha>0$。" * 1300
        p, r = await project_and_run(api, instruction=instruction)
        other = await api.write("/branches", {"source_branch_id": p["branch_id"], "name": "source"})
        source = await api.write(
            "/objects",
            {"branch_id": other["branch_id"], "kind": "claim", "body": "历史关键词 条件 $x>0$"},
        )
        new = await api.write(
            f"/objects/{source['object_id']}/revisions",
            {
                "branch_id": other["branch_id"],
                "expected_revision_id": source["revision_id"],
                "body": "新版本 $x>1$",
            },
        )
        task = await api.write(f"/runs/{r['run_id']}/claim", worker=True)
        assert len(task["instruction"]) < len(instruction)
        runtime = Runtime(app.state.service)
        with app.state.database.sessions() as session:
            run = session.get(Run, r["run_id"])
            args = {**task["context_summary"]["run_context_read_ref"], "max_chars": 20000}
            chunks = []
            while True:
                page = read(runtime, session, run, **args)
                chunks.append(page["body"])
                if page["next_offset"] is None:
                    break
                args["offset"] = page["next_offset"]
            assert json.loads("".join(chunks))["run_context"]["instruction"] == instruction
            found = runtime.agent.execute_action(
                session, run, AgentAction(type="search_project", arguments={"query": "历史关键词"})
            )["items"][0]
            assert found["branch_id"] == other["branch_id"] and found["current"] is False
            assert found["current_revision_id"] == new["revision_id"]
            assert found["read_ref"]["revision_id"] == source["revision_id"]
            foreign = await api.write(
                "/projects", {"title": "foreign", "body": "历史关键词 foreign"}
            )
            with pytest.raises(DomainError) as error:
                read(
                    runtime,
                    session,
                    run,
                    object_id=foreign["object_id"],
                    branch_id=foreign["branch_id"],
                )
            assert error.value.response["error"] == "cross_project_read"
            with pytest.raises(DomainError) as error:
                read(
                    runtime,
                    session,
                    run,
                    object_id=p["object_id"],
                    section="record",
                    context_attempt_id="foreign-attempt",
                )
            assert error.value.response["error"] == "invalid_context_attempt"

    exercise(app, scenario)


def test_worker_repairs_a_large_escaped_response_inside_actual_prompt_limit(app):
    from mathagent.providers.remote import ProviderFailure

    async def scenario(api, client):
        p, r = await project_and_run(api, instruction="\x00" * 50000)
        calls = []

        class Repair:
            async def generate(self, task):
                calls.append(sum(len(m["content"]) for m in messages_for(task)))
                assert calls[-1] < 190000
                if len(calls) == 1:
                    raise ProviderFailure(
                        "invalid_structured_output",
                        outcome="spent",
                        observation={
                            "raw_text": "\x00" * 30000,
                            "complete": True,
                            "finish_reason": "stop",
                            "usage": {"total_tokens": 7},
                            "provider_request_id": "synthetic-malformed",
                        },
                    )
                assert task["repair_output"]
                assert task["inputs"][0]["body_sha256"] == digest("对实数 $x$，研究 $x^2+1>0$。")
                return result("格式修复只保存候选。", next_action="finish")

        await HTTPWorker(client, provider_factory=lambda _: Repair(), fake_delay_seconds=0).run(
            once=True
        )
        assert len(calls) == 2
        assert (await api.get(f"/runs/{r['run_id']}/budget"))["spent"] == 2
        assert len((await api.get(f"/runs/{r['run_id']}/steps"))["steps"]) == 1

    exercise(app, scenario)


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
