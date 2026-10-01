"""A receipt-driven worker chain exercises recording, lookup and bounded calculation."""

import json

from mathagent.persistence.artifacts import ArtifactStore
from mathagent.persistence.models import Adoption, Branch, Event, Review, Revision, Run
from mathagent.persistence.research_models import ResearchRecordReference
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_autonomous_agent import action, exercise, project_and_run, result
from test_autonomous_agent import app as app


def test_agent_record_lookup_branch_and_tool_chain_uses_actual_receipts_and_scoped_evidence(app):
    visited = []

    async def scenario(api, client):
        project, run = await project_and_run(api)
        source_fields = {
            "title": "合成来源标识 RECORD_CHAIN_SOURCE",
            "authors": ["合成作者"],
            "url_or_identifier": "urn:synthetic:record-chain",
            "locator": "命题 1，第 2 页",
            "accessed_on": "2026-09-08",
            "body": "RECORD_CHAIN_SOURCE：待核对的有理数计算来源。",
            "verification": "unverified",
        }

        class Script:
            async def generate(self, task):
                history = task["previous_steps"]
                visited.append(len(history))
                if not history:
                    return result(
                        "先保存来源、候选命题和分支，下一轮读取实际回执。",
                        [
                            action("record_source", **source_fields),
                            {"type": "create_branch", "arguments": {"name": "操作链派生分支"}},
                            action(
                                "write_draft", kind="claim", body="$\\frac{1}{3}+\\frac{2}{5}=1$。"
                            ),
                            action(
                                "write_draft",
                                kind="context",
                                body="仅在有理数域 $\\mathbb{Q}$ 中计算。",
                                payload={"role": "assumption"},
                            ),
                            action("record_source", **{**source_fields, "author": "human"}),
                        ],
                    )
                assert [item["type"] for item in task["operation_results"]] == [
                    item["type"] for item in history[-1]["actions"]
                ]
                if len(history) <= 2:
                    first = history[0]["actions"]
                    source, branch, claim, assumption = [a["result"] for a in first[:4]]
                    assert first[4]["status"] == "rejected"
                if len(history) == 1:
                    return result(
                        "使用回执中的目标版本计算，再检索刚保存的来源。",
                        [
                            action(
                                "calculate",
                                tool="rational_arithmetic",
                                inputs={"expression": "1/3+2/5"},
                                target_revision_id=claim["revision_id"],
                            ),
                            action("search_project", query="RECORD_CHAIN_SOURCE"),
                            action(
                                "read_object",
                                object_id=project["object_id"],
                                branch_id=branch["branch_id"],
                            ),
                        ],
                    )
                if len(history) == 2:
                    calculation = task["operation_results"][0]["result"]
                    assert calculation["exact_result"]["value"] == "11/15"
                    assert source["revision_id"] in {
                        item["revision_id"]
                        for item in task["operation_results"][1]["result"]["items"]
                    }
                    return result(
                        "只记录这一算术断言的错误与可重试条件。",
                        [
                            action(
                                "record_failure",
                                target_revision_id=claim["revision_id"],
                                assumption_revision_ids=[assumption["revision_id"]],
                                evidence_revision_ids=[calculation["revision_id"]],
                                outcome="argument_error",
                                body="RECORD_CHAIN_FAILURE：$\\frac{1}{3}+\\frac{2}{5}=\\frac{11}{15}\\ne1$。",
                                scope="只检查该版本的有理数等式；不推断项目原目标的真值。",
                                evidence_explanation="固定工具进行了精确分数运算。",
                                retry_conditions=["修正通分过程后重试该等式。"],
                            )
                        ],
                    )
                if len(history) == 3:
                    failure = task["operation_results"][0]["result"]
                    return result(
                        "回读已保存的失败版本并从项目检索确认。",
                        [
                            action(
                                "read_object",
                                object_id=failure["object_id"],
                                revision_id=failure["revision_id"],
                                section="payload",
                            ),
                            action("search_project", query="RECORD_CHAIN_FAILURE"),
                        ],
                    )
                read_back = task["operation_results"][0]["result"]
                assert read_back["section"] == "payload" and not read_back["excerpted"]
                stored_payload = json.loads(read_back["body"])
                assert stored_payload["outcome"] == "argument_error"
                assert read_back["revision_id"] in {
                    item["revision_id"] for item in task["operation_results"][1]["result"]["items"]
                }
                answer = result(
                    "工具结果与范围明确的失败记录均已保存，等待人工判断。", next_action="finish"
                )
                answer["result"]["cited_revision_ids"] = [
                    stored_payload["target_revision_id"],
                    *stored_payload["evidence_revision_ids"],
                    read_back["revision_id"],
                ]
                return answer

        await HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0).run(
            once=True
        )
        steps = (await api.get(f"/runs/{run['run_id']}/steps"))["steps"]
        assert visited == list(range(5)) and len(steps) == 5
        outcomes = [item for step in steps for item in step["actions"]]
        assert [item["status"] for item in outcomes].count("rejected") == 1
        assert steps[0]["actions"][4]["error"] == "invalid_operation_arguments"
        source, branch, claim, assumption = [a["result"] for a in steps[0]["actions"][:4]]
        calculation = steps[1]["actions"][0]["result"]
        failure = steps[2]["actions"][0]["result"]
        with app.state.database.sessions() as session:
            assert session.get(Run, run["run_id"]).state == "completed"
            assert session.get(Branch, branch["branch_id"]).parent_id == project["branch_id"]
            source_record = session.get(Revision, source["revision_id"])
            failure_record = session.get(Revision, failure["revision_id"])
            assert source_record.author == failure_record.author == "agent:fake"
            assert source_record.payload["authors"] == ["合成作者"]
            assert source_record.payload["verification"] == "unverified"
            assert failure_record.payload["outcome"] == "argument_error"
            refs = session.scalars(
                select(ResearchRecordReference).where(
                    ResearchRecordReference.record_revision_id == failure["revision_id"]
                )
            ).all()
            assert {(r.role, r.target_revision_id) for r in refs} == {
                ("target", claim["revision_id"]),
                ("assumption", assumption["revision_id"]),
                ("evidence", calculation["revision_id"]),
            }
            review = session.get(Review, calculation["review_id"])
            assert review.kind == "exact_computation" and review.coverage == "partial"
            assert review.author == "exact_tool:rational_arithmetic"
            assert review.target_revision_id == claim["revision_id"]
            assert session.scalars(select(Adoption)).all() == []
            event = session.scalar(
                select(Event).where(
                    Event.type == "branch.created",
                    Event.branch_id == branch["branch_id"],
                )
            )
            assert event.author == "agent:fake"
        saved = json.loads(
            ArtifactStore(app.state.database.path).read(calculation["artifact_files"][0])
        )
        assert saved["exact_result"]["value"] == "11/15" and saved["status"] == "ok"
        snapshot = await api.get(f"/projects/{project['project_id']}/snapshot")
        assert all(obj["adoption_state"] == "draft" for obj in snapshot["objects"])
        assert snapshot["support"]["claims"][claim["revision_id"]]["status"] != "supported"
        assert (await api.get(f"/runs/{run['run_id']}/budget"))["spent"] == 5

    exercise(app, scenario)
