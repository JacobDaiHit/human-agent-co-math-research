"""Measure synthetic local API/SQLite latency; never call models or claim UI latency."""

import argparse
import json
import math
import os
import platform
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
HUMAN_TOKEN = "synthetic-benchmark-human"
WORKER_TOKEN = "synthetic-benchmark-worker"
AUTH = {"Authorization": f"Bearer {HUMAN_TOKEN}"}


def distribution(samples):
    ordered = sorted(samples)
    return {
        "sample_count": len(ordered),
        "minimum_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "mean_ms": round(statistics.mean(ordered), 3),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        "maximum_ms": round(ordered[-1], 3),
        "samples_ms": [round(sample, 3) for sample in samples],
    }


def post(client, path, payload, *, key=None):
    response = client.post(
        path, json=payload, headers={**AUTH, "Idempotency-Key": key or str(uuid4())}
    )
    if response.status_code != 201:
        raise RuntimeError(f"{path}: HTTP {response.status_code}: {response.text}")
    return response.json()


def get_snapshot(client, project_id, branch_id):
    response = client.get(
        f"/projects/{project_id}/snapshot", params={"branch_id": branch_id}, headers=AUTH
    )
    response.raise_for_status()
    return response.json(), len(response.content)


@contextmanager
def local_service(directory):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        **os.environ,
        "MATHAGENT_DATABASE": str(directory / "performance.sqlite3"),
        "MATHAGENT_TOKEN": HUMAN_TOKEN,
        "MATHAGENT_WORKER_TOKEN": WORKER_TOKEN,
        "MATHAGENT_ENABLE_REAL_API": "0",
        "MATHAGENT_LOAD_ENV": "0",
    }
    url = f"http://127.0.0.1:{port}"
    with (directory / "uvicorn.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mathagent.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=30) as client:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Temporary benchmark API exited during startup.")
                    try:
                        response = client.get("/health", timeout=0.5)
                        if response.status_code == 200:
                            if response.json()["real_models_enabled"]:
                                raise RuntimeError("Real model execution must be disabled.")
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Temporary benchmark API did not start within 20 seconds.")
                yield client, url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def seed(client):
    project = post(
        client,
        "/projects",
        {
            "title": "Synthetic SQLite scale benchmark",
            "body": "SYNTHETIC ENGINEERING FIXTURE: measure state latency; no mathematical claim.",
        },
    )
    branch = project["branch_id"]
    objects = [project]
    contexts, claims = [], []
    for kind, count, target in (("context", 20, contexts), ("claim", 179, claims)):
        for index in range(count):
            obj = post(
                client,
                "/objects",
                {
                    "branch_id": branch,
                    "kind": kind,
                    "body": f"SYNTHETIC {kind} {index}: " + "Unreviewed benchmark fixture. " * 12,
                    "payload": {
                        "synthetic": True,
                        **({"role": "definition"} if kind == "context" else {}),
                    },
                },
            )
            objects.append(obj)
            target.append(obj)
    for index in range(100):
        obj = post(
            client,
            "/proof-plans",
            {
                "branch_id": branch,
                "conclusion_revision_id": claims[index]["revision_id"],
                "premise_revision_ids": [
                    claims[(index + offset) % 179]["revision_id"] for offset in (1, 2, 3)
                ],
                "context_revision_ids": [contexts[index % 20]["revision_id"]],
                "body": f"SYNTHETIC proof plan {index}: "
                + "Draft only; not a mathematical proof. " * 15,
            },
        )
        objects.append(obj)
    for index in range(1000):
        source = index % 300
        offset = 1 + index // 300
        post(
            client,
            "/relations",
            {
                "branch_id": branch,
                "source_id": objects[source]["object_id"],
                "target_id": objects[(source + offset) % 300]["object_id"],
                "kind": "similar",
            },
        )
    return project, claims[:2]


def concurrent_mutations(url, branch_id, targets, sample_count):
    barrier = Barrier(2, timeout=45)

    def command_client(index):
        current = targets[index]
        samples, receipts = [], []
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=30) as client:
                client.get("/health").raise_for_status()  # Warm up the connection, outside timing.
                for iteration in range(sample_count // 2):
                    payload = {
                        "branch_id": branch_id,
                        "expected_revision_id": current["revision_id"],
                        "body": f"SYNTHETIC client {index} revision {iteration}: "
                        + "Changed benchmark lemma. " * 12,
                    }
                    key = str(uuid4())
                    barrier.wait()  # Both clients contend for SQLite's serialized writer.
                    start = time.perf_counter_ns()
                    current = post(
                        client, f"/objects/{current['object_id']}/revisions", payload, key=key
                    )
                    samples.append((time.perf_counter_ns() - start) / 1_000_000)
                    receipts.append({"key": key, "revision_id": current["revision_id"]})
            return {
                "client": index + 1,
                "latency": distribution(samples),
                "receipts": receipts,
                "final_head": current,
            }
        except BaseException:
            barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(command_client, range(2)))


