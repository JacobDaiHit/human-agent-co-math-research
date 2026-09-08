"""Closed-book, resumable batch execution through the real API and HTTP worker.

This module never opens an answer key and does not grade mathematics. Each case
has its own SQLite database and API process. Only the inference transport has an
external socket, restricted to the configured official completion endpoint.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import sysconfig
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
from mathagent.providers.remote import DEFAULT_URLS, RemoteProvider
from mathagent.runtime.worker import HTTPWorker

OPERATIONS = [
    "read_object", "search_project", "write_draft", "revise_object", "propose_proof",
    "record_failure", "record_source", "create_branch", "spawn_task", "request_review",
    "discuss", "calculate",
]
INSTRUCTION = r"""Solve the given short-answer Olympiad problem autonomously without human help.
No internet search, external references, hidden answer key, or solution hints are available.
Choose your own mathematical approach. Use the permitted operations when useful.
Before finalizing, obtain an independent review of your proposed solution: first save the
full proposed solution as a claim or artifact with write_draft, then request_review using
the actual revision_id returned in the next step. A review is advice, not an oracle.
Read the review, correct any demonstrated issues, and give your own final conclusion.
Do not repeatedly request the same review or invent IDs. Budget includes all descendants
and any format repair. A completed task is not an automatically adopted theorem.
Use LaTeX for all mathematical expressions. In the final research body give a concise
complete justification and exactly one final answer in \boxed{...}. For no solution use
\boxed{\varnothing}. If unresolved, explicitly say so and do not invent an answer.
Set next_action=finish only when delivering your final answer or unresolved conclusion;
research verdict must remain null, even if the independent review passed.
"""
TERMINAL = {
    "completed", "failed", "cancelled", "paused", "interrupted", "budget_exhausted",
    "step_limit", "reconciliation_required",
}


def stamp():
    return datetime.now(UTC).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_problems(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(value) != {"schema_version", "benchmark", "problems"}:
        raise ValueError("Problem-only file must not contain grading fields")
    cases = value["problems"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 400:
        raise ValueError("Expected between one and 400 problems")
    seen = set()
    for case in cases:
        if set(case) != {"id", "category", "problem"}:
            raise ValueError("Case must contain only id, category and problem")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", case["id"]) or case["id"] in seen:
            raise ValueError("Unsafe or duplicate case identifier")
        if not isinstance(case["problem"], str) or not 1 <= len(case["problem"]) <= 100000:
            raise ValueError("Invalid problem text")
        seen.add(case["id"])
    return value


def last_boxed(body):
    """Extract balanced LaTeX braces, never guess an answer from intermediate prose."""
    if not body:
        return None
    start = body.rfind(r"\boxed{")
    if start < 0:
        return None
    start += len(r"\boxed{")
    depth = 1
    for index in range(start, len(body)):
        if body[index] in "{}" and (index == 0 or body[index - 1] != "\\"):
            depth += 1 if body[index] == "{" else -1
        if depth == 0:
            return body[start:index].strip() or None
    return None


@dataclass(frozen=True)
class Limits:
    request_budget: int = 12
    max_steps: int = 6
    max_children: int = 3
    max_depth: int = 2
    max_review_rounds: int = 2
    max_output_tokens: int = 65536
    request_timeout_seconds: int = 600
    case_timeout_seconds: int = 1800
    parallel_cases: int = 2
    thinking_mode: str = "enabled"
    reasoning_effort: str = "max"

    def validate(self):
        if not 1 <= self.request_budget <= 100 or not 1 <= self.max_steps <= 40:
            raise ValueError("Invalid request/step budget")
        if self.parallel_cases not in {1, 2} or not 1 <= self.case_timeout_seconds <= 7200:
            raise ValueError("Invalid batch concurrency/deadline")
        if not 256 <= self.max_output_tokens <= 65536 or not 1 <= self.request_timeout_seconds <= 600:
            raise ValueError("Invalid provider budget/deadline")
        if self.thinking_mode != "enabled" or self.reasoning_effort not in {"high", "max"}:
            raise ValueError("This pilot requires an explicit high/max thinking setting")


class CompletionOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, endpoint, directory, inner=None):
        self.endpoint = endpoint
        self.directory = directory
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.number = 0

    async def handle_async_request(self, request):
        if request.method != "POST" or str(request.url) != self.endpoint:
            raise httpx.UnsupportedProtocol("Benchmark external endpoint is not permitted")
        self.number += 1
        payload = json.loads(request.content)
        if "tools" in payload or "functions" in payload:
            raise httpx.UnsupportedProtocol("Native or remote tools are not permitted")
        # Headers (including Authorization) are deliberately never serialized.
        write_json(self.directory / f"dispatch-{time.time_ns()}-{self.number}.json", {
            "created_at": stamp(), "endpoint": self.endpoint, "payload": payload,
            "payload_sha256": digest(request.content), "external_tools": False,
        })
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


@contextmanager
def batch_lock(directory):
    """OS lock is released on process death; stale text is never a liveness check."""
    lock = (directory / ".runner.lock").open("a+b")
    try:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        lock.close()


@asynccontextmanager
async def local_api(directory, config):
    with socket.socket() as socket_reservation:
        socket_reservation.bind(("127.0.0.1", 0))
        port = socket_reservation.getsockname()[1]
    human, worker = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MATHAGENT_")}
    prefix = "MATHAGENT_" + config.name.upper()
    environment.update({
        "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[2]), sysconfig.get_paths()["purelib"]]),
        "PYTHONNOUSERSITE": "1",
        "MATHAGENT_LOAD_ENV": "0", "MATHAGENT_ENABLE_REAL_API": "1",
        "MATHAGENT_DATABASE": str(directory / "research.sqlite3"),
        "MATHAGENT_TOKEN": human, "MATHAGENT_WORKER_TOKEN": worker,
        prefix + "_API_KEY": config.api_key, prefix + "_MODEL": config.model,
        prefix + "_BASE_URL": config.base_url,
    })
    with (directory / "api.log").open("ab") as log:
        process = await asyncio.create_subprocess_exec(
            getattr(sys, "_base_executable", sys.executable), "-X", "utf8", "-m", "uvicorn", "mathagent.api.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning", "--no-access-log",
            cwd=directory, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                         timeout=30, headers={"Authorization": "Bearer " + worker}) as client:
                for _ in range(200):
                    if process.returncode is not None:
                        raise RuntimeError("Isolated API exited at startup")
                    try:
                        response = await client.get("/health", timeout=0.5)
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Isolated API startup timeout")
                yield client, human
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def run_case(case, directory, config, limits, batch_id, *, api_factory=local_api,
                   transport_factory=None):
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.json"
    if report_path.exists():
        existing = json.loads(report_path.read_text(encoding="utf-8"))
        if existing.get("finished_at"):
            return existing
    journal_path = directory / "checkpoint.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {
        "started_at": stamp(), "deadline_epoch": time.time() + limits.case_timeout_seconds,
    }
    write_json(journal_path, journal)
    base = {"case_id": case["id"], "problem_id": case["id"], "category": case["category"],
            "problem_sha256": digest(case["problem"].encode()), "started_at": journal["started_at"],
            "interventions": [], "human_interventions": 0, "model_web_tools": False,
            "completed": False, "state": "starting", "final_answer": None, "final_body": None}
    write_json(report_path, base)
    print(json.dumps({"case": case["id"], "state": "starting"}), flush=True)
    async with api_factory(directory, config) as (client, human):
        headers = {"Authorization": "Bearer " + human}

        async def read(path):
            response = await client.get(path, headers=headers)
            if not response.is_success:
                raise RuntimeError(f"Benchmark read HTTP {response.status_code}")
            return response.json()

        async def write(path, payload=None, method="POST", label=None):
            key = str(uuid5(NAMESPACE_URL, f"{batch_id}/{case['id']}/{label or path}"))
            response = await client.request(method, path, json=payload,
                                            headers={**headers, "Idempotency-Key": key})
            if not response.is_success:
                raise RuntimeError(f"Benchmark command HTTP {response.status_code}")
            return response.json()

        project = await write("/projects", {"title": case["id"], "body": case["problem"]})
        project_id, branch_id = project["project_id"], project["branch_id"]
        await write(f"/projects/{project_id}/runtime-settings", {
            "request_budget": limits.request_budget, "allow_real_api": True,
            "allowed_providers": [config.name]}, "PUT")
        await write(f"/projects/{project_id}/agent-policy", {"allowed_operations": OPERATIONS}, "PUT")
        options = {k: v for k, v in asdict(limits).items() if k not in {"case_timeout_seconds", "parallel_cases"}}
        research = await write("/runs", {"branch_id": branch_id,
            "goal_object_id": project["object_id"], "provider": config.name,
            "autonomous": True, "instruction": INSTRUCTION, **options})
        run_id = research["run_id"]
        journal.update(project_id=project_id, branch_id=branch_id, run_id=run_id)
        write_json(journal_path, journal)
        timed_out = time.time() >= journal["deadline_epoch"]
        if timed_out:
            base["interventions"].append({"actor": "benchmark_scheduler", "kind": "deadline_cancel", "at": stamp()})
            await write(f"/branches/{branch_id}/interventions", {"action": "cancel"}, label="deadline")
        transport = CompletionOnlyTransport(DEFAULT_URLS[config.name] + "/chat/completions", directory,
            inner=transport_factory(case) if transport_factory else None)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as external:
            worker = HTTPWorker(client, providers=[config.name], concurrency=1,
                provider_factory=lambda _: RemoteProvider(config, external))
            work = asyncio.create_task(asyncio.sleep(0) if timed_out else worker.run(once=True))
            previous = None
            try:
                while not work.done():
                    budget = await read(f"/projects/{project_id}/budget")
                    status = (budget["occupied"], budget["spent"], budget["unknown"])
                    if status != previous:
                        print(json.dumps({"case": case["id"], "occupied": status[0],
                                          "spent": status[1], "unknown": status[2]}), flush=True)
                        previous = status
                    write_json(directory / "progress.json", {**base, "state": "running",
                        "updated_at": stamp(), "budget": budget})
                    if time.time() >= journal["deadline_epoch"]:
                        timed_out = True
                        base["interventions"].append({"actor": "benchmark_scheduler", "kind": "deadline_cancel", "at": stamp()})
                        await write(f"/branches/{branch_id}/interventions", {"action": "cancel"}, label="deadline")
                        work.cancel()
                        break
                    await asyncio.sleep(1)
                with contextlib.suppress(asyncio.CancelledError):
                    await work
            finally:
                if not work.done():
                    work.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await work
        snapshot = await read(f"/projects/{project_id}/snapshot")
        root = next(run for run in snapshot["runs"] if run["id"] == run_id)
        all_runs = {run["id"]: run for run in snapshot["runs"]}
        all_reviews = {review["id"]: review for review in snapshot["reviews"]}
        branches = (await read(f"/projects/{project_id}/branches"))["branches"]
        for branch in branches:
            if branch["id"] == branch_id:
                continue
            other = await read(f"/projects/{project_id}/snapshot?branch_id={branch['id']}")
            all_runs.update((run["id"], run) for run in other["runs"])
            all_reviews.update((review["id"], review) for review in other["reviews"])
        calls = []
        for run in all_runs.values():
            calls.extend((await read(f"/runs/{run['id']}/calls"))["calls"])
        steps = (await read(f"/runs/{run_id}/steps"))["steps"]
        final_body = steps[-1]["body"] if steps else None
        budget = await read(f"/projects/{project_id}/budget")
        reviews = [review for review in all_reviews.values() if review["kind"] == "llm_review"]
        answer = last_boxed(final_body) if root["state"] == "completed" and not timed_out else None
        report = {**base, "state": root["state"],
            "terminal_reason": "case_timeout" if timed_out else root["state"],
            "completed": root["state"] == "completed" and answer is not None and not timed_out,
            "final_answer": answer, "final_body": final_body, "requests": budget["occupied"],
            "budget": budget, "project_id": project_id, "root_run_id": run_id,
            "steps": steps, "calls": calls, "reviews": reviews, "runs": list(all_runs.values()),
            "independent_reviews": len(reviews),
            "network_dispatches": len(list(directory.glob("dispatch-*.json"))),
            "network_dispatches_this_process": transport.number,
            "evaluation_kind": "unattended_agent_short_answer", "scored": False}
        write_json(report_path, report)
        export = await write("/exports", {"project_id": project_id, "branch_id": branch_id})
        response = await client.get(f"/exports/{export['export_id']}/download", headers=headers)
        response.raise_for_status()
        (directory / "research-export.zip").write_bytes(response.content)
        report["finished_at"] = stamp()
        write_json(report_path, report)
        print(json.dumps({"case": case["id"], "state": report["state"],
                          "answer_present": answer is not None, "requests": report["requests"]}), flush=True)
        return report


def source_fingerprint(root):
    files = [*root.joinpath("services/api/src/mathagent").rglob("*.py"),
             root / "scripts/imo_answerbench.py", root / "pyproject.toml", root / "uv.lock"]
    entries = {str(path.relative_to(root)).replace("\\", "/"): digest(path.read_bytes()) for path in sorted(files)}
    return {"sha256": digest(json.dumps(entries, sort_keys=True).encode()), "files": entries}


async def run_batch(problems_file, directory, config, limits, root, *, resume=False,
                    api_factory=local_api, transport_factory=None):
    limits.validate()
    if config.name not in DEFAULT_URLS or config.base_url != DEFAULT_URLS[config.name] or not config.ready():
        raise ValueError("Benchmark requires a configured official inference endpoint")
    problems = load_problems(problems_file)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=resume)
    with batch_lock(directory):
        fingerprint = source_fingerprint(Path(root))
        stable = {"problems_sha256": digest(Path(problems_file).read_bytes()),
            "provider": config.name, "requested_model": config.model, "limits": asdict(limits),
            "source": fingerprint, "instruction_sha256": digest(INSTRUCTION.encode()),
            "case_ids": [case["id"] for case in problems["problems"]]}
        plan_file = directory / "plan.json"
        if plan_file.exists():
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
            if plan["configuration"] != stable:
                raise ValueError("Resume must use the exact frozen source, problems, model and limits")
        else:
            plan = {"batch_id": secrets.token_hex(16), "created_at": stamp(), "configuration": stable,
                "human_interventions_allowed": False, "model_web_tools": False,
                "answer_key_loaded": False, "max_requests_total": len(problems["problems"]) * limits.request_budget}
            write_json(plan_file, plan)
            write_json(directory / "problem-only.json", problems)
        semaphore = asyncio.Semaphore(limits.parallel_cases)

        async def bounded(case):
            async with semaphore:
                if source_fingerprint(Path(root)) != fingerprint:
                    raise RuntimeError("Source changed during a frozen benchmark")
                case_dir = directory / case["id"]
                try:
                    return await run_case(case, case_dir, config, limits, plan["batch_id"],
                        api_factory=api_factory, transport_factory=transport_factory)
                except Exception as error:
                    # Preserve partial journals/observations and continue the other cases.
                    case_dir.mkdir(parents=True, exist_ok=True)
                    partial_path = case_dir / "report.json"
                    partial = json.loads(partial_path.read_text(encoding="utf-8")) if partial_path.exists() else {}
                    failed = {**partial, "case_id": case["id"], "problem_id": case["id"], "state": "runner_error",
                        "previous_state": partial.get("state"), "completed": False,
                        "error_type": type(error).__name__, "observed_at": stamp(), "retry_requires_resume": True}
                    failed.pop("finished_at", None)
                    write_json(case_dir / "report.json", failed)
                    print(json.dumps({"case": case["id"], "state": "runner_error", "error_type": type(error).__name__}), flush=True)
                    return failed

        reports = await asyncio.gather(*(bounded(case) for case in problems["problems"]))
        summary = {"batch_id": plan["batch_id"], "finished_at": stamp(),
            "cases": [{k: report.get(k) for k in ("case_id", "state", "completed", "requests", "independent_reviews", "terminal_reason")} for report in reports],
            "case_reports": [f"{case['id']}/report.json" for case in problems["problems"]],
            "all_completed": all(report["completed"] for report in reports),
            "source_unchanged": source_fingerprint(Path(root)) == fingerprint,
            "answer_key_loaded": False, "scored": False}
        write_json(directory / "report.json", summary)
        return summary
