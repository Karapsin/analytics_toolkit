"""Trusted host transport for contained visual capture and safe artifact collection."""

from __future__ import annotations

import hashlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
from typing import Any
import uuid

MAX_LOG = 100 * 1024 * 1024
LOG_PART = 8 * 1024 * 1024
RUNTIME_BUDGET = False
UNSAFE_LOG_WRITER = threading.Event()


def trusted_module(name: str) -> Any:
    if name not in {"capture_artifact", "capture_sandbox"}:
        raise ValueError("Unknown trusted transport module")
    path = Path(__file__).resolve().parent / (name + ".py")
    spec = importlib.util.spec_from_file_location("isolated_visual_" + name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Trusted transport missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contained_output(container: str, phase: str, destination: Path, timeout: int,
                     limit: int | None = None, quarantine_root: Path | None = None) -> dict[str, Any]:
    """Drain both complete streams; explicitly report any retained-log capacity loss."""
    lock = threading.Lock()
    retained = [0]
    streams: dict[str, Any] = {}
    failures: list[str] = []
    capacity = MAX_LOG if limit is None else min(MAX_LOG, max(0, limit))
    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def pump(stream: Any, name: str, own_directory: int) -> None:
        digest = hashlib.sha256()
        count = 0
        parts: list[dict[str, Any]] = []
        file = None
        part_digest = None
        try:
            with stream:
                for body in iter(lambda: stream.read(65536), b""):
                    count += len(body)
                    digest.update(body)
                    with lock:
                        length = min(len(body), capacity - retained[0])
                        retained[0] += length
                    remaining = memoryview(body)[:length]
                    while remaining:
                        if file is None or parts[-1]["bytes"] == LOG_PART:
                            if file is not None:
                                file.close()
                                parts[-1]["sha256"] = part_digest.hexdigest()
                            path = destination / f"{phase}-{name}-{len(parts):06d}.bin"
                            descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                                 0o600, dir_fd=own_directory)
                            file = os.fdopen(descriptor, "wb")
                            part_digest = hashlib.sha256()
                            parts.append({"path": path.name, "bytes": 0})
                        length = min(len(remaining), LOG_PART - parts[-1]["bytes"])
                        file.write(remaining[:length])
                        part_digest.update(remaining[:length])
                        parts[-1]["bytes"] += length
                        remaining = remaining[length:]
        except Exception as exc:
            failures.append(type(exc).__name__ + ": " + str(exc))
        finally:
            if file is not None:
                file.close()
                parts[-1]["sha256"] = part_digest.hexdigest()
            with lock:
                streams[name] = {"bytes": count, "sha256": digest.hexdigest(), "parts": parts,
                                 "retained_complete": sum(x["bytes"] for x in parts) == count}
            os.close(own_directory)

    process = None
    readers: list[Any] = []
    timed_out = False
    was_interrupted = False
    lifecycle_errors: list[str] = []
    stopped = False
    quarantine = None
    try:
        process = subprocess.Popen(["docker", "start", "--attach", container],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            own_directory = os.dup(directory)
            try:
                reader = threading.Thread(target=pump, args=(stream, name, own_directory), daemon=True)
            except BaseException:
                os.close(own_directory)
                raise
            readers.append(reader)
            # From start() onward the pump may own its descriptor even if start
            # raises after launching it. Never race a late pump with a host close.
            UNSAFE_LOG_WRITER.set()
            reader.start()
        process.wait(timeout=timeout)
    except BaseException as exc:
        timed_out = isinstance(exc, (subprocess.TimeoutExpired, TimeoutError))
        was_interrupted = isinstance(exc, (InterruptedError, KeyboardInterrupt))
        lifecycle_errors.append(type(exc).__name__ + ": " + str(exc))
    finally:
        os.close(directory)
        if process is not None and lifecycle_errors:
            try:
                process.kill()
                process.wait(timeout=30)
            except BaseException as exc:
                lifecycle_errors.append("CLI cleanup: " + type(exc).__name__ + ": " + str(exc))
        for reader in readers:
            try:
                reader.join(timeout=5)
            except BaseException as exc:
                lifecycle_errors.append("Reader join: " + type(exc).__name__ + ": " + str(exc))
        try:
            stopped = not any(x.is_alive() for x in readers)
        except BaseException as exc:
            lifecycle_errors.append("Reader state: " + type(exc).__name__ + ": " + str(exc))
        if not stopped or lifecycle_errors:
            # Include startup/join failures: a start() exception is not proof
            # that a pump never started. Earlier phase logs remain separate.
            quarantine = (quarantine_root or destination.parent) / (
                "quarantined-logs-" + uuid.uuid4().hex)
            try:
                destination.rename(quarantine)
                destination.mkdir(mode=0o700)
                UNSAFE_LOG_WRITER.clear()
            except BaseException as exc:
                lifecycle_errors.append("Log quarantine: " + type(exc).__name__ + ": " + str(exc))
        else:
            UNSAFE_LOG_WRITER.clear()
    complete = not failures and not lifecycle_errors and len(streams) == 2 and stopped
    complete = complete and all(x["retained_complete"] for x in streams.values())
    with lock:
        snapshot = copy.deepcopy(streams)
    exit_code = process.returncode if process is not None else None
    return {"phase": phase, "exit_code": exit_code, "timed_out": timed_out,
            "interrupted": was_interrupted,
            "outputs_complete": complete, "streams": snapshot, "collection_errors": list(failures),
            "readers_stopped": stopped, "quarantined_logs": str(quarantine) if quarantine else None,
            "lifecycle_errors": lifecycle_errors, "logs_safe": not UNSAFE_LOG_WRITER.is_set(),
            "ok": exit_code == 0 and not timed_out and not was_interrupted and complete}


def collect(source: Path, destination: Path, artifact: Any) -> None:
    """Copy stopped-container output as data through no-follow descriptors only."""
    inventory = artifact._inventory(source)
    records = []
    total = 0
    for name in sorted(inventory):
        kind, info = inventory[name]
        if kind != "file":
            continue
        if total + info[2] > artifact.MAX_TOTAL:
            raise ValueError("Complete artifact exceeds aggregate capacity before read")
        body = artifact._regular(source / name, source)
        total += len(body)
        if total > artifact.MAX_TOTAL:
            raise ValueError("Complete artifact exceeds aggregate capacity")
        records.append((name, body))
    if artifact._inventory(source) != inventory:
        raise ValueError("Container output changed during collection")
    artifact._publish(destination, records)


def capture(candidate: Path, output: Path, identity: dict[str, Any]) -> bool:
    """Retain a failed preparation envelope before any candidate can execute."""
    artifact = trusted_module("capture_artifact")
    descriptor = artifact._directory(output.parent)
    os.close(descriptor)
    output.mkdir(mode=0o700)
    logs = output / "isolation-diagnostics"
    logs.mkdir(mode=0o700)
    try:
        return _capture(candidate, output, identity)
    except (Exception, KeyboardInterrupt) as exc:
        report = {"schema_version": 1, "kind": "visual-capture-isolation", **identity,
                  "ok": False, "stage": "transport-preparation-or-finalization",
                  "error": type(exc).__name__ + ": " + str(exc),
                  "visual_approval": False}
        artifact.export_failure(output / "visual-diagnostics", None, identity=identity,
                                result=report)
        (logs / "preparation-failure.json").write_text(json.dumps(report, indent=2) + "\n")
        return False


def _capture(candidate: Path, output: Path, identity: dict[str, Any]) -> bool:
    artifact = trusted_module("capture_artifact")
    sandbox_module = trusted_module("capture_sandbox")
    trusted = Path(__file__).resolve().parents[2]
    staging = candidate.absolute()
    # Validate both roots before candidate programs run; never run host Git afterwards.
    for path in (staging, trusted, output.parent):
        descriptor = artifact._directory(path)
        os.close(descriptor)
    if staging == trusted or staging in trusted.parents or output == staging or staging in output.parents:
        raise ValueError("Candidate and trusted transport roots overlap")
    if any("," in str(path) for path in (staging, trusted)):
        raise ValueError("Unsupported bind-mount path")
    environment = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null"}
    observed = subprocess.check_output(["git", "-C", str(staging), "rev-parse", "HEAD"],
                                       env=environment, text=True, timeout=30).strip()
    if observed != identity["candidate"]:
        raise ValueError("Candidate checkout differs from dispatch")
    # Checkout's post action may run host Git. Its repository remains pristine;
    # only an independent run-owned copy is ever visible to candidate programs.
    candidate = output.parent / ("atk-visual-candidate-" + uuid.uuid4().hex)
    shutil.copytree(staging, candidate, symlinks=True)
    config = candidate / ".git/config"
    if not (candidate / ".git").is_dir() or (candidate / ".git").is_symlink():
        raise ValueError("Independent candidate Git directory missing")
    if config.is_symlink() or not config.is_file() or config.stat().st_nlink != 1:
        raise ValueError("Independent candidate Git config is unsafe")
    if any((candidate / name).exists() for name in (
        ".git/objects/info/alternates", ".git/commondir", ".git/gitdir")):
        raise ValueError("Independent candidate cannot reference external object storage")
    # Do not transplant checkout credential includes/helpers into the writable copy.
    config.write_text("[core]\nrepositoryformatversion = 0\nfilemode = true\nbare = false\n"
                      "logallrefupdates = true\nhooksPath = /dev/null\n"
                      '[remote "origin"]\nurl = https://github.com/' + identity["repository"] +
                      ".git\nfetch = +refs/heads/*:refs/remotes/origin/*\n")
    logs = output / "isolation-diagnostics"
    sandbox = sandbox_module.Sandbox(candidate, trusted, logs / "ownership.json")
    report: dict[str, Any] = {"schema_version": 1, "kind": "visual-capture-isolation",
                              **identity, "owner": sandbox.owner, "phases": [], "ok": False}
    error = None
    try:
        sandbox.setup()
        report["image"] = sandbox.image
        install = sandbox.create("install", ["sh", "-ec",
            "python -m venv /work/.venv && /work/.venv/bin/python -m pip install "
            "-e '.[tui]' -r agent_tools/requirements-mcp.txt"], identity)
        sandbox.verify()
        sandbox.validate_container(install)
        install_logs = logs / "install-streams"
        install_logs.mkdir(mode=0o700)
        result = contained_output(install, "install", install_logs, 600,
                                  quarantine_root=output.parent)
        result["stream_directory"] = "install-streams"
        report["phases"].append(result)
        sandbox.remove(install)
        if result["ok"]:
            container = sandbox.create("capture", ["/work/.venv/bin/python",
                "/trusted/.github/agent/capture_artifact.py"], identity)
            sandbox.verify()
            sandbox.validate_container(container)
            retained = sum(path.stat().st_size for path in logs.rglob("*") if path.is_file())
            capture_logs = logs / "capture-streams"
            capture_logs.mkdir(mode=0o700)
            result = contained_output(container, "capture", capture_logs, 900,
                                      MAX_LOG - retained, quarantine_root=output.parent)
            result["stream_directory"] = "capture-streams"
            report["phases"].append(result)
            sandbox.remove(container)
            report["ok"] = result["ok"]
    except (Exception, KeyboardInterrupt) as exc:
        error = type(exc).__name__ + ": " + str(exc)
    finally:
        if RUNTIME_BUDGET:
            signal.alarm(90)
        try:
            sandbox.cleanup()
            report["cleanup_complete"] = True
        except (Exception, KeyboardInterrupt) as exc:
            report["cleanup_complete"] = False
            report["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
            report["ok"] = False
        report["remaining_containers"] = list(sandbox.containers)
        report["remaining_network"] = sandbox.network_id
        report["remaining_guards"] = sandbox.rules
        report["uncertain_operations"] = sandbox.uncertain_operations()
        if RUNTIME_BUDGET:
            signal.alarm(60)
    if error:
        report["error"] = error
        report["ok"] = False
    try:
        if sandbox.containers or report["uncertain_operations"]:
            report["ok"] = False
            report["collection_refused"] = "Owned child/resource absence cannot be established"
            artifact.export_failure(output / "visual-diagnostics", None, identity=identity,
                                    result={"ok": False, "stage": "uncertain-isolation-cleanup"})
        elif report["ok"]:
            collect(candidate / "visual-artifact", output / "visual-artifact", artifact)
        else:
            failed = candidate / "visual-diagnostics"
            if failed.exists() or failed.is_symlink():
                collect(failed, output / "visual-diagnostics", artifact)
            else:
                artifact.export_failure(output / "visual-diagnostics", None,
                    identity=identity, result={"ok": False, "stage": "isolated-setup-or-capture",
                                              "isolation_report": "isolation-diagnostics/isolation.json"})
    except (Exception, KeyboardInterrupt) as exc:
        report["ok"] = False
        report["collection_error"] = type(exc).__name__ + ": " + str(exc)
    if RUNTIME_BUDGET:
        signal.alarm(0)
    (logs / "isolation.json").write_text(json.dumps(report, indent=2) + "\n")
    return report["ok"] is True


def main() -> None:
    global RUNTIME_BUDGET
    identity = {"candidate": os.environ["CANDIDATE"],
                "repository": os.environ["GITHUB_REPOSITORY"],
                "run_id": int(os.environ["GITHUB_RUN_ID"]),
                "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}
    if re.fullmatch(r"[0-9a-f]{40}", identity["candidate"]) is None or (
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", identity["repository"]) is None
    ) or min(identity["run_id"], identity["run_attempt"]) <= 0:
        raise ValueError("Invalid dispatch identity")
    root = Path(os.environ["RUNNER_TEMP"]) / ("atk-visual-" + uuid.uuid4().hex)
    def interrupted(_number: int, _frame: Any) -> None:
        if _number == signal.SIGALRM:
            raise TimeoutError("Visual transport wall-clock budget expired")
        raise InterruptedError("Visual transport interrupted; owned cleanup required")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    # Leave eight minutes of the job budget for owned cleanup and safe upload.
    signal.alarm(22 * 60)
    RUNTIME_BUDGET = True
    try:
        ok = capture(Path(os.environ["CANDIDATE_ROOT"]), root, identity)
    finally:
        signal.alarm(0)
        RUNTIME_BUDGET = False
        # Never give upload actions a path that may retain an unquarantined
        # writer. This command file is host-only and never enters a container.
        if not UNSAFE_LOG_WRITER.is_set():
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write("artifact_root=" + str(root) + "\n")
    if not ok:
        raise SystemExit("Isolated visual capture failed; see separate complete/incomplete diagnostics")


if __name__ == "__main__":
    main()
