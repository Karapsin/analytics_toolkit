"""Contained execution failures cannot become trusted host execution or visual approval."""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.repository.github_agent import controller

isolation = controller.trusted_module("capture_isolation")
sandbox_module = controller.trusted_module("capture_sandbox")
artifact = controller.trusted_module("capture_artifact")
IDENTITY = {"candidate": "a" * 40, "repository": "owner/repo", "run_id": 123, "run_attempt": 2}


def test_probe_dispatch_cannot_start_ordinary_host_candidate_jobs() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/tests.yml").read_text()
    jobs = workflow.split("\njobs:\n", 1)[1]
    blocks = {}
    for line in jobs.splitlines(keepends=True):
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            name = line.strip()[:-1]
            blocks[name] = ""
        blocks[name] += line
    assert set(blocks) == {
        "source-validation",
        "visual-isolation-probe",
        "fast-checks",
        "tests",
        "sdk-conformance",
        "quality",
    }
    guard = "!(github.event_name == 'workflow_dispatch' && inputs.visual_isolation_probe)"
    for name, body in blocks.items():
        if name == "visual-isolation-probe":
            assert (
                "if: github.event_name == 'workflow_dispatch' && inputs.visual_isolation_probe"
                in body
            )
            assert "persist-credentials: false" in body
            assert (
                'run: python -I "$GITHUB_WORKSPACE/.github/agent/capture_isolation_probe.py"'
                in body
            )
            assert "quarantined-logs" not in body
            assert "path: ${{ steps.probe.outputs.artifact_root }}/" not in body
            assert "${{ steps.probe.outputs.artifact_root }}/isolation-diagnostics/" in body
            assert "${{ steps.probe.outputs.artifact_root }}/probe-evidence/" in body
        elif name != "source-validation":
            assert guard in body
            assert name != "fast-checks" or "if: always() && " + guard in body
            # Skipped probe jobs must not supersede ordinary exact-head checks.
            check_name = next(line for line in body.splitlines() if line.startswith("    name:"))
            assert "inputs.visual_isolation_probe && 'Probe-only " in check_name
            if name == "fast-checks":
                assert "|| 'fast-checks' }}" in check_name


def write_fixture_output(state: Any, tmp_path: Path, phase: str) -> None:
    if phase == "capture":
        folder = state.runtime_candidate / "visual-artifact"
        folder.mkdir()
        (folder / "capture.json").write_text(json.dumps({"candidate": IDENTITY["candidate"]}))
        if state.unsafe == "symlink":
            (folder / "scene.png").symlink_to(tmp_path / "private-credential")
        elif state.unsafe == "hardlink":
            os.link(folder / "capture.json", folder / "scene.png")
        elif state.unsafe == "fifo":
            os.mkfifo(folder / "scene.png")
        else:
            (folder / "scene.png").write_bytes(b"complete PNG fixture")


class FakeSandbox:
    def __init__(self, state: Any, *_args: Any) -> None:
        self.state = state
        state.runtime_candidate = _args[0]
        self.owner, self.image = "fixture-owner", "sha256:" + "b" * 64
        self.containers: list[str] = []
        self.network_id = None
        self.rules: list[Any] = []

    def setup(self) -> None:
        self.state.events.append("setup")

    def uncertain_operations(self) -> list[Any]:
        return []

    def verify(self) -> None:
        self.state.events.append("verify")

    def validate_container(self, phase: str) -> None:
        self.state.events.append("validate:" + phase)

    def create(self, phase: str, *_args: Any) -> str:
        self.state.events.append("create:" + phase)
        self.containers.append(phase)
        return phase

    def remove(self, phase: str) -> None:
        self.state.events.append("remove:" + phase)
        self.containers.remove(phase)

    def cleanup(self) -> None:
        self.state.events.append("cleanup")
        if self.state.cleanup_failure:
            self.containers.append("retained-child")
            self.state.live = True
            message = "Cannot establish owned child removal"
            raise ValueError(message)
        self.containers.clear()


