"""Bounded raw child output for headless visual diagnostics only."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

MAX_STREAM_BYTES = 8 * 1024 * 1024
DRAIN_SECONDS = 5


class CaptureProcessError(RuntimeError):
    """A failed capture whose private raw output and outcome were retained."""


def _stop(transport: asyncio.SubprocessTransport) -> None:
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(transport.get_pid(), signal.SIGKILL)
    with suppress(ProcessLookupError):
        transport.kill()
    for descriptor in (1, 2):
        pipe = transport.get_pipe_transport(descriptor)
        if pipe is not None:
            pipe.close()


class _Output(asyncio.SubprocessProtocol):
    def __init__(self, destination: Path, record: dict[str, Any]):
        self.record = record
        self.exited = asyncio.Event()
        self.drained = asyncio.Event()
        self.closed = False
        self.lost: set[int] = set()
        self.files: dict[int, Any] = {}
        try:
            for descriptor, name in ((1, "stdout"), (2, "stderr")):
                self.files[descriptor] = (destination / (name + ".bin")).open("xb")
        except BaseException:
            self.close()
            raise
        self.transport: Any = None
        self.record["streams"] = {
            name: {"bytes": 0, "complete": False, "capacity_exceeded": False}
            for name in ("stdout", "stderr")
        }

    def connection_made(self, transport: Any) -> None:
        self.transport = transport
        if self.closed:
            _stop(transport)
            transport.close()

    def pipe_data_received(self, descriptor: int, data: bytes) -> None:
        if self.closed:
            return
        name = "stdout" if descriptor == 1 else "stderr"
        state = self.record["streams"][name]
        room = MAX_STREAM_BYTES - state["bytes"]
        try:
            self.files[descriptor].write(data[:room])
            state["bytes"] += min(room, len(data))
            if len(data) > room:
                state["capacity_exceeded"] = True
                _stop(self.transport)
        except OSError as exc:
            state["error"] = type(exc).__name__
            _stop(self.transport)

    def pipe_connection_lost(self, descriptor: int, exc: Exception | None) -> None:
        if self.closed or descriptor not in self.files:
            return
        self.lost.add(descriptor)
        state = self.record["streams"]["stdout" if descriptor == 1 else "stderr"]
        state["complete"] = exc is None
        if exc is not None:
            state["error"] = type(exc).__name__
        if self.lost == {1, 2}:
            self.drained.set()

    def process_exited(self) -> None:
        self.exited.set()

    def close(self) -> None:
        # No callback may mutate output or metadata after this flag is set.
        self.closed = True
        for stream in self.files.values():
            stream.close()


async def _exit_after_stop(protocol: _Output, record: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(protocol.exited.wait(), timeout=DRAIN_SECONDS)
    except asyncio.TimeoutError:
        record["exit_timeout"] = True


async def _observe(transport: Any, protocol: _Output, timeout: int, record: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(protocol.exited.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        record["timed_out"] = True
        _stop(transport)
        await _exit_after_stop(protocol, record)
    try:
        await asyncio.wait_for(protocol.drained.wait(), timeout=DRAIN_SECONDS)
    except asyncio.TimeoutError:
        record["drain_timeout"] = True
        _stop(transport)  # Also kills descendants after the direct child exits.
    record["returncode"] = transport.get_returncode()
    record["outputs_complete"] = (
        not record["timed_out"]
        and not record["drain_timeout"]
        and protocol.drained.is_set()
        and all(
            item["complete"] and not item["capacity_exceeded"] and "error" not in item
            for item in record["streams"].values()
        )
    )


async def _capture(
    command: list[str], cwd: Path, timeout: int, destination: Path
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "command": command,
        "timeout_seconds": timeout,
        "timed_out": False,
        "drain_timeout": False,
        "outputs_complete": False,
    }
    protocol = _Output(destination, record)
    transport = None
    try:
        loop = asyncio.get_running_loop()
        transport, _ = await asyncio.wait_for(
            loop.subprocess_exec(
                lambda: protocol,
                *command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            ),
            timeout=timeout,
        )
        await _observe(transport, protocol, timeout, record)
    except BaseException as exc:  # noqa: BLE001 - Cancellation must close owned process transports.
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        owned = transport if transport is not None else protocol.transport
        if owned is not None:
            _stop(owned)
            await _exit_after_stop(protocol, record)
    finally:
        protocol.close()
        owned = transport if transport is not None else protocol.transport
        if owned is not None:
            owned.close()
    return record


def run_capture(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    destination: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Use an isolated event loop so an existing MCP loop is never nested."""
    destination.mkdir(parents=True, exist_ok=False)
    result: list[dict[str, Any]] = []
    control: dict[str, Any] = {}
    cancelled = threading.Event()
    completed = threading.Event()

    async def capture() -> dict[str, Any]:
        control.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        if cancelled.is_set():
            raise asyncio.CancelledError
        return await _capture(command, cwd, timeout, destination)

    def execute() -> None:
        try:
            result.append(asyncio.run(capture()))
        except BaseException as exc:  # noqa: BLE001 - Report thread failure before signaling completion.
            result.append({"outputs_complete": False, "error": type(exc).__name__})
        finally:
            completed.set()

    def cancel() -> None:
        cancelled.set()
        if "loop" in control:
            with suppress(
                RuntimeError
            ):  # A closing loop still signals completed before publication.
                control["loop"].call_soon_threadsafe(control["task"].cancel)

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        completed.wait()
    except BaseException:
        cancel()
        while not completed.is_set():
            cancel()
            with suppress(
                BaseException
            ):  # Repeated interrupts still wait for closed output streams.
                completed.wait()
        worker.join()
        raise
    worker.join()  # No pipe-reader thread or live callback may outlast final metadata.
    record = result[0]
    (destination / "execution.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    if not record.get("outputs_complete") or (check and record.get("returncode")):
        message = f"capture failed; complete outcome diagnostics: {destination}"
        raise CaptureProcessError(message)
    return subprocess.CompletedProcess(command, record["returncode"], "", "")
