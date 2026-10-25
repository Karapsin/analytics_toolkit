"""Deterministic subprocess-transport boundary for visual diagnostic tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agent_tools import sql_explorer_visual_process as process

if TYPE_CHECKING:
    import pytest


class _Pipe:
    def __init__(self, protocol: Any, descriptor: int):
        self.protocol, self.descriptor, self.closed = protocol, descriptor, False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.protocol.pipe_connection_lost(self.descriptor, None)


class _Transport:
    def __init__(self, protocol: Any):
        self.protocol, self.returncode, self.closed = protocol, None, False
        self.pipes = {i: _Pipe(protocol, i) for i in (1, 2)}

    def get_pid(self) -> int:
        return 12345

    def kill(self) -> None:
        if self.returncode is not None:
            raise ProcessLookupError
        self.returncode = -9
        self.protocol.process_exited()

    def finish(self) -> None:
        if self.returncode is None:
            self.returncode = 7
        self.protocol.process_exited()

    def get_returncode(self) -> Any:
        return self.returncode

    def get_pipe_transport(self, descriptor: int) -> _Pipe:
        return self.pipes[descriptor]

    def close(self) -> None:
        self.closed = True
        for pipe in self.pipes.values():
            pipe.close()


class _Fixture:
    def __init__(self, timed_out: bool, descendant: bool, stdout: bytes, spawn_error: bool):
        self.timed_out, self.descendant = timed_out, descendant
        self.stdout, self.spawn_error = stdout, spawn_error
        self.observed: dict[str, Any] = {"group_kills": []}
        self.original_wait, self.calls = asyncio.wait_for, 0

    async def start(self, loop: Any, factory: Any, *_a: Any, **_k: Any) -> Any:
        protocol = factory()
        transport = _Transport(protocol)
        self.observed.update(protocol=protocol, transport=transport)
        protocol.connection_made(transport)
        if self.spawn_error:
            message = "spawn failed after transport ownership"
            raise OSError(message)
        protocol.pipe_data_received(1, self.stdout)
        protocol.pipe_data_received(2, b"different error\n")
        if not self.timed_out:
            loop.call_soon(transport.finish)
        if not self.descendant and not self.timed_out:
            for pipe in transport.pipes.values():
                loop.call_soon(pipe.close)
        return transport, protocol

    async def wait(self, awaitable: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if (self.timed_out and self.calls == 2) or (self.descendant and self.calls == 3):
            awaitable.close()
            raise asyncio.TimeoutError
        return await self.original_wait(awaitable, **kwargs)


def fake_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timed_out: bool = False,
    descendant: bool = False,
    stdout: bytes = b"partial output\n",
    spawn_error: bool = False,
) -> dict[str, Any]:
    fixture = _Fixture(timed_out, descendant, stdout, spawn_error)

    async def start(loop: Any, factory: Any, *args: Any, **kwargs: Any) -> Any:
        return await fixture.start(loop, factory, *args, **kwargs)

    monkeypatch.setattr(asyncio.BaseEventLoop, "subprocess_exec", start)
    monkeypatch.setattr(process.asyncio, "wait_for", fixture.wait)
    monkeypatch.setattr(
        process.os, "killpg", lambda pid, sig: fixture.observed["group_kills"].append((pid, sig))
    )
    return fixture.observed
