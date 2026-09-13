"""Trusted, root-owned container supervisor. It is not untrusted solver code."""

import json
import os
import resource
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_OUTPUT = 32 * 1024
MAX_CODE = 64 * 1024


def emit(**value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def main():
    code = sys.stdin.buffer.read(MAX_CODE + 1)
    if len(code) > MAX_CODE:
        emit(reason="code_too_large")
        return
    try:
        timeout = max(1, min(10, int(os.environ.get("MATHAGENT_TIMEOUT_SECONDS", "5"))))
    except ValueError:
        timeout = 5
    program = Path("/work/program.py")
    program.write_bytes(code)
    # The untrusted uid needs read access to execute it, but cannot modify the
    # root-owned source after the supervisor has written it.
    os.chmod(program, 0o644)
    command = [sys.executable, "-I", "-S", str(program)]
    child = subprocess.Popen(
        command, cwd="/work", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
        preexec_fn=_drop_privileges,
    )
    stdout, stderr, overflow, timed_out = _collect(child, timeout)
    if timed_out:
        emit(reason="timeout", stdout=stdout, stderr=stderr)
    elif overflow:
        emit(reason="output_limit_exceeded", stdout=stdout, stderr=stderr)
    else:
        emit(reason=None if child.returncode == 0 else "program_error",
             exit_code=child.returncode, stdout=stdout, stderr=stderr)


def _drop_privileges():
    timeout = max(1, min(10, int(os.environ.get("MATHAGENT_TIMEOUT_SECONDS", "5"))))
    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)


def _collect(child, timeout):
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    for name, pipe in (("stdout", child.stdout), ("stderr", child.stderr)):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    overflow = timed_out = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _kill_group(child)
            _close_pipes(selector)
            break
        for key, _ in selector.select(remaining):
            chunk = os.read(key.fileobj.fileno(), 8192)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            buffer = buffers[key.data]
            room = MAX_OUTPUT - len(buffer)
            buffer.extend(chunk[:room])
            if len(chunk) > room:
                overflow = True
                _kill_group(child)
                _close_pipes(selector)
                break
        if overflow:
            break
    try:
        child.wait(timeout=0.2 if overflow or timed_out else max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(child)
        try:
            child.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
    selector.close()
    stdout = bytes(buffers["stdout"]).decode("utf-8", "replace")
    stderr = bytes(buffers["stderr"]).decode("utf-8", "replace")
    return stdout, stderr, overflow, timed_out


def _kill_group(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _close_pipes(selector):
    for key in list(selector.get_map().values()):
        selector.unregister(key.fileobj)
        key.fileobj.close()


if __name__ == "__main__":
    main()
