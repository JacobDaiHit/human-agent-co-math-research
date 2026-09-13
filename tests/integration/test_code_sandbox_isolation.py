"""Opt-in real Docker acceptance. Mock tests cannot satisfy this isolation gate."""

import os
import time

import pytest
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
