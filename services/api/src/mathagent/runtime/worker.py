"""Independent HTTP worker. Start explicitly; fake mode never opens a provider socket.

Run ``python -m mathagent.runtime.worker --help``. The API is the sole writer;
every external dispatch has a committed ledger reservation and dispatch marker.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from mathagent.config import EnvironmentFileError, load_local_environment
from mathagent.providers.protocol import PROMPT_VERSION
from mathagent.providers.remote import DeepSeekProvider, GLMProvider, ProviderFailure

log = logging.getLogger("mathagent.worker")


class WorkerAPIError(Exception):
    """Sanitized API failure: never include response bodies, URLs or credentials."""


class HTTPWorker:
    def __init__(
        self,
        client,
        *,
        providers=("fake",),
        concurrency=2,
        fake_delay_seconds=4.0,
        poll_seconds=0.5,
        provider_factory=None,
    ):
        if concurrency not in {1, 2}:
            raise ValueError("Worker concurrency must be 1 or 2")
        if not providers or not set(providers).issubset({"fake", "deepseek", "glm"}):
            raise ValueError("Unsupported provider")
        if fake_delay_seconds < 0 or poll_seconds <= 0:
            raise ValueError("Worker intervals must be nonnegative, with positive polling")
        self.client = client
        self.providers = list(dict.fromkeys(providers))
        self.concurrency = concurrency
        self.fake_delay_seconds = fake_delay_seconds
        self.poll_seconds = poll_seconds
        self.custom_provider = provider_factory is not None
        self.provider_factory = provider_factory or (
            lambda name: {"deepseek": DeepSeekProvider, "glm": GLMProvider}[name]()
        )

    async def _post(self, path, payload):
        # A lost HTTP response is retried with the SAME command key. In particular,
        # a reservation/start/settlement retry can never create a second charge.
        key = str(uuid4())
        for retry in range(3):
            try:
                response = await self.client.post(
                    path, json=payload, headers={"Idempotency-Key": key}
                )
                if response.status_code >= 500 and retry < 2:
                    await asyncio.sleep(0.2 * (retry + 1))
                    continue
                if not response.is_success:
                    raise WorkerAPIError(f"api_http_{response.status_code}")
                return response.json()
            except (httpx.TransportError, ValueError):
                if retry == 2:
                    raise WorkerAPIError("api_outcome_unknown") from None
                await asyncio.sleep(0.2 * (retry + 1))
        raise WorkerAPIError("api_unavailable")

    async def _heartbeat(self, task, ended):
        interval = max(0.1, min(10.0, task["lease_seconds"] / 3))
        while not ended.is_set():
            try:
                await asyncio.wait_for(ended.wait(), timeout=interval)
            except TimeoutError:
                try:
                    await self._post(
                        f"/attempts/{task['attempt_id']}/heartbeat",
                        {"token": task["token"], "boundary": False},
                    )
                except WorkerAPIError:
                    # Do not abort a request in flight: it may already be billed.
                    # Settle/complete will fence this lease or preserve unknown state.
                    return

    async def _safe_fail(self, task, reason):
        with contextlib.suppress(WorkerAPIError):
            await self._post(
                f"/attempts/{task['attempt_id']}/fail", {"token": task["token"], "reason": reason}
            )

    async def execute(self, task):
        ended = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(task, ended))
        attempt_path = f"/attempts/{task['attempt_id']}"
        execution = {"token": task["token"]}
        repaired = False
        try:
            provider = self.provider_factory(task["provider"]) if task["provider"] != "fake" or self.custom_provider else None
            for retry in range(3):
                boundary = await self._post(
                    attempt_path + "/heartbeat", {**execution, "boundary": True}
                )
                if not boundary["continue"]:
                    return
                reservation = await self._post(attempt_path + "/requests", execution)
                if not reservation["continue"]:
                    return
                request_path = f"/requests/{reservation['request_id']}"
                config = provider.describe(task) if provider and hasattr(provider, "describe") else {
                    "provider": task["provider"], "model": "synthetic-fixture" if task["provider"] == "fake" else "mock-provider",
                    "parameters": {}, "prompt_template_version": PROMPT_VERSION,
                    "simulated": task["provider"] == "fake",
                }
                await self._post(request_path + "/observation", {
                    **execution, "observation": {"call_config": config, "raw_text": "", "complete": False}
                })
                started = await self._post(request_path + "/start", execution)
                if not started["continue"]:
                    return
                try:
                    if provider is None:
                        await asyncio.sleep(self.fake_delay_seconds)
                        result = {
                            "mode": task["mode"],
                            "body": task["scripted_output"],
                            "findings": ["模拟测试输出，未进行数学验证"],
                            "cited_revision_ids": list(task["read_set"].values())[:500],
                            "verdict": "inconclusive" if task["mode"] == "review" else None,
                        }
                        if task["mode"] == "review":
                            result["scope"] = "仅验证执行流程；未检查任何数学论证。"
                        output = {"result": result, "usage": {}, "provider_request_id": None}
                    else:
                        output = await provider.generate(task)
                except ProviderFailure as error:
                    observation = getattr(error, "observation", None) or {"raw_text": "", "complete": False}
                    observation = {**observation, "call_config": config}
                    await self._post(request_path + "/observation", {**execution, "observation": observation})
                    await self._post(
                        request_path + "/settle",
                        {
                            **execution,
                            "outcome": error.outcome,
                            "reason": error.code,
                            "usage": {k: v for k, v in observation.get("usage", {}).items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0},
                            "provider_request_id": observation.get("provider_request_id"),
                        },
                    )
                    if error.outcome == "unaccepted" and error.retryable and retry < 2:
                        await asyncio.sleep(0.5 * 2**retry)
                        continue
                    if error.code == "invalid_structured_output" and not repaired and retry < 2:
                        repaired = True
                        from mathagent.runtime.context import compact_task

                        task = compact_task({**task, "repair_output": observation.get("raw_text", "")[:30000]})
                        continue
                    await self._safe_fail(task, error.code)
                    return
                observation = output.get("observation") or {
                    "raw_text": json.dumps(output["result"], ensure_ascii=False), "finish_reason": "stop", "complete": True,
                    "usage": output.get("usage", {}), "provider_request_id": output.get("provider_request_id"),
                }
                observation = {**observation, "call_config": config}
                await self._post(request_path + "/observation", {**execution, "observation": observation, "result": output["result"]})
                # Provider usage is informational. Budget accounting uses request
                # count, and cannot be altered by vendor token usage metadata.
                usage = {
                    k: v
                    for k, v in output.get("usage", {}).items()
                    if isinstance(v, int) and not isinstance(v, bool) and v >= 0
                }
                await self._post(
                    request_path + "/settle",
                    {
                        **execution,
                        "outcome": "spent",
                        "usage": usage,
                        "provider_request_id": output.get("provider_request_id"),
                    },
                )
                if task.get("autonomous"):
                    await self._post(attempt_path + "/steps", {
                        **execution, "request_id": reservation["request_id"], "result": output["result"],
                    })
                else:
                    await self._post(
                        attempt_path + "/complete",
                        {**execution, "body": output["result"]["body"], "result": output["result"]},
                    )
                return
        except asyncio.CancelledError:
            # Graceful process shutdown tries to mark in-flight outcomes unknown.
            # If killed before this reaches the API, lease recovery does the same.
            await asyncio.shield(self._safe_fail(task, "worker_shutdown"))
            raise
        except WorkerAPIError:
            await self._safe_fail(task, "worker_api_unavailable")
        except Exception:
            # Never log exception text: SDK/transport errors may embed credentials.
            await self._safe_fail(task, "worker_execution_error")
            log.error("Worker execution failed; the ledger preserves dispatch outcome.")
        finally:
            ended.set()
            await heartbeat

    async def run(self, *, once=False, stop=None):
        stop = stop or asyncio.Event()

        async def slot():
            while not stop.is_set():
                try:
                    response = await self._post("/worker/claim-next", {"providers": self.providers})
                    if response["task"]:
                        await self.execute(response["task"])
                        continue
                    if once:
                        return
                except WorkerAPIError:
                    log.warning("API unavailable; waiting for the next poll.")
                    if once:
                        raise
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

        await asyncio.gather(*(slot() for _ in range(self.concurrency)))


def main():
    parser = argparse.ArgumentParser(description="MathAgent 独立 HTTP worker（默认只运行 fake）")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument(
        "--providers", nargs="+", choices=["fake", "deepseek", "glm"], default=["fake"]
    )
    parser.add_argument("--concurrency", type=int, choices=[1, 2], default=2)
    parser.add_argument("--fake-delay", type=float, default=4.0)
    parser.add_argument("--once", action="store_true", help="处理当前可领取队列后退出")
    args = parser.parse_args()
    try:
        load_local_environment()
    except EnvironmentFileError as error:
        parser.error(str(error))
    token_file = args.token_file or (
        Path(os.getenv("MATHAGENT_DATABASE", "data/mathagent.db")).parent / "worker.token"
    )
    parsed = urlparse(args.api_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", "/api", "/api/"}
    ):
        parser.error("API address must be an HTTP loopback address")
    token = os.getenv("MATHAGENT_WORKER_TOKEN", "")
    if not token:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            parser.error("Worker token file is unavailable; start the API first")
    if not token:
        parser.error("Worker token must be nonempty")

    async def serve():
        async with httpx.AsyncClient(
            base_url=args.api_url.rstrip("/"),
            trust_env=False,
            timeout=15,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + token},
        ) as client:
            await HTTPWorker(
                client,
                providers=args.providers,
                concurrency=args.concurrency,
                fake_delay_seconds=args.fake_delay,
            ).run(once=args.once)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
