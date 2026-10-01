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
import random
import re
import secrets
import socket
import subprocess
import sys
import sysconfig
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
from mathagent.evaluation.outcomes import (
    RESEARCH_SUBMISSION_RULE,
    SUBMISSION_RULE,
    agent_outcomes,
    usage_summary,
)
from mathagent.providers import research
from mathagent.providers.remote import DEFAULT_URLS, RemoteProvider
from mathagent.runtime.completion import boxed_answers
from mathagent.runtime.worker import HTTPWorker

OPERATIONS = [
    "read_object", "search_project", "write_draft", "revise_object", "propose_proof",
    "record_failure", "record_source", "create_branch", "spawn_task", "request_review",
    "discuss", "calculate",
]
TERMINAL = {
    "completed", "failed", "cancelled", "paused", "interrupted", "budget_exhausted",
    "step_limit", "reconciliation_required",
}

def instruction_for(limits):
    if limits.solver != "agent":
        from mathagent.evaluation.direct import DIRECT_INSTRUCTION
        return DIRECT_INSTRUCTION
    if limits.solver_controller == "continuous_research":
        return r"""Solve the original mathematical problem closed-book, without human help,
internet access, hidden answers or solution hints. Work on the actual local task.
Use calculations, saved worknotes, new local tasks and a research peer when useful.
Keep the original assumptions and quantifiers. Present mathematical reasoning as
ordinary text with LaTeX. The peer is a researcher, not an approving authority.
For the peer's first task provide the original problem and a neutral research goal,
not your candidate answer. Exchange actual arguments, failures and open questions
after that independent attempt; do not seek votes or agreement.
All members and tools share the problem's request and output budgets.
When ready, explicitly submit your selected solution body and its short answer.
For tasks asking for all solutions, explain both membership and exhaustiveness.
If unresolved, save the remaining obstacle and submit an unresolved outcome.
A submitted solution is not program-verified mathematical truth.
"""
    raise ValueError("Retired solvers cannot dispatch new research")


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
    completion_policy: str = "draft"
    length_recovery: str = "none"
    unknown_recovery: str = "stop"
    code_sandbox: bool = False
    evaluation_mode: str = "research"
    solver: str = "agent"
    solver_controller: str = "continuous_research"
    discussion: bool = True
    research_deadline_seconds: int = 1800
    search_config: dict = field(default_factory=dict)
    cumulative_output_token_budget: int | None = None
    builtin_calculator: bool = False
    case_order_seed: int = 0

    def validate(self):
        if not 1 <= self.request_budget <= 100:
            raise ValueError("Invalid request budget")
        if self.parallel_cases not in {1, 2} or not 1 <= self.case_timeout_seconds <= 7200:
            raise ValueError("Invalid batch concurrency/deadline")
        if not 256 <= self.max_output_tokens <= 65536 or not 1 <= self.request_timeout_seconds <= 600:
            raise ValueError("Invalid provider budget/deadline")
        if self.thinking_mode != "enabled" or self.reasoning_effort not in {"high", "max"}:
            raise ValueError("This pilot requires an explicit high/max thinking setting")
        if self.evaluation_mode not in {"answer", "research"}:
            raise ValueError("Invalid evaluation mode")
        if self.solver not in {"agent", "direct", "self_refine", "independent_samples"}:
            raise ValueError("Invalid solver")
        if self.solver == "agent" and self.solver_controller != "continuous_research":
            raise ValueError("Retired solvers are read-only; new runs use continuous_research")
        if self.search_config:
            raise ValueError("Search configuration belongs to retired historical workflows")
        if self.solver != "agent" and (self.evaluation_mode != "answer" or self.code_sandbox):
            raise ValueError("Plain baselines require answer mode without tools")
        if self.solver == "direct" and self.request_budget != 1:
            raise ValueError("Direct baseline is exactly one request; use self_refine for multiple calls")
        if self.length_recovery not in {"none", "high"}:
            raise ValueError("Invalid output limit recovery policy")
        if self.unknown_recovery not in {"stop", "once"}:
            raise ValueError("Invalid unknown outcome recovery policy")
        if type(self.code_sandbox) is not bool:
            raise ValueError("Invalid sandbox policy")
        if type(self.discussion) is not bool or not 1 <= self.research_deadline_seconds <= 86400:
            raise ValueError("Invalid discussion/deadline configuration")
        if type(self.builtin_calculator) is not bool:
            raise ValueError("Invalid calculator policy")
        if type(self.case_order_seed) is not int:
            raise ValueError("Invalid case ordering seed")
        if self.cumulative_output_token_budget is not None and (
                type(self.cumulative_output_token_budget) is not int or
                not 256 <= self.cumulative_output_token_budget <= 6_553_600):
            raise ValueError("Invalid cumulative output token budget")


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
        if "functions" in payload or any(
            tool.get("type") != "function" or tool.get("function", {}).get("name") not in research.ARGUMENT_MODELS
            for tool in payload.get("tools", [])
        ):
            raise httpx.UnsupportedProtocol("Only locally executed research tools are permitted")
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
    # Only the operator-selected image crosses into the isolated API; project
    # opt-in and the frozen-image check still happen before inference dispatch.
    if os.environ.get("MATHAGENT_SANDBOX_IMAGE"):
        environment["MATHAGENT_SANDBOX_IMAGE"] = os.environ["MATHAGENT_SANDBOX_IMAGE"]
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
                   transport_factory=None, sandbox_image=None):
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
            "interventions": [], "human_interventions": None, "unattended_eligible": None, "model_web_tools": False,
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

        async def cancel_at_deadline():
            receipt = await write(f"/branches/{branch_id}/interventions", {"action": "cancel"}, label="deadline")
            journal["scheduler_cancel_receipt"] = receipt
            write_json(journal_path, journal)

        project = await write("/projects", {"title": case["id"], "body": case["problem"]})
        project_id, branch_id = project["project_id"], project["branch_id"]
        await write(f"/projects/{project_id}/runtime-settings", {
            "request_budget": limits.request_budget, "allow_real_api": True,
            "allowed_providers": [config.name]}, "PUT")
        operations = [name for name in OPERATIONS if limits.code_sandbox or name != "calculate"]
        await write(f"/projects/{project_id}/agent-policy", {"allowed_operations": operations}, "PUT")
        if limits.code_sandbox:
            sandbox = await write(f"/projects/{project_id}/code-sandbox", {"enabled": True}, "PUT")
            if not sandbox.get("ready") or sandbox.get("image_id") != sandbox_image:
                raise RuntimeError("Sandbox differs from frozen preflight; no inference dispatched")
        options = {k: v for k, v in asdict(limits).items() if k in {
            "request_budget", "max_output_tokens", "request_timeout_seconds",
            "thinking_mode", "reasoning_effort", "unknown_recovery",
            "discussion", "research_deadline_seconds", "cumulative_output_token_budget"}}
        options["solver_controller"] = limits.solver_controller
        research = await write("/runs", {"branch_id": branch_id,
            "goal_object_id": project["object_id"], "provider": config.name,
            "autonomous": True, "instruction": instruction_for(limits), **options})
        run_id = research["run_id"]
        journal.update(project_id=project_id, branch_id=branch_id, run_id=run_id)
        write_json(journal_path, journal)
        timed_out = time.time() >= journal["deadline_epoch"]
        if timed_out:
            await cancel_at_deadline()
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
                        await cancel_at_deadline()
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
        search_state = await read(f"/runs/{run_id}/search") if limits.solver_controller == "bounded_search_v1" else None
        research_state = await read(f"/runs/{run_id}/research") if limits.solver_controller == "continuous_research" else None
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
        root_budget = await read(f"/runs/{run_id}/budget")
        reviews = [review for review in all_reviews.values() if review["kind"] == "llm_review"]
        answers = boxed_answers(final_body)
        answer = answers[0] if len(answers) == 1 and root["state"] == "completed" and not timed_out else None
        final_citations = set(next((call.get("result", {}).get("cited_revision_ids", [])
            for call in calls if steps and call["request_id"] == steps[-1]["request_id"] and call.get("result")), []))
        reviewed_targets = {review["target_revision_id"] for review in reviews if review["verdict"] == "passed"}
        review_completed = bool(reviewed_targets & final_citations)
        completion_checks = steps[-1]["receipt"].get("completion_checks", {}) if steps else {}
        finalized_after_review = completion_checks.get("policy") == "reviewed_answer" and completion_checks.get("passed") is True
        workflow_completed = root["state"] == "completed" and answer is not None and review_completed and finalized_after_review and not timed_out
        if research_state is not None:
            final_body = (research_state.get("solution") or {}).get("body")
            workflow_completed = root["state"] == "completed" and research_state["session"]["outcome"] == "solved" and not timed_out
        outcomes = agent_outcomes(steps, calls, reviews, workflow_completed, research_state)
        answer = outcomes["answer_submission"]["answer"]
        mode_completed = workflow_completed if limits.evaluation_mode == "research" else (
            answer is not None and root["state"] == "completed" and not timed_out)
        events, cursor = [], 0
        while True:
            page = await read(f"/projects/{project_id}/events?after_seq={cursor}")
            events.extend(page["events"])
            cursor = page["last_seq"]
            if cursor >= page["project_seq"] or not page["events"]:
                break
        audit = intervention_audit(events, journal.get("scheduler_cancel_receipt"))
        report = {**base, **audit, "state": root["state"],
            "terminal_reason": "case_timeout" if timed_out else "unresolved" if research_state and research_state["session"]["outcome"] == "unresolved" else "missing_final_answer" if root["state"] == "completed" and answer is None else "review_missing" if not research_state and limits.evaluation_mode == "research" and root["state"] == "completed" and not review_completed else root["state"],
            "runtime_terminal": root["state"] in TERMINAL, "runtime_completed": root["state"] == "completed",
            "final_answer_present": answer is not None, "review_completed": review_completed,
            "finalized_after_review": finalized_after_review, "completion_checks": completion_checks,
            "reviewed_candidate_revision_ids": sorted(reviewed_targets & final_citations),
            "workflow_completed": workflow_completed, "completed": mode_completed,
            "report_schema_version": "2.0", "evaluation_mode": limits.evaluation_mode,
            "solver": limits.solver, **outcomes, "usage_summary": usage_summary(calls),
            "solver_controller": limits.solver_controller,
            "elapsed_seconds": (datetime.now(UTC) - datetime.fromisoformat(journal["started_at"])).total_seconds(),
            "final_answer": answer, "final_body": final_body, "requests": budget["occupied"],
            "budget": budget, "project_id": project_id, "root_run_id": run_id,
            "output_token_budget": root_budget.get("output_token_budget"),
            "financial_reconciliation_pending": budget["unknown"] > 0,
            "unknown_retries_authorized": sum(event["type"] == "request.unknown_retry_authorized" for event in events),
            "steps": steps, "calls": calls, "reviews": reviews, "runs": list(all_runs.values()),
            "independent_reviews": len(reviews),
            "network_dispatches": len(list(directory.glob("dispatch-*.json"))),
            "network_dispatches_this_process": transport.number,
            "evaluation_kind": "unattended_agent_short_answer" if audit["unattended_eligible"] else "intervened_agent_short_answer", "scored": False}
        if search_state is not None:
            report["search_state"] = search_state
        if research_state is not None:
            report["research_state"] = research_state
            report["discussion_enabled"] = limits.discussion
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
    files.extend(path for path in (root / "sandbox/runner.py", root / "sandbox/Dockerfile") if path.is_file())
    entries = {str(path.relative_to(root)).replace("\\", "/"): digest(path.read_bytes()) for path in sorted(files)}
    return {"sha256": digest(json.dumps(entries, sort_keys=True).encode()), "files": entries}


