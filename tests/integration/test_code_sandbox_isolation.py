"""Opt-in real Docker acceptance. Mock tests cannot satisfy this isolation gate."""

import asyncio
import os
import time
from uuid import uuid4

import pytest
from mathagent.evaluation.answerbench import local_api
from mathagent.providers.remote import ProviderConfig
from mathagent.tools.code_sandbox import CodeSandbox

pytestmark = pytest.mark.skipif(
    os.getenv("MATHAGENT_TEST_CODE_SANDBOX") != "1",
    reason="Real offline Docker isolation requires an available engine and explicitly pinned image",
)


@pytest.fixture
def sandbox(tmp_path):
    value = CodeSandbox(tmp_path / "isolation.sqlite3")
    assert value.status()["ready"], "Sandbox acceptance must not silently fall back to host Python"
    return value


def test_container_has_no_host_files_credentials_or_external_network(sandbox):
    code = r'''
import os, socket
assert os.getuid() == 65534 and os.getgroups() == []
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/host')
assert not any('API_KEY' in name for name in os.environ)
try:
    os.kill(1, 19)
except PermissionError:
    pass
else:
    raise AssertionError('untrusted child can control supervisor')
sock = socket.socket()
sock.settimeout(1)
try:
    sock.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise AssertionError('external network reachable')
try:
    open('/opt/mathagent/runner.py', 'w')
except OSError:
    pass
else:
    raise AssertionError('trusted supervisor writable')
print('ISOLATION_OK')
'''
    result = sandbox.execute("isolation", "boundaries", code, 5)
    assert result["ok"] and result["stdout"].strip() == "ISOLATION_OK", result


def test_escaped_process_and_closed_pipes_cannot_defeat_timeout(sandbox):
    code = "import os, time\nif os.fork() == 0:\n os.setsid()\n while True: time.sleep(1)\nos._exit(0)"
    started = time.monotonic()
    result = sandbox.execute("isolation", "escaped-process", code, 1)
    assert not result["ok"] and result["reason"] == "timeout", result
    assert time.monotonic() - started < 15
    result = sandbox.execute("isolation", "closed-pipes", "import os\nos.close(1)\nos.close(2)\nwhile True: pass", 1)
    assert not result["ok"]


def test_output_is_bounded_and_program_error_is_not_success(sandbox):
    flood = sandbox.execute("isolation", "output-flood", "while True: print('x' * 8192)", 2)
    assert not flood["ok"] and flood["reason"] == "output_limit_exceeded", flood
    assert len(flood["stdout"].encode()) <= 32768
    failed = sandbox.execute("isolation", "program-error", "raise ValueError('synthetic failure')", 2)
    assert not failed["ok"] and failed["reason"] == "program_error", failed


def test_general_symbolic_mathematics_is_preinstalled(sandbox):
    source = """from sympy import Matrix, Rational, symbols, factor, integrate
x = symbols('x')
assert Rational(1, 3) + Rational(2, 5) == Rational(11, 15)
assert factor(x**2 - 1) == (x - 1)*(x + 1)
assert integrate(3*x**2, x) == x**3
assert Matrix([[1, 2], [3, 4]]).det() == -2
print('SYMPY_OK')
"""
    result = sandbox.execute("isolation", "symbolic-math", source, 5)
    assert result["ok"] and result["stdout"].strip() == "SYMPY_OK", result


def test_background_symbolic_work_exceeds_old_ten_second_limit_and_survives_restart(sandbox):
    source = "import time\ntime.sleep(11)\nfrom sympy import symbols, factor\nx=symbols('x')\nprint(factor(x*x-1))\nprint('x'*100000)"
    job = sandbox.start("isolation", "long-symbolic-work", source, 30, research_root_id="research-a")
    assert job["status"] == "running", job
    restarted = CodeSandbox(sandbox.database_path, sandbox.image_id)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        result = restarted.poll("isolation", job["job_id"])
        if result.get("status") != "running":
            break
        time.sleep(0.2)
    assert result.get("status") == "complete" and result["ok"], result
    assert "(x - 1)*(x + 1)" in result["stdout"] and len(result["stdout"]) > 100000
    assert restarted.poll("isolation", job["job_id"])["cached"]


def test_human_stop_cancels_only_this_researchs_background_computations(sandbox):
    first = sandbox.start("isolation", "cancel-long-work", "while True: pass", 3600, research_root_id="research-a")
    other = sandbox.start("isolation", "keep-other-work", "import time\ntime.sleep(15)", 30, research_root_id="research-b")
    assert first.get("status") == other.get("status") == "running"
    stopped = sandbox.cancel_research("isolation", "research-a")
    assert len(stopped) == 1 and stopped[0]["reason"] == "cancelled"
    assert sandbox.poll("isolation", other["job_id"])["status"] == "running"
    sandbox.cancel("isolation", other["job_id"])


def test_benchmark_api_receives_pinned_image_without_model_dispatch(tmp_path):
    asyncio.run(_check_benchmark_api(tmp_path))


async def _check_benchmark_api(tmp_path):
    config = ProviderConfig("deepseek", "synthetic-unused", "deepseek-v4-flash", "https://api.deepseek.com", True)
    async with local_api(tmp_path, config) as (client, human):
        headers = {"Authorization": "Bearer " + human, "Idempotency-Key": str(uuid4())}
        created = await client.post("/projects", json={"title": "Isolation preflight", "body": "Synthetic fixture"}, headers=headers)
        assert created.is_success, created.text
        project_id = created.json()["project_id"]
        headers["Idempotency-Key"] = str(uuid4())
        enabled = await client.put(f"/projects/{project_id}/code-sandbox", json={"enabled": True}, headers=headers)
        assert enabled.is_success, enabled.text
        assert enabled.json()["ready"] is True
        assert enabled.json()["image_id"] == os.environ["MATHAGENT_SANDBOX_IMAGE"]
        budget = await client.get(f"/projects/{project_id}/budget", headers=headers)
        assert budget.json()["occupied"] == 0
