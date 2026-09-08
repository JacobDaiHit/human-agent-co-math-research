"""Exercise the opt-in paid harness with deterministic wire responses first."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
from test_autonomous_agent import action, result


def test_six_request_harness_is_reproducible_without_external_network(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "1")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")
    spec = importlib.util.spec_from_file_location("real_acceptance", Path(__file__).resolve().parents[2] / "scripts/acceptance_real.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    requests = []

    def wire(request):
        payload = json.loads(request.content)
        task = json.loads(payload["messages"][1]["content"])
        requests.append(task)
        if task["mode"] == "review":
            output = result("恒等式和等号条件在此局部推导成立。", mode="review", verdict="passed", next_action="finish")
        elif task["instruction"].startswith("独立写"):
            output = result(r"由平方差和非负可证；等号当且仅当 $a=b=c$。", next_action="finish")
        elif not task["previous_steps"]:
            output = result("登记实际计算和证明。", [action("calculate", tool="polynomial_identity", inputs={
                "left": "2*((a+b+c)**2-3*(a*b+b*c+c*a))", "right": "(a-b)**2+(b-c)**2+(c-a)**2", "variables": ["a", "b", "c"]}),
                action("propose_proof", conclusion_revision_id=task["target_revision_id"], body=r"差式等于平方差和的一半，故非负；等号当且仅当 $a=b=c$。")])
        elif len(task["previous_steps"]) == 1:
            proof = task["previous_steps"][0]["actions"][1]["result"]
            output = result("请求独立审查。", [action("request_review", target_revision_id=proof["revision_id"])], next_action="wait")
        else:
            output = result("此论证与局部审查已保存，仍为未采纳草稿。", next_action="finish")
        return httpx.Response(200, json={"id": f"fixture-{len(requests)}", "usage": {"total_tokens": 11},
            "choices": [{"message": {"content": json.dumps(output["result"], ensure_ascii=False)}, "finish_reason": "stop"}]})

    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(wire)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    passed = asyncio.run(harness.execute(tmp_path, "deepseek"))
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert len(requests) == 6
    assert passed is True
    assert report["passed"], report["checks"]
    assert (tmp_path / "research-export.zip").is_file()
    assert "synthetic-key" not in json.dumps(report)
