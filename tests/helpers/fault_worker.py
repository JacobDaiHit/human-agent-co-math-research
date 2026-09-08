"""Disposable loopback API and deterministic HTTP-worker fault boundaries.

This helper is only invoked by test_process_recovery.py with a synthetic environment.
It never constructs a remote provider and rejects non-loopback socket connections.
"""

import argparse
import asyncio
import json
import os
import socket
from pathlib import Path

import httpx

OUTPUT_BODY = "Synthetic completed proof: $1+1=2$. No external model was called."


def local_sockets_only():
    connect = socket.socket.connect
    resolve = socket.getaddrinfo

    def guarded_connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            raise RuntimeError("Fault tests only permit loopback sockets.")
        return connect(sock, address)

    def guarded_resolve(host, *args, **kwargs):
        if host not in {None, "127.0.0.1", "::1", "localhost", b"127.0.0.1", b"localhost"}:
            raise RuntimeError("Fault tests do not resolve external hosts.")
        return resolve(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.getaddrinfo = guarded_resolve


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def serve_api(port):
    import mathagent.api.app as api_module
    import uvicorn

    original_runtime = api_module.Runtime
    # Test-only lease duration. Runtime logic, HTTP, SQLite, and process loss are real.
    api_module.Runtime = lambda state: original_runtime(state, lease_seconds=2)
    uvicorn.run(
        api_module.create_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


async def run_worker(api_url, stage, marker, journal):
    from mathagent.runtime.worker import HTTPWorker

    class FaultWorker(HTTPWorker):
        async def wait_at(self, phase, **fields):
            write_json(marker, {"phase": phase, "pid": os.getpid(), **fields})
            await asyncio.Event().wait()

        async def _post(self, path, payload):
            result = await super()._post(path, payload)
            if path.endswith("/requests") and result.get("continue"):
                self.request_id = result["request_id"]
                if stage == "before_dispatch":
                    await self.wait_at("reserved", request_id=self.request_id)
            if path.endswith("/observation") and payload.get("result") is not None:
                if stage in {"after_observation", "invalid_observation", "autonomous_observation"}:
                    await self.wait_at("observed", request_id=self.request_id)
            if path.endswith("/settle") and payload.get("outcome") == "spent":
                if stage == "after_settlement":
                    await self.wait_at("settled", request_id=self.request_id)
            if path.endswith("/complete") and stage == "duplicate_complete":
                replies = []
                for _ in range(2):
                    replay = await self.client.post(
                        path,
                        json=payload,
                        headers={"Idempotency-Key": "fault-test-repeat-" + path.split("/")[2]},
                    )
                    replay.raise_for_status()
                    replies.append(replay.json())
                write_json(
                    marker,
                    {
                        "phase": "completed_repeatedly",
                        "pid": os.getpid(),
                        "identical": result == replies[0] == replies[1],
                        "output_revision_id": result["output_revision_id"],
                    },
                )
            return result

    class SyntheticProvider:
        def describe(self, task):
            return {
                "provider": task["provider"],
                "model": "process-fault-synthetic",
                "parameters": {},
                "prompt_template_version": "fault-test-v1",
                "simulated": True,
            }

        async def generate(self, task):
            # This line models vendor acceptance. It is an append-only local fixture,
            # not a remote API request or an actual monetary charge.
            with journal.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "attempt_id": task["attempt_id"],
                            "provider_request_id": "synthetic-accepted-" + task["attempt_id"],
                        }
                    )
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            if stage == "in_flight":
                await worker.wait_at("accepted", request_id=worker.request_id)
            result = {
                "mode": task["mode"],
                "body": OUTPUT_BODY,
                "findings": ["A synthetic process-recovery fixture only."],
                "cited_revision_ids": list(task["read_set"].values()),
                "actions": [],
                "next_action": "finish",
            }
            if stage == "invalid_observation":
                result["cited_revision_ids"] = ["not-in-the-assigned-input-snapshot"]
            if stage == "autonomous_observation":
                result["actions"] = [
                    {
                        "type": "write_draft",
                        "arguments": {
                            "kind": "claim",
                            "body": "THIS OLD PROPOSAL MUST NOT EXECUTE.",
                        },
                    }
                ]
                result["next_action"] = "continue"
            return {
                "result": result,
                "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16},
                "provider_request_id": "synthetic-accepted-" + task["attempt_id"],
            }

    async with httpx.AsyncClient(
        base_url=api_url,
        timeout=5,
        trust_env=False,
        headers={"Authorization": "Bearer " + os.environ["MATHAGENT_WORKER_TOKEN"]},
    ) as client:
        worker = FaultWorker(
            client,
            providers=["deepseek"],
            concurrency=1,
            provider_factory=lambda _: SyntheticProvider(),
            poll_seconds=0.1,
        )
        await worker.run(once=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["api", "worker"])
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument(
        "--stage",
        default="normal",
        choices=[
            "normal",
            "before_dispatch",
            "in_flight",
            "after_observation",
            "after_settlement",
            "invalid_observation",
            "autonomous_observation",
            "duplicate_complete",
        ],
    )
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args()
    if os.environ.get("MATHAGENT_LOAD_ENV") != "0":
        parser.error("The fault helper requires local environment loading to be disabled.")
    local_sockets_only()
    if args.mode == "api":
        serve_api(args.port)
    else:
        if args.marker is None or args.journal is None:
            parser.error("Worker mode requires marker and journal paths.")
        asyncio.run(
            run_worker(f"http://127.0.0.1:{args.port}", args.stage, args.marker, args.journal)
        )


if __name__ == "__main__":
    main()
