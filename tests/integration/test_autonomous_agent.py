"""End-to-end autonomous research proposals over HTTP, using synthetic model outputs."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from mathagent.api.app import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "agent.sqlite3", token="human", worker_token="worker")
    app.state.database.migrate()
    yield app
    app.state.database.close()


def result(body, actions=(), *, next_action="continue", mode="research", verdict=None):
    return {"result": {"mode": mode, "body": body, "findings": ["仅为工程测试夹具"],
                       "cited_revision_ids": [], "scope": "检查指定版本的局部计算。" if mode == "review" else None,
                       "verdict": verdict, "actions": list(actions), "next_action": next_action},
            "usage": {"total_tokens": 10}, "provider_request_id": "synthetic-request"}


def action(name, **arguments):
    return {"type": name, "arguments": arguments}


class Client:
    def __init__(self, client):
        self.client = client

    async def write(self, path, payload=None, *, method="POST", worker=False, expect=None):
        r = await self.client.request(method, path, json=payload,
               headers={"Authorization": "Bearer worker" if worker else "Bearer human", "Idempotency-Key": str(uuid4())})
        assert r.status_code == expect if expect else r.is_success, r.text
        return r.json()

    async def get(self, path):
        r = await self.client.get(path, headers={"Authorization": "Bearer human"})
        assert r.is_success, r.text
        return r.json()


async def project_and_run(api, **overrides):
    p = await api.write("/projects", {"title": "自主流程夹具", "body": "对实数 $x$，研究 $x^2+1>0$。"})
    r = await api.write("/runs", {"branch_id": p["branch_id"], "goal_object_id": p["object_id"],
                                "autonomous": True, "request_budget": 12, **overrides})
    return p, r


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://127.0.0.1:8000",
                                    headers={"Authorization": "Bearer worker"}) as c:
            await scenario(Client(c), c)
    asyncio.run(run())
