"""Opt-in, six-request public-math acceptance. Never invoked by the test suite.

Uses the real adapter and HTTP worker against an isolated ASGI API database.
Independent OS-process/loopback-HTTP recovery is exercised separately offline.
"""

import argparse
import asyncio
import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from mathagent.api.app import create_app
from mathagent.config import load_local_environment
from mathagent.providers.remote import ProviderConfig, RemoteProvider, provider_status
from mathagent.runtime.worker import HTTPWorker


async def execute(directory, provider_name):
    config = ProviderConfig.from_env(provider_name)
    if not config.ready():
        raise RuntimeError("Selected provider is not configured and enabled")
    human, worker = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    app = create_app(directory / "acceptance.sqlite3", token=human, worker_token=worker)
    app.state.database.migrate()
    report = {"checked_at": datetime.now(UTC).isoformat(), "provider": provider_name,
              "simulated": False, "request_cap": 6, "model_web_tools": False,
              "mathematical_correctness_verified": False,
              "api_transport": "isolated ASGI HTTP; OS-process recovery tested separately",
              "provider_status": provider_status(), "checks": {}}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000",
                                     headers={"Authorization": "Bearer " + worker}) as client:
            async def write(path, payload=None, method="POST"):
                response = await client.request(method, path, json=payload, headers={
                    "Authorization": "Bearer " + human, "Idempotency-Key": str(uuid4())})
                if not response.is_success:
                    raise RuntimeError(f"Local acceptance command failed: {response.status_code}")
                return response.json()

            async def read(path):
                response = await client.get(path, headers={"Authorization": "Bearer " + human})
                response.raise_for_status()
                return response.json()

            project = await write("/projects", {"title": "真实功能验收 · 对称不等式（非 IMO）",
                "body": r"对任意实数 $a,b,c$，证明 $(a+b+c)^2\ge3(ab+bc+ca)$，并确定等号条件。"})
            branch_id, project_id = project["branch_id"], project["project_id"]
            claim = await write("/objects", {"branch_id": branch_id, "kind": "claim",
                "body": r"对任意实数 $a,b,c$，$(a+b+c)^2\ge3(ab+bc+ca)$，等号当且仅当 $a=b=c$。"})
            await write(f"/projects/{project_id}/runtime-settings", {"request_budget": 6,
                "allow_real_api": True, "allowed_providers": [provider_name]}, "PUT")
            research = await write("/runs", {"branch_id": branch_id, "goal_object_id": claim["object_id"],
                "provider": provider_name, "autonomous": True, "request_budget": 4,
                "solver_controller": "continuous_research", "discussion": True, "max_output_tokens": 8192,
                "instruction": "这是功能验收，不是 IMO。主研究者与同伴共用四次请求。"
                "保存简洁的个人工作稿，用原题和中立目标邀请一名同伴先独立探索，随后交换具体推导。"
                "依据实际推导交付普通数学正文；已有完整结果时直接提交，不需要审批或复制式收尾。"
                "没有解决就明确提交未完成的研究，不要冒充证明。不开放网页检索或宿主代码执行。" })
            print("Real research started; shared cap is four requests.", flush=True)
            await HTTPWorker(client, providers=[provider_name], concurrency=1).run(once=True)
            snapshot = await read(f"/projects/{project_id}/snapshot")
            steps = (await read(f"/runs/{research['run_id']}/steps"))["steps"]
            report["project_id"] = project_id
            report["research_run_id"] = research["run_id"]
            report["research_steps"] = steps
            state = await read(f"/runs/{research['run_id']}/research")
            report["checks"].update({
                "worknote_saved": any(member["personal_note"] for member in state["members"]),
                "persistent_independent_peer": len(state["members"]) == 2 and
                    any(work["independent"] for work in state["work"]),
                "explicit_submission": state["session"]["solution_revision_id"] is not None,
                "research_completed": next(r for r in snapshot["runs"] if r["id"] == research["run_id"])["state"] == "completed",
                "no_automatic_adoption": all(o["adoption_state"] == "draft" for o in snapshot["objects"]),
            })
            print("Research finished; checking an accepted request during a human edit.", flush=True)
            intervention = await write("/runs", {"branch_id": branch_id, "goal_object_id": claim["object_id"],
                "provider": provider_name, "autonomous": False, "request_budget": 2, "max_output_tokens": 8192,
                "instruction": "独立写出此命题的简短证明与等号条件，用LaTeX；无需调用其他操作。"})
            edited = None

            async def edit_when_accepted(response):
                nonlocal edited
                if response.status_code == 200 and edited is None:
                    edited = await write(f"/objects/{claim['object_id']}/revisions", {"branch_id": branch_id,
                        "expected_revision_id": claim["revision_id"],
                        "body": r"现在限制为整数 $a,b,c\in\mathbb{Z}$：证明 $(a+b+c)^2\ge3(ab+bc+ca)$，并确定等号条件。"})
                    print("Provider returned success headers; human changed the goal before output completion.", flush=True)

            async with httpx.AsyncClient(event_hooks={"response": [edit_when_accepted]}, trust_env=False,
                                         follow_redirects=False) as remote:
                await HTTPWorker(client, providers=[provider_name], concurrency=1,
                    provider_factory=lambda _: RemoteProvider(config, remote)).run(once=True)
            snapshot = await read(f"/projects/{project_id}/snapshot")
            interrupted = next(r for r in snapshot["runs"] if r["id"] == intervention["run_id"])
            report["checks"]["accepted_request_edit_quarantined"] = bool(edited and interrupted["attempts"] and
                interrupted["attempts"][0]["state"] == "quarantined")
            if report["checks"]["accepted_request_edit_quarantined"]:
                await write(f"/runs/{intervention['run_id']}/resume")
                await HTTPWorker(client, providers=[provider_name], concurrency=1).run(once=True)
            snapshot = await read(f"/projects/{project_id}/snapshot")
            resumed = next(r for r in snapshot["runs"] if r["id"] == intervention["run_id"])
            report["checks"]["resume_reads_new_revision"] = bool(edited and len(resumed["attempts"]) == 2 and
                resumed["attempts"][1]["read_set"].get(claim["object_id"]) == edited["revision_id"] and resumed["state"] == "completed")
            report["intervention_run_id"] = intervention["run_id"]
            budget = await read(f"/projects/{project_id}/budget")
            report["budget"] = budget
            report["checks"]["six_request_cap"] = budget["occupied"] <= 6
            report["checks"]["no_unknown_requests"] = budget["unknown"] == 0
            report["calls"] = []
            for run in snapshot["runs"]:
                report["calls"].extend((await read(f"/runs/{run['id']}/calls"))["calls"])
            report["checks"]["frozen_config_and_usage"] = all(c["call_config"].get("prompt_sha256") and c["raw_sha256"]
                and c["provider_request_id"] and c["usage"] for c in report["calls"])
            export = await write("/exports", {"project_id": project_id, "branch_id": branch_id})
            response = await client.get(f"/exports/{export['export_id']}/download", headers={"Authorization": "Bearer " + human})
            response.raise_for_status()
            (directory / "research-export.zip").write_bytes(response.content)
            report["passed"] = all(report["checks"].values())
    finally:
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        app.state.database.close()
    print(json.dumps({"passed": report["passed"], "checks": report["checks"],
                      "request_count": report["budget"]["occupied"], "report": str(directory / "report.json")}, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Authorize this one bounded paid validation")
    parser.add_argument("--provider", choices=["deepseek", "glm"], default="deepseek")
    args = parser.parse_args()
    if not args.execute:
        parser.error("No API calls made. Pass --execute only when this bounded run is authorized.")
    load_local_environment()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    output = Path("data/acceptance") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=False)
    try:
        passed = asyncio.run(execute(output, args.provider))
    except Exception as error:
        # Exception messages/URLs can contain service credentials.
        print(json.dumps({"error_type": type(error).__name__, "report_directory": str(output)}))
        raise SystemExit(1) from None
    if not passed:
        raise SystemExit(1)
