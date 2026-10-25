from __future__ import annotations

import contextlib
import subprocess
import sys

import pytest

from agent_tools import bootstrap_process
from tests._support.clock import FakeClock
from tests._support.paths import REPO_ROOT


class Child:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 123
        self.waits = []
        self.codes = [0]
        self.terminated = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def wait(self, timeout=None):
        self.waits.append(timeout)
        outcome = self.codes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


@pytest.mark.parametrize(
    "failure",
    ["ReadTimeoutError: body interrupted", "connection reset by peer", "503 Server Error"],
)
def test_retries_stream_failures_and_records_full_log(tmp_path, monkeypatch, failure):
    clock = FakeClock()
    monkeypatch.setattr(bootstrap_process.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(bootstrap_process.time, "sleep", clock.sleep)
    children = []

    def spawn(args, **kwargs):
        child = Child(args, **kwargs)
        children.append(child)
        child.codes = [1 if len(children) == 1 else 0]
        kwargs["stdout"].write((failure if len(children) == 1 else "installed").encode())
        kwargs["stdout"].flush()
        return child

    monkeypatch.setattr(bootstrap_process.subprocess, "Popen", spawn)
    result = bootstrap_process.run(
        tmp_path, {"display": "download", "args": ["pip"], "network": True}
    )
    assert result["ok"] is True
    assert len(children) == 2
    log = tmp_path / result["log_ref"]
    assert failure in log.read_text()
    assert "installed" in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600
    assert children[0].kwargs["env"]["PIP_DEFAULT_TIMEOUT"] == "120"
    assert children[0].kwargs["env"]["PIP_RETRIES"] == "5"
    assert children[0].kwargs["env"]["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
    assert clock.monotonic() == 2


@pytest.mark.parametrize(
    "failure",
    [
        "ResolutionImpossible: conflicting dependencies",
        "No matching distribution",
        "CERTIFICATE_VERIFY_FAILED",
        "403 Client Error",
    ],
)
def test_permanent_failures_are_not_retried(tmp_path, monkeypatch, failure):
    children = []

    def spawn(args, **kwargs):
        child = Child(args, **kwargs)
        child.codes = [1]
        children.append(child)
        kwargs["stdout"].write(failure.encode())
        kwargs["stdout"].flush()
        return child

    monkeypatch.setattr(bootstrap_process.subprocess, "Popen", spawn)
    result = bootstrap_process.run(
        tmp_path, {"display": "download", "args": ["pip"], "network": True}
    )
    assert result["ok"] is False
    assert len(children) == 1
    assert failure in result["stderr"]


def test_retry_budget_is_three_and_user_timeout_is_preserved(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(bootstrap_process.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(bootstrap_process.time, "sleep", clock.sleep)
    monkeypatch.setenv("PIP_DEFAULT_TIMEOUT", "240")
    monkeypatch.setenv("PIP_RETRIES", "9")
    environments = []

    def spawn(args, **kwargs):
        child = Child(args, **kwargs)
        child.codes = [1]
        environments.append(kwargs["env"])
        kwargs["stdout"].write(b"ReadTimeoutError")
        kwargs["stdout"].flush()
        return child

    monkeypatch.setattr(bootstrap_process.subprocess, "Popen", spawn)
    result = bootstrap_process.run(
        tmp_path, {"display": "download", "args": ["pip"], "network": True}
    )
    assert result["ok"] is False
    assert len(environments) == 3
    assert clock.monotonic() == 7
    assert environments[0]["PIP_DEFAULT_TIMEOUT"] == "240"
    assert environments[0]["PIP_RETRIES"] == "9"


@pytest.mark.parametrize("interrupt", [False, True])
def test_timeout_or_interrupt_terminates_process(tmp_path, monkeypatch, interrupt):
    child = Child(["pip"])
    child.codes = [KeyboardInterrupt() if interrupt else subprocess.TimeoutExpired("pip", 900), 0]
    monkeypatch.setattr(bootstrap_process.subprocess, "Popen", lambda *args, **kwargs: child)
    signals = []
    monkeypatch.setattr(
        bootstrap_process.os, "killpg", lambda pid, signal: signals.append((pid, signal))
    )
    times = iter([0, 0, 900, 901])
    monkeypatch.setattr(bootstrap_process.time, "monotonic", lambda: next(times))
    command = {"display": "download", "args": ["pip"], "network": True}
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            bootstrap_process.run(tmp_path, command)
    else:
        assert bootstrap_process.run(tmp_path, command)["ok"] is False
    assert signals


def test_os_failure_is_a_failed_receipt(tmp_path, monkeypatch):
    def spawn(*args, **kwargs):
        msg = "missing python"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(bootstrap_process.subprocess, "Popen", spawn)
    result = bootstrap_process.run(tmp_path, {"display": "venv", "args": ["missing"]})
    assert result["ok"] is False
    assert "missing python" in result["stderr"]


def test_real_subprocess_environment_and_log_are_private(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ATK_BOOTSTRAP_PROGRESS", "1")
    result = bootstrap_process.run(
        tmp_path,
        {
            "display": "probe",
            "args": [
                sys.executable,
                "-c",
                'import os; print("private output"); print(os.environ["PROBE_VALUE"])',
            ],
            "env": {"PROBE_VALUE": "expected"},
        },
    )
    assert result["ok"] is True
    assert "private output" not in capsys.readouterr().out
    assert "expected" in (tmp_path / result["log_ref"]).read_text()


def test_locks_reject_concurrent_resume_and_release_after_process_exit(tmp_path):
    location = tmp_path / "session.lock"
    with contextlib.ExitStack() as stack:
        stack.enter_context(bootstrap_process.lock(location))
        with pytest.raises(BlockingIOError):
            stack.enter_context(bootstrap_process.lock(location, wait=False))
    code = (
        "from pathlib import Path; import os; from agent_tools.bootstrap_process import lock; "
        f"\nwith lock(Path({str(location)!r})): os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, check=True)
    with bootstrap_process.lock(location, wait=False):
        assert location.is_file()
