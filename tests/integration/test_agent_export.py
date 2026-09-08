"""Research ZIPs retain autonomous task families, calls and actual revision closure."""

import hashlib
import io
import json
import zipfile

from mathagent.exports.service import ExportService, export_zip
from mathagent.persistence.agent_models import AgentRun, ProviderCall
from mathagent.persistence.models import Attempt
from mathagent.runtime.worker import HTTPWorker
from sqlalchemy import select
from test_autonomous_agent import action, exercise, project_and_run, result
from test_autonomous_agent import app as app


def test_agent_export_contains_cross_branch_family_history_calls_and_no_credentials(app):
    async def scenario(api, client):
        project, parent = await project_and_run(api)
        branch = await api.write(
            "/branches", {"source_branch_id": project["branch_id"], "name": "子研究分支"}
        )
        premise = await api.write(
            "/objects",
            {
                "branch_id": branch["branch_id"],
                "kind": "claim",
                "body": "历史读取对象：$x^2+1>0$。",
            },
        )
        unrelated = await api.write(
            "/runs",
            {
                "branch_id": branch["branch_id"],
                "goal_object_id": premise["object_id"],
                "autonomous": False,
            },
        )
        await api.write(f"/runs/{unrelated['run_id']}/interventions", {"action": "pause"})

        class Script:
            async def generate(self, task):
                if task["mode"] == "review":
                    return result(
                        "对指定旧版本进行模拟局部审查。",
                        mode="review",
                        verdict="inconclusive",
                        next_action="finish",
                    )
                if task["run_id"] != parent["run_id"]:
                    return result(
                        "子任务留下有精确依赖的候选论证。",
                        [
                            action(
                                "propose_proof",
                                conclusion_revision_id=premise["revision_id"],
                                body="$x^2\\ge0$，所以 $x^2+1>0$。",
                            )
                        ],
                        next_action="finish",
                    )
                history = task["previous_steps"]
                if not history:
                    return result(
                        "读取其他分支材料并委派研究。",
                        [
                            action(
                                "read_object",
                                object_id=premise["object_id"],
                                branch_id=branch["branch_id"],
                            ),
                            action(
                                "spawn_task",
                                goal_object_id=premise["object_id"],
                                branch_id=branch["branch_id"],
                                instruction="检查正性",
                                request_budget=2,
                            ),
                        ],
                        next_action="wait",
                    )
                if len(history) == 1:
                    return result(
                        "另开审查会话，绑定该目标。",
                        [
                            action(
                                "request_review",
                                target_revision_id=project["revision_id"],
                                instruction="仅检查该版本的局部结论",
                            )
                        ],
                        next_action="wait",
                    )
                return result(
                    "结合子任务与局部审查，保存明确的新问题版本。",
                    [
                        action(
                            "revise_object",
                            object_id=project["object_id"],
                            expected_revision_id=project["revision_id"],
                            body="将待研究目标写为 $x^2+1\\ge1>0$。",
                        )
                    ],
                    next_action="finish",
                )

        await HTTPWorker(client, provider_factory=lambda _: Script(), fake_delay_seconds=0).run(
            once=True
        )
        steps = (await api.get(f"/runs/{parent['run_id']}/steps"))["steps"]
        assert len(steps) == 3 and all(
            a["status"] == "completed" for s in steps for a in s["actions"]
        )
        child_ids = {
            steps[0]["actions"][1]["result"]["run_id"],
            steps[1]["actions"][0]["result"]["run_id"],
        }
        changed = await api.write(
            f"/objects/{premise['object_id']}/revisions",
            {
                "branch_id": branch["branch_id"],
                "expected_revision_id": premise["revision_id"],
                "body": "已改成另一版本 $x^2+2>0$。",
            },
        )
        await api.write(
            f"/branches/{branch['branch_id']}/runtime-settings",
            {"request_budget": 17},
            method="PUT",
        )
        forbidden = {"synthetic-call-secret", "synthetic-agent-secret"}
        with app.state.database.sessions.begin() as session:
            forbidden.update(session.scalars(select(Attempt.token)))
            config = session.get(AgentRun, parent["run_id"])
            config.options = {**config.options, "token": "synthetic-agent-secret"}
            call = session.scalar(select(ProviderCall))
            call.call_config = {**call.call_config, "authorization": "synthetic-call-secret"}
            call.raw_text_truncated = True
            call.finish_reason = "length"
            call.complete = False
            altered_request = call.request_id
        created = await api.write(
            "/exports",
            {
                "project_id": project["project_id"],
                "branch_id": project["branch_id"],
                "object_ids": [project["object_id"]],
            },
        )
        bundle = ExportService(app.state.service).get(created["export_id"])
        contents = export_zip(bundle)
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            manifest_bytes = archive.read("manifest.json")
            manifest = json.loads(manifest_bytes)
            draft = archive.read("research.md")
        for secret in forbidden - {""}:
            assert secret.encode() not in manifest_bytes and secret.encode() not in draft
        exported = {r["id"]: r for r in manifest["snapshot"]["runs"]}
        assert set(exported) == {parent["run_id"], *child_ids}
        assert unrelated["run_id"] not in exported
        revisions = {r["id"]: r for r in manifest["revisions"]}
        assert premise["revision_id"] in revisions
        assert changed["revision_id"] in revisions  # live child goal is also identified
        assert steps[2]["actions"][0]["result"]["revision_id"] in revisions
        assert (
            project["revision_id"] in revisions
        )  # the independent review remains on its old target
        all_calls, all_steps = [], []
        for run_id, run in exported.items():
            actual_calls = (await api.get(f"/runs/{run_id}/calls"))["calls"]
            actual_steps = (await api.get(f"/runs/{run_id}/steps"))["steps"]
            assert len(run["provider_calls"]) == len(actual_calls)
            assert run["steps"] == actual_steps
            all_calls.extend(run["provider_calls"])
            all_steps.extend(run["steps"])
            if run_id in child_ids:
                assert run["agent"]["parent_run_id"] == parent["run_id"]
                assert run["agent"]["root_run_id"] == parent["run_id"]
            for attempt in run["attempts"]:
                assert "token" not in attempt
                assert set(attempt["read_set"].values()) <= revisions.keys()
                assert (
                    set(attempt["checkpoint"].get("context_revision_ids", [])) <= revisions.keys()
                )
                assert attempt["output_revision_id"] in revisions
        assert len(all_calls) == 5 and len(all_steps) == 4
        for call in all_calls:
            assert call["call_config"] and call["raw_text"] and call["usage"]
            assert call["raw_sha256"] == hashlib.sha256(call["raw_text"].encode()).hexdigest()
        retained = next(c for c in all_calls if c["request_id"] == altered_request)
        assert retained["raw_text_truncated"] and retained["finish_reason"] == "length"
        assert retained["complete"] is False
        for step in all_steps:
            assert step["output_revision_id"] in revisions
            for item in step["actions"]:
                revision_id = item.get("result", {}).get("revision_id")
                if revision_id:
                    assert revision_id in revisions
        runtime_branches = {
            item["branch"]["id"]: item for item in manifest["snapshot"]["runtime_branches"]
        }
        assert set(runtime_branches) == {project["branch_id"], branch["branch_id"]}
        assert runtime_branches[branch["branch_id"]]["settings"]["request_budget"] == 17
        assert manifest["snapshot"]["run_scope"] == "selected_branch_and_related_task_families"
        assert any(e["payload"].get("run_id") in child_ids for e in manifest["snapshot"]["events"])
        assert b"provider_calls" in draft and b"raw_text_truncated" in draft
        await api.write(
            f"/branches/{branch['branch_id']}/runtime-settings",
            {"request_budget": 18},
            method="PUT",
        )
        assert export_zip(ExportService(app.state.service).get(created["export_id"])) == contents

    exercise(app, scenario)