@pytest.fixture
def capture(tmp_path: Path, monkeypatch: Any) -> Any:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / ".git").mkdir()
    (candidate / ".git/config").write_text("[core]\nbare = false\n")
    events: list[str] = []
    state = SimpleNamespace(
        candidate=candidate,
        output=tmp_path / "output",
        events=events,
        install_ok=True,
        unsafe=None,
        cleanup_failure=False,
        live=False,
    )

    def output(_container: str, phase: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append("execute:" + phase)
        write_fixture_output(state, tmp_path, phase)
        return {
            "ok": state.install_ok if phase == "install" else True,
            "phase": phase,
            "outputs_complete": True,
            "exit_code": 0,
        }

    real_collect = isolation.collect

    def collect(*args: Any) -> None:
        events.append("collect")
        assert events.index("cleanup") < len(events) - 1
        assert not state.live
        real_collect(*args)

    monkeypatch.setattr(
        isolation,
        "trusted_module",
        lambda name: (
            artifact
            if name == "capture_artifact"
            else SimpleNamespace(Sandbox=lambda *args: FakeSandbox(state, *args))
        ),
    )
    monkeypatch.setattr(
        isolation.subprocess, "check_output", lambda *_a, **_k: IDENTITY["candidate"]
    )
    monkeypatch.setattr(isolation, "contained_output", output)
    monkeypatch.setattr(isolation, "collect", collect)
    return state


def test_only_stopped_containers_are_copied_to_a_fresh_trusted_destination(capture: Any) -> None:
    assert isolation.capture(capture.candidate, capture.output, IDENTITY)
    assert capture.events.index("remove:install") < capture.events.index("create:capture")
    assert capture.events.index("remove:capture") < capture.events.index("collect")
    assert (capture.output / "visual-artifact/scene.png").read_bytes() == b"complete PNG fixture"
    assert not (capture.output / "visual-diagnostics").exists()
    assert not (capture.candidate / "visual-artifact").exists()
    assert (capture.candidate / ".git/config").read_text() == "[core]\nbare = false\n"


def test_install_failure_retains_honest_diagnostics_without_capture_or_approval(
    capture: Any,
) -> None:
    capture.install_ok = False
    assert not isolation.capture(capture.candidate, capture.output, IDENTITY)
    assert "create:capture" not in capture.events
    assert not (capture.output / "visual-artifact").exists()
    failure = json.loads((capture.output / "visual-diagnostics/diagnostics.json").read_bytes())
    assert failure["candidate"] == IDENTITY["candidate"]
    assert failure["visual_approval"] is False
    assert failure["collection_complete"] is False


def test_preparation_failure_retains_an_envelope_without_starting_candidates(capture: Any) -> None:
    (capture.candidate / ".git/config").unlink()
    assert not isolation.capture(capture.candidate, capture.output, IDENTITY)
    assert capture.events == []
    report = json.loads(
        (capture.output / "isolation-diagnostics/preparation-failure.json").read_bytes()
    )
    assert report["ok"] is False
    assert report["visual_approval"] is False
    assert report["stage"] == "transport-preparation-or-finalization"
    failure = json.loads((capture.output / "visual-diagnostics/diagnostics.json").read_bytes())
    assert failure["collection_complete"] is False


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo"])
def test_candidate_output_cannot_make_the_uploader_read_host_files(
    capture: Any, unsafe: str
) -> None:
    capture.unsafe = unsafe
    assert not isolation.capture(capture.candidate, capture.output, IDENTITY)
    assert not (capture.output / "visual-artifact").exists()
    report = json.loads((capture.output / "isolation-diagnostics/isolation.json").read_bytes())
    assert "collection_error" in report


def test_unproven_child_removal_prevents_collection_and_keeps_failure_explicit(
    capture: Any,
) -> None:
    capture.cleanup_failure = True
    assert not isolation.capture(capture.candidate, capture.output, IDENTITY)
    assert "collect" not in capture.events
    report = json.loads((capture.output / "isolation-diagnostics/isolation.json").read_bytes())
    assert report["cleanup_complete"] is False
    assert report["remaining_containers"]


def test_candidate_repository_is_never_imported_by_the_host(capture: Any, monkeypatch: Any) -> None:
    original = builtins.__import__

    def importing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(("agent_tools", "analytics_toolkit")):
            message = "Candidate package imported on host"
            raise AssertionError(message)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    assert isolation.capture(capture.candidate, capture.output, IDENTITY)


@pytest.mark.parametrize("overflow", [False, True])
def test_stdout_and_stderr_have_complete_hashes_and_honest_retention_limits(
    tmp_path: Path,
    monkeypatch: Any,
    overflow: bool,
) -> None:
    stdout, stderr = b"first stdout\n" * 9, b"separate stderr\n" * 7

    class Process:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.stdout, self.stderr = io.BytesIO(stdout), io.BytesIO(stderr)
            self.returncode = 0

        def wait(self, **_kwargs: Any) -> int:
            return 0

    monkeypatch.setattr(isolation.subprocess, "Popen", Process)
    monkeypatch.setattr(isolation, "LOG_PART", 32)
    monkeypatch.setattr(isolation, "MAX_LOG", 100 if overflow else 1000)
    result = isolation.contained_output("owned-container", "install", tmp_path, 1)
    assert result["ok"] is not overflow
    for name, body in (("stdout", stdout), ("stderr", stderr)):
        stream = result["streams"][name]
        assert stream["bytes"] == len(body)
        assert stream["sha256"] == hashlib.sha256(body).hexdigest()
        for part in stream["parts"]:
            raw = (tmp_path / part["path"]).read_bytes()
            assert len(raw) == part["bytes"] <= 32
            assert hashlib.sha256(raw).hexdigest() == part["sha256"]
    if overflow:
        assert sum(x["bytes"] for v in result["streams"].values() for x in v["parts"]) == 100
        assert result["outputs_complete"] is False


def container_info(sandbox: Any) -> dict[str, Any]:
    return {
        "Id": "c" * 64,
        "Image": sandbox.image,
        "Config": {
            "Image": sandbox.image,
            "User": "1001:1001",
            "WorkingDir": "/work",
            "Cmd": ["python", "-c", "pass"],
            "Env": ["PATH=/bin"],
            "Labels": {"atk.visual.owner": sandbox.owner},
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges=true"],
            "NetworkMode": "none",
            "PublishAllPorts": False,
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "PidsLimit": 512,
            "Memory": 6 * 1024**3,
            "NanoCpus": 2 * 10**9,
            "Tmpfs": {"/tmp": "rw,exec,nosuid,nodev,size=3g,mode=1777"},
        },
        "Mounts": [
            {"Destination": "/work", "Type": "bind", "Source": str(sandbox.candidate), "RW": True},
            {
                "Destination": "/trusted",
                "Type": "bind",
                "Source": str(sandbox.trusted),
                "RW": False,
            },
        ],
    }


@pytest.fixture
def configured_sandbox(tmp_path: Path, monkeypatch: Any) -> Any:
    monkeypatch.setattr(sandbox_module.os, "getuid", lambda: 1001)
    monkeypatch.setattr(sandbox_module.os, "getgid", lambda: 1001)
    sandbox = sandbox_module.Sandbox(tmp_path / "candidate", tmp_path / "trusted")
    sandbox.image = "sha256:" + "b" * 64
    sandbox.containers = ["c" * 64]
    sandbox.configurations["c" * 64] = {
        "environment": {"PATH": "/bin"},
        "network": "none",
        "arguments": ["python", "-c", "pass"],
    }
    return sandbox


@pytest.mark.parametrize("fault", [None, "env", "mount", "privilege", "network", "image", "owner"])
def test_actual_container_readback_rejects_credentials_mounts_privileges_or_identity_drift(
    configured_sandbox: Any,
    monkeypatch: Any,
    fault: str | None,
) -> None:
    sandbox = configured_sandbox
    value = container_info(sandbox)
    if fault == "env":
        value["Config"]["Env"].append("GITHUB_TOKEN=must-never-be-forwarded")
    elif fault == "mount":
        value["Mounts"].append({"Destination": "/var/run/docker.sock"})
    elif fault == "privilege":
        value["HostConfig"]["Privileged"] = True
    elif fault == "network":
        value["HostConfig"]["NetworkMode"] = "host"
    elif fault == "image":
        value["Image"] = "sha256:" + "d" * 64
    elif fault == "owner":
        value["Config"]["Labels"] = {}
    monkeypatch.setattr(sandbox_module, "command", lambda *_args: json.dumps([value]))
    if fault:
        with pytest.raises(ValueError, match=r"isolation|ownership"):
            sandbox.validate_container("c" * 64)
    else:
        sandbox.validate_container("c" * 64)


def test_owned_guards_are_not_released_when_child_removal_fails(
    configured_sandbox: Any,
    monkeypatch: Any,
) -> None:
    sandbox = configured_sandbox
    calls: list[Any] = []

    def remove(_container: str) -> None:
        message = "Owned child cannot be stopped"
        raise ValueError(message)

    monkeypatch.setattr(sandbox, "remove", remove)
    monkeypatch.setattr(sandbox, "firewall", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="cannot be stopped"):
        sandbox.cleanup()
    assert calls == []
    assert sandbox.containers == ["c" * 64]


def test_no_copy_is_published_from_an_output_tree_changed_during_read(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "original").write_bytes(b"first")
    original = artifact._regular

    def reading(*args: Any) -> bytes:
        value = original(*args)
        (source / "late-file").write_bytes(b"changed after inventory")
        return value

    monkeypatch.setattr(artifact, "_regular", reading)
    with pytest.raises(ValueError, match="changed during collection"):
        isolation.collect(source, tmp_path / "export", artifact)
    assert not (tmp_path / "export").exists()


def test_aggregate_capacity_is_checked_before_opening_the_excess_file(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a", "b"):
        (source / name).write_bytes(b"1234")
    read: list[str] = []
    original = artifact._regular

    def reading(path: Path, boundary: Path) -> bytes:
        read.append(path.name)
        return original(path, boundary)

    monkeypatch.setattr(artifact, "MAX_TOTAL", 6)
    monkeypatch.setattr(artifact, "_regular", reading)
    with pytest.raises(ValueError, match="aggregate capacity before read"):
        isolation.collect(source, tmp_path / "export", artifact)
    assert read == ["a"]
    assert not (tmp_path / "export").exists()


def test_unjoined_output_pumps_can_only_write_into_quarantine(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    threads: list[Any] = []

    class DeferredThread:
        def __init__(self, target: Any, args: Any, **_kwargs: Any) -> None:
            self.target, self.args = target, args
            threads.append(self)

        def start(self) -> None:
            pass

        def join(self, **_kwargs: Any) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    process = SimpleNamespace(
        stdout=io.BytesIO(b"late stdout"),
        stderr=io.BytesIO(b"late stderr"),
        returncode=0,
        wait=lambda **_kwargs: 0,
    )
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(isolation.threading, "Thread", DeferredThread)
    result = isolation.contained_output("owned", "capture", logs, 1)
    assert not result["ok"]
    assert not result["outputs_complete"]
    assert result["streams"] == {}
    quarantine = Path(result["quarantined_logs"])
    for thread in threads:
        thread.target(*thread.args)
    assert list(logs.iterdir()) == []
    assert sorted(p.read_bytes() for p in quarantine.iterdir()) == [b"late stderr", b"late stdout"]
    assert result["streams"] == {}  # Late state cannot mutate the published report.


def test_interrupt_marks_the_owned_phase_failed_and_drains_cli_output(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class Process:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.stdout, self.stderr = io.BytesIO(b"before interrupt"), io.BytesIO(b"failure")
            self.returncode = None

        def wait(self, **_kwargs: Any) -> int:
            if self.returncode is None:
                raise KeyboardInterrupt
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(isolation.subprocess, "Popen", Process)
    result = isolation.contained_output("owned", "install", tmp_path, 1)
    assert not result["ok"]
    assert result["interrupted"]
    assert not result["timed_out"]
    assert result["exit_code"] == -9
    assert result["readers_stopped"]
    assert not result["outputs_complete"]


class FaultyThread:
    def __init__(
        self, threads: list[Any], fault: str, target: Any, args: Any, **_kwargs: Any
    ) -> None:
        self.fault = fault
        self.target, self.args, self.index = target, args, len(threads)
        threads.append(self)

    def start(self) -> None:
        if self.index == 1 and self.fault in {"second-start", "quarantine"}:
            message = "Second pump startup interrupted after possible launch"
            raise RuntimeError(message)

    def join(self, **_kwargs: Any) -> None:
        if self.index == 0 and self.fault == "join":
            raise KeyboardInterrupt

    def is_alive(self) -> bool:
        return True


@pytest.mark.parametrize("fault", ["second-start", "join", "quarantine"])
def test_reader_start_and_join_failures_cannot_leave_writers_in_uploads(
    tmp_path: Path,
    monkeypatch: Any,
    fault: str,
) -> None:
    uploaded = tmp_path / "output/isolation-diagnostics"
    uploaded.mkdir(parents=True)
    earlier = uploaded / "install-complete.bin"
    earlier.write_bytes(b"earlier complete install stream")
    phase = uploaded / "capture-streams"
    phase.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    threads: list[Any] = []

    process = SimpleNamespace(
        stdout=io.BytesIO(b"late stdout"),
        stderr=io.BytesIO(b"late stderr"),
        returncode=-9,
        wait=lambda **_kwargs: -9,
        kill=lambda: None,
    )
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        isolation.threading,
        "Thread",
        lambda *args, **kwargs: FaultyThread(threads, fault, *args, **kwargs),
    )
    if fault == "quarantine":

        def refused_rename(_source: Path, _target: Path) -> None:
            message = "Synthetic quarantine unavailable"
            raise OSError(message)

        monkeypatch.setattr(Path, "rename", refused_rename)
    result = isolation.contained_output("owned", "capture", phase, 1, quarantine_root=private)
    assert not result["ok"]
    assert not result["outputs_complete"]
    assert result["lifecycle_errors"]
    for thread in threads:
        thread.target(*thread.args)
    assert earlier.read_bytes() == b"earlier complete install stream"
    assert result["streams"] == {}
    if fault == "quarantine":
        assert result["logs_safe"] is False
        assert isolation.UNSAFE_LOG_WRITER.is_set()
        isolation.UNSAFE_LOG_WRITER.clear()  # Synthetic pumps above are now finished.
    else:
        assert list(phase.iterdir()) == []
        assert Path(result["quarantined_logs"]).parent == private
        assert result["logs_safe"] is True


def test_unquarantined_writer_withholds_every_actions_upload_path(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    command_file = tmp_path / "command-file"
    command_file.touch()
    values = {
        "CANDIDATE": "a" * 40,
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(command_file),
        "CANDIDATE_ROOT": str(tmp_path / "candidate"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    def unsafe_capture(*_args: Any) -> bool:
        isolation.UNSAFE_LOG_WRITER.set()
        return False

    monkeypatch.setattr(isolation, "capture", unsafe_capture)
    monkeypatch.setattr(isolation.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(isolation.signal, "alarm", lambda *_args: None)
    try:
        with pytest.raises(SystemExit, match="Isolated visual capture failed"):
            isolation.main()
        assert command_file.read_bytes() == b""
    finally:
        isolation.UNSAFE_LOG_WRITER.clear()


def test_accepted_create_with_lost_response_is_durable_and_cannot_claim_clean(
    configured_sandbox: Any,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    sandbox = configured_sandbox
    sandbox.containers.clear()
    sandbox.state_path = tmp_path / "ownership.json"
    created: list[str] = []

    def creating(arguments: list[str]) -> str:
        created.append("accepted-but-reply-lost")
        raise subprocess.TimeoutExpired(arguments, 30)

    monkeypatch.setattr(sandbox_module, "command", creating)
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox.mutation(["docker", "create", "--name", "owned-fixture"])
    state = json.loads(sandbox.state_path.read_bytes())
    assert created
    assert state["owner"] == sandbox.owner
    assert state["operations"][0]["state"] == "uncertain"
    with pytest.raises(ValueError, match="response uncertain"):
        sandbox.cleanup()
    assert len(created) == 1  # No guessed delete, guard release or success claim.
