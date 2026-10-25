"""Private logs, process deadlines and locks for interactive session preparation."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NETWORK_ERRORS = (
    "readtimeout",
    "read timed out",
    "read operation timed out",
    "connecttimeout",
    "connectionerror",
    "connection reset",
    "connection broken",
    "connection aborted",
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "remote end closed connection",
    "incompleteread",
    "chunkedencodingerror",
    "503 server error",
    "500 server error",
    "502 server error",
    "504 server error",
    "too many 500",
    "too many 502",
    "too many 503",
    "too many 504",
)
PERMANENT_ERRORS = (
    "resolutionimpossible",
    "conflicting dependencies",
    "no matching distribution",
    "could not find a version",
    "certificate_verify_failed",
    "401 client error",
    "403 client error",
    "hashes from the requirements file",
)


def progress(message: str) -> None:
    if os.environ.get("ATK_BOOTSTRAP_PROGRESS") == "1":
        print(message, file=sys.stderr, flush=True)


@contextlib.contextmanager
def lock(path: Path, *, wait: bool = True) -> Iterator[None]:
    """Use an OS lock: process death releases it without stale lock recovery."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        if os.name == "nt":  # pragma: no cover - the interactive launcher is Unix-only.
            import msvcrt  # noqa: PLC0415

            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415 - keep imports portable for the MCP CLI.

            fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield


def _tail(path: Path, offset: int = 0) -> str:
    with path.open("rb") as stream:
        stream.seek(max(offset, path.stat().st_size - 8000))
        return stream.read().decode("utf-8", errors="replace")


def _wait(process: subprocess.Popen[bytes], deadline: float, label: str) -> int:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 900)
        try:
            return process.wait(timeout=min(15, remaining))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise
            progress(f"{label}: still running; full output is in the bootstrap log.")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - Unix interactive launcher.
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":  # pragma: no cover
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run(root: Path, command: dict[str, Any]) -> dict[str, Any]:
    """Run a bootstrap step with bounded retries, keeping full output off stdout."""
    directory = root / ".rag_index/bootstrap"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = directory / f"{uuid.uuid4().hex}.log"
    environment = os.environ.copy()
    environment.update(command.get("env") or {})
    environment.setdefault("PIP_DEFAULT_TIMEOUT", "120")
    environment.setdefault("PIP_RETRIES", "5")
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_INPUT"] = "1"
    started = time.monotonic()
    deadline = started + float(command.get("timeout", 900))
    attempts = 3 if command.get("network") else 1
    label = str(command["display"])
    returncode: int | None = None
    error = ""
    with os.fdopen(os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as output:
        for attempt in range(attempts):
            progress(f"{label} (attempt {attempt + 1}/{attempts}); log: {log}")
            offset = output.tell()
            output.write(f"\n{label}; attempt {attempt + 1}\n".encode())
            output.flush()
            try:
                with subprocess.Popen(
                    command["args"],
                    cwd=root,
                    env=environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                ) as process:
                    try:
                        returncode = _wait(process, deadline, label)
                    except BaseException:
                        _terminate(process)
                        raise
            except subprocess.TimeoutExpired:
                error = "Bootstrap step exceeded its time limit."
                break
            except OSError as exc:
                error = str(exc)
                break
            if returncode == 0:
                break
            excerpt = _tail(log, offset).lower()
            retryable = any(item in excerpt for item in NETWORK_ERRORS) and not any(
                item in excerpt for item in PERMANENT_ERRORS
            )
            delay = (2, 5, 0)[attempt]
            if not retryable or attempt + 1 == attempts or time.monotonic() + delay >= deadline:
                break
            progress(f"Transient network failure; retrying in {delay}s.")
            time.sleep(delay)
    ok = returncode == 0 and not error
    return {
        "ok": ok,
        "command": label,
        "returncode": returncode,
        "stdout": "",
        "stderr": "" if ok else error or _tail(log)[-2000:],
        "summary": "Completed" if ok else error or "Bootstrap command failed.",
        "log_ref": str(log.relative_to(root)),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