def verify_database(path, clients, expected_revisions):
    with closing(sqlite3.connect(path)) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "objects",
                "revisions",
                "relations",
                "proof_plans",
                "dependencies",
                "manuscript_blocks",
                "runs",
                "attempts",
                "conflicts",
                "command_receipts",
                "events",
            )
        }
        for client in clients:
            final = client["final_head"]
            head = connection.execute(
                "SELECT revision_id FROM branch_heads WHERE object_id = ?", (final["object_id"],)
            ).fetchone()[0]
            if head != final["revision_id"]:
                raise RuntimeError("A benchmark mutation was lost from the final branch head.")
            for receipt in client["receipts"]:
                row = connection.execute(
                    "SELECT status_code, response FROM command_receipts WHERE key = ?",
                    (receipt["key"],),
                ).fetchone()
                if (
                    not row
                    or row[0] != 201
                    or json.loads(row[1])["revision_id"] != receipt["revision_id"]
                ):
                    raise RuntimeError(
                        "A successful HTTP command has no matching committed receipt."
                    )
        if (counts["objects"], counts["relations"], counts["revisions"], counts["conflicts"]) != (
            300,
            1000,
            expected_revisions,
            0,
        ):
            raise RuntimeError(f"Unexpected benchmark database counts: {counts}")
        return {
            "counts": counts,
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "all_measured_receipts_committed": True,
            "final_heads_match_receipts": True,
        }


def benchmark(mutation_samples=40, snapshot_samples=20):
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    with TemporaryDirectory(prefix="mathagent-performance-") as directory:
        path = Path(directory)
        with local_service(path) as (client, url):
            setup_start = time.perf_counter()
            project, targets = seed(client)
            setup_seconds = time.perf_counter() - setup_start
            for _ in range(3):
                get_snapshot(client, project["project_id"], project["branch_id"])
            clients = concurrent_mutations(url, project["branch_id"], targets, mutation_samples)
            snapshot_times, payload_sizes = [], []
            for _ in range(snapshot_samples):
                start = time.perf_counter_ns()
                snapshot, size = get_snapshot(client, project["project_id"], project["branch_id"])
                snapshot_times.append((time.perf_counter_ns() - start) / 1_000_000)
                payload_sizes.append(size)
                if len(snapshot["objects"]) != 300 or len(snapshot["relations"]) != 1000:
                    raise RuntimeError("Snapshot did not return the full benchmark dataset.")
            database = verify_database(
                path / "performance.sqlite3", clients, 300 + mutation_samples
            )
        command_stats = distribution(
            [sample for result in clients for sample in result["latency"]["samples_ms"]]
        )
        snapshot_stats = distribution(snapshot_times)
        return {
            "benchmark": "synthetic_local_sqlite_http_scale",
            "started_at_utc": started_at,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "environment": {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "logical_cpu_count": os.cpu_count(),
                "sqlite": sqlite3.sqlite_version,
                "packages": {
                    package: version(package)
                    for package in ("fastapi", "sqlalchemy", "uvicorn", "httpx")
                },
            },
            "method": {
                "transport": "Real loopback HTTP to a separate uvicorn process; persistent real SQLite file in local temporary directory.",
                "timing": "perf_counter_ns before HTTP request through full response body decode; excludes fixture setup and process startup.",
                "p95_method": "Nearest rank: sorted_samples[ceil(0.95 * n) - 1].",
                "command": "POST /objects/{id}/revisions with unique idempotency keys and current expected_revision_id.",
                "concurrency": "Two independent HTTP clients start each mutation round at a barrier, updating different lemma objects on one branch.",
                "snapshot": "GET /projects/{id}/snapshot on one client after mutations; 3 warmup snapshots excluded.",
                "dataset": "1 problem, 20 definition contexts, 179 claims, 100 draft proof plans, 400 declared dependencies, 1000 research relations; all synthetic.",
                "fixture_setup_seconds": round(setup_seconds, 3),
                "concurrent_command_clients": 2,
                "real_model_calls": 0,
                "model_workers_started": 0,
                "synthetic_fixtures": True,
            },
            "command_receipt_latency": command_stats,
            "command_clients": [
                {"client": result["client"], "latency": result["latency"]} for result in clients
            ],
            "snapshot_latency": {
                **snapshot_stats,
                "response_bytes_min": min(payload_sizes),
                "response_bytes_max": max(payload_sizes),
            },
            "budgets": {
                "command_receipt": {
                    "target_p95_ms_exclusive": 1000,
                    "measured_p95_ms": command_stats["p95_ms"],
                    "passed": command_stats["p95_ms"] < 1000,
                },
                "snapshot_diagnostic": {
                    "comparison_ms": 2000,
                    "measured_p95_ms": snapshot_stats["p95_ms"],
                    "under_comparison": snapshot_stats["p95_ms"] < 2000,
                    "ui_acceptance": False,
                },
                "submission_to_ui_update": {"target_ms": 2000, "measured": False, "passed": None},
            },
            "persistence_verification": database,
            "limitations": [
                "The 2-second plan target concerns submission-to-UI updates. This script does not measure SSE delivery, browser render, React Flow layout, or user interaction; snapshot latency alone cannot establish that target.",
                "Two concurrent command clients exercise SQLite writer contention; they are not two model-executing workers.",
                "Synthetic short draft bodies and unadopted proof plans do not represent large attachments, long histories, or worst-case adopted AND/OR proof analysis.",
                "This is one warm local run on the recorded machine under its current load, not a universal performance guarantee or cold-start measurement.",
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/acceptance/performance.json")
    parser.add_argument("--mutations", type=int, default=40)
    parser.add_argument("--snapshots", type=int, default=20)
    options = parser.parse_args()
    if options.mutations < 20 or options.mutations % 2 or options.snapshots < 10:
        parser.error("Use an even mutation sample count >=20 and a snapshot count >=10.")
    report = benchmark(options.mutations, options.snapshots)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(options.output.resolve()),
                "budgets": report["budgets"],
                "counts": report["persistence_verification"]["counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
