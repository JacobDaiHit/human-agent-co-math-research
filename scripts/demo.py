"""Replay the M0/M1 version-intervention scenario with explicit deterministic fixtures."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient
from mathagent.api.app import create_app


def demonstrate(directory):
    os.environ["MATHAGENT_LOAD_ENV"] = "0"
    os.environ["MATHAGENT_ENABLE_REAL_API"] = "0"
    app = create_app(Path(directory) / "demo.db", token="demo-human", worker_token="demo-worker")
    with TestClient(app) as client:

        def post(path, payload=None, worker=False):
            response = client.post(
                path,
                json=payload,
                headers={
                    "Authorization": "Bearer demo-worker" if worker else "Bearer demo-human",
                    "Idempotency-Key": str(uuid4()),
                },
            )
            assert response.status_code in {200, 201, 202}, response.text
            return response.json()

        project = post(
            "/projects",
            {"title": "版本干预固定演示", "body": "研究过程中改写引理后，如何保留正确的研究状态？"},
        )
        branch = project["branch_id"]

        def obj(body):
            return post("/objects", {"branch_id": branch, "kind": "claim", "body": body})

        def adopt(revision_id):
            post(
                "/adoptions",
                {
                    "branch_id": branch,
                    "revision_id": revision_id,
                    "state": "adopted",
                    "reason": "确定性工程测试夹具中的采用记录。",
                },
            )

        def proof(claim, premises, body):
            plan = post(
                "/proof-plans",
                {
                    "branch_id": branch,
                    "conclusion_revision_id": claim["revision_id"],
                    "body": body,
                    "premise_revision_ids": [p["revision_id"] for p in premises],
                },
            )
            adopt(claim["revision_id"])
            adopt(plan["revision_id"])
            post(
                "/reviews",
                {
                    "branch_id": branch,
                    "target_revision_id": plan["revision_id"],
                    "kind": "human_review",
                    "verdict": "passed",
                    "coverage": "whole_plan",
                    "scope": "固定样例的完整初等推导；不是真实模型生成或独立科研发现。",
                    "findings": ["此审查条目由演示脚本预置，用于检查版本绑定。"],
                },
            )
            return plan

        lemma = obj(r"对实数 $x$，$x^2 \ge 0$。")
        proof(lemma, [], "实数平方非负。")
        conclusion = obj("对实数 $x$，$x^2 + 1 > 0$。")
        dependent = proof(conclusion, [lemma], r"由 $x^2 \ge 0$，加 $1$ 得到 $x^2 + 1 \ge 1 > 0$。")
        alternative = proof(
            conclusion, [], r"若 $x\ge0$，则 $x\cdot x\ge0$；若 $x<0$，则 $(-x)\cdot(-x)\ge0$；加 $1$ 后严格为正。"
        )
        run = post(
            "/runs",
            {
                "branch_id": branch,
                "goal_object_id": conclusion["object_id"],
                "instruction": "模拟继续研究；仅用于版本干预测试。",
            },
        )
        attempt = post(f"/runs/{run['run_id']}/claim", worker=True)
        revision = post(
            f"/objects/{lemma['object_id']}/revisions",
            {
                "branch_id": branch,
                "expected_revision_id": lemma["revision_id"],
                "body": r"对复数 $z$，$|z|^2 \ge 0$。",
                "payload": {"change": "定义域及表达式变化，需要重审"},
            },
        )
        late = post(
            f"/attempts/{attempt['attempt_id']}/complete",
            {
                "token": attempt["token"],
                "body": attempt["scripted_output"],
            },
            worker=True,
        )
        post(f"/runs/{run['run_id']}/resume")
        resumed = post(f"/runs/{run['run_id']}/claim", worker=True)
        post(
            f"/attempts/{resumed['attempt_id']}/complete",
            {
                "token": resumed["token"],
                "body": resumed["scripted_output"],
            },
            worker=True,
        )
        snapshot = client.get(
            f"/projects/{project['project_id']}/snapshot",
            headers={"Authorization": "Bearer demo-human"},
        ).json()
        checks = {
            "old_plan_flagged": dependent["revision_id"] in revision["affected_plan_revision_ids"],
            "old_run_reported": run["run_id"] in revision["stale_run_ids"],
            "alternative_kept": snapshot["support"]["plans"][alternative["revision_id"]]["status"]
            == "supported",
            "conclusion_kept": snapshot["support"]["claims"][conclusion["revision_id"]]["status"]
            == "supported",
            "late_output_quarantined": late["quarantined"] and late["output_branch_id"] != branch,
            "resumed_uses_new_revision": resumed["read_set"][lemma["object_id"]]
            == revision["revision_id"],
            "new_attempt_created": resumed["attempt_id"] != attempt["attempt_id"],
        }
        assert all(checks.values()), checks
        return {
            "scenario": "M0-M1 running lemma edit",
            "simulated": True,
            "real_model_calls": 0,
            "checks": checks,
            "note": "所有数学材料和审查记录均为人工编写的测试夹具；未进行自主科研。",
        }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        result = demonstrate(directory)
    target = Path(__file__).resolve().parents[1] / "docs" / "acceptance" / "m0-m1-demo.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