def intervention_audit(events, scheduler_receipt=None):
    """Read durable controls; a scheduler receipt exempts only its exact events."""
    scheduler_receipt = scheduler_receipt or {}
    scheduler_ids = {row["intervention_id"] for row in scheduler_receipt.get("affected_runs", [])
                     if row.get("intervention_id")}
    controls = []
    for event in events:
        kind, payload = event["type"], event["payload"]
        if kind == "run.intervention":
            scheduler = event["id"] in scheduler_ids
        elif kind == "branch.intervention" and not payload.get("affected_runs"):
            scheduler = bool(scheduler_receipt) and payload == scheduler_receipt
        elif kind in {"run.resumed", "run.options_changed"}:
            scheduler = False
        elif kind == "search.decision" and payload.get("action") == "human_intervention":
            scheduler = False
        else:
            continue
        controls.append({"event_id": event["id"], "kind": kind,
            "actor": "benchmark_scheduler" if scheduler else "human",
            "payload": payload, "created_at": event.get("created_at")})
    human_count = sum(control["actor"] == "human" for control in controls)
    return {"interventions": controls, "human_interventions": human_count,
            "unattended_eligible": human_count == 0}


async def run_batch(problems_file, directory, config, limits, root, *, resume=False,
                    api_factory=local_api, transport_factory=None, case_ids=None):
    limits.validate()
    if resume and limits.solver != "agent":
        raise ValueError("Plain baseline resume is not supported; reconcile durable calls before a new preregistered run")
    if config.name not in DEFAULT_URLS or config.base_url != DEFAULT_URLS[config.name] or not config.ready():
        raise ValueError("Benchmark requires a configured official inference endpoint")
    problems = load_problems(problems_file)
    if case_ids is not None:
        known = {case["id"] for case in problems["problems"]}
        if not case_ids or len(set(case_ids)) != len(case_ids) or not set(case_ids) <= known:
            raise ValueError("Select nonempty, unique, known case IDs")
        problems = {**problems, "problems": [case for case in problems["problems"] if case["id"] in case_ids]}
    problems = {**problems, "problems": list(problems["problems"])}
    random.Random(limits.case_order_seed).shuffle(problems["problems"])
    directory = Path(directory).resolve()
    sandbox_configuration = {"enabled": False}
    if limits.code_sandbox:
        from mathagent.tools.code_sandbox import TOOL_VERSION, CodeSandbox
        sandbox = CodeSandbox(directory / "sandbox-preflight.sqlite3").status()
        if not sandbox["ready"]:
            raise ValueError("Sandbox unavailable before benchmark: " + sandbox["reason"])
        sandbox_configuration = {"enabled": True, "image_id": sandbox["image_id"],
                                 "network": "none", "tool_version": TOOL_VERSION}
    directory.mkdir(parents=True, exist_ok=resume)
    with batch_lock(directory):
        fingerprint = source_fingerprint(Path(root))
        stable = {"problems_sha256": digest(Path(problems_file).read_bytes()),
            "provider": config.name, "requested_model": config.model, "limits": asdict(limits),
            "source": fingerprint, "instruction_sha256": digest(instruction_for(limits).encode()),
            "submission_rule": RESEARCH_SUBMISSION_RULE if limits.solver == "agent" and limits.solver_controller == "continuous_research" else SUBMISSION_RULE,
            "selection_rule": ("independent-single-box-whitespace-vote-earliest-v1"
                               if limits.solver == "independent_samples" else RESEARCH_SUBMISSION_RULE
                               if limits.solver == "agent" and limits.solver_controller == "continuous_research" else SUBMISSION_RULE),
            "code_sandbox": sandbox_configuration,
            "scope": "targeted_retest" if case_ids is not None else "full_fixture",
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
                    if limits.solver != "agent":
                        from mathagent.evaluation.direct import run_direct_case
                        return await run_direct_case(case, case_dir, config, limits,
                                                     transport_factory=transport_factory)
                    return await run_case(case, case_dir, config, limits, plan["batch_id"],
                        api_factory=api_factory, transport_factory=transport_factory,
                        sandbox_image=sandbox_configuration.get("image_id"))
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
            "scope": stable["scope"],
            "cases": [{k: report.get(k) for k in ("case_id", "state", "completed", "requests", "independent_reviews", "terminal_reason", "human_interventions", "unattended_eligible", "financial_reconciliation_pending", "unknown_retries_authorized")} for report in reports],
            "case_reports": [f"{case['id']}/report.json" for case in problems["problems"]],
            "all_completed": all(report["completed"] for report in reports),
            "all_unattended": all(report.get("unattended_eligible") is True for report in reports),
            "source_unchanged": source_fingerprint(Path(root)) == fingerprint,
            "answer_key_loaded": False, "scored": False}
        write_json(directory / "report.json", summary)
        return summary
