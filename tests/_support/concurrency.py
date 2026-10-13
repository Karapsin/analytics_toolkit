"""Bounded synchronization for real-worker tests, without scheduling delays."""

from __future__ import annotations

import asyncio
from concurrent.futures import as_completed
from contextlib import contextmanager
from threading import Barrier, Event, Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest


WAIT_SECONDS = 10.0
_ANY_RESULT = object()


class ConcurrencyProbe:
    """Rendezvous the first worker wave and observe all subsequent calls.

    A timeout detects too little parallelism. The high-water mark detects too
    much. Later calls need not form full waves (e.g. eleven tasks, cap five).
    """

    def __init__(self, expected: int, *, timeout: float = WAIT_SECONDS) -> None:
        if expected < 1:
            message = "Expected concurrency must be positive"
            raise ValueError(message)
        self.expected = expected
        self.timeout = timeout
        self.active = 0
        self.maximum = 0
        self.calls = 0
        self._lock = Lock()
        self._first_wave = Barrier(expected)

    @contextmanager
    def worker(self) -> Iterator[None]:
        with self._lock:
            self.calls += 1
            first_wave = self.calls <= self.expected
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if first_wave:
                self._first_wave.wait(timeout=self.timeout)
            yield
        finally:
            with self._lock:
                self.active -= 1

    def assert_complete(self, *, calls: int) -> None:
        assert not self._first_wave.broken, "Expected workers did not rendezvous"
        assert self.calls == calls, f"Expected {calls} calls, got {self.calls}"
        assert self.active == 0, f"{self.active} workers remain active"
        assert self.maximum == self.expected, (
            f"Expected concurrency {self.expected}, observed {self.maximum}"
        )


def wait_event(event: Event) -> None:
    assert event.wait(WAIT_SECONDS), "Worker did not signal the expected state"


async def wait_event_async(event: Event) -> None:
    # run_in_executor keeps the UI/event loop responsive and works on Python 3.8.
    await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(None, wait_event, event),
        timeout=WAIT_SECONDS + 1,
    )


def release_on_completion(monkeypatch: pytest.MonkeyPatch, module: Any, release: Event) -> None:
    """Unblock a slow fake only after another real Future has completed."""

    def completed(futures: Any) -> Iterator[Any]:
        try:
            for future in as_completed(futures, timeout=WAIT_SECONDS):
                release.set()
                yield future
        finally:
            release.set()

    monkeypatch.setattr(module, "as_completed", completed)


def observe_call(
    monkeypatch: pytest.MonkeyPatch,
    target: Any,
    name: str,
    *,
    after_error: type[Exception] | None = None,
    after_result: object = _ANY_RESULT,
) -> Event:
    """Observe a real blocking primitive, optionally after a failed attempt."""
    entered = Event()
    original = getattr(target, name)

    def observed(*args: Any, **kwargs: Any) -> Any:
        if after_error is None and after_result is _ANY_RESULT:
            entered.set()
        try:
            result = original(*args, **kwargs)
        except Exception as error:
            if after_error is not None and isinstance(error, after_error):
                entered.set()
            raise
        if after_result is not _ANY_RESULT and result == after_result:
            entered.set()
        return result

    monkeypatch.setattr(target, name, observed)
    return entered
