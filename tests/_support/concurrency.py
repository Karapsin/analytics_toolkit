"""Bounded synchronization for real-worker tests, without scheduling delays."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, as_completed
from contextlib import contextmanager
from threading import Barrier, Condition, Event, Lock, Thread, local
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import pytest


WAIT_SECONDS = 10.0
_ANY_RESULT = object()


class ConcurrencyProbe:
    """Rendezvous the first worker wave and observe all subsequent calls.

    Rendezvous mode checks overlap only. For cap checks use held=True with
    SchedulingObservation.run(), which releases only after scheduling evidence.
    Later calls need not form full waves (e.g. eleven tasks, cap five).
    """

    def __init__(self, expected: int, *, timeout: float = WAIT_SECONDS, held: bool = False) -> None:
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
        self.held = held
        self.release = Event()
        self.observation: SchedulingObservation | None = None

    @contextmanager
    def worker(self) -> Iterator[None]:
        with self._lock:
            self.calls += 1
            first_wave = self.calls <= self.expected
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if self.observation is not None:
                self.observation.entered()
            if self.held:
                assert self.release.wait(self.timeout + WAIT_SECONDS), (
                    "Held workers were not released"
                )
            elif first_wave:
                self._first_wave.wait(timeout=self.timeout)
            yield
        finally:
            with self._lock:
                self.active -= 1

    def assert_complete(self, *, calls: int) -> None:
        assert self.held or not self._first_wave.broken, "Expected workers did not rendezvous"
        assert self.calls == calls, f"Expected {calls} calls, got {self.calls}"
        assert self.active == 0, f"{self.active} workers remain active"
        assert self.maximum == self.expected, (
            f"Expected concurrency {self.expected}, observed {self.maximum}"
        )


class SchedulingObservation:
    """Observe initial executor capacity and real limiter contention.

    Submitted work is not evidence of cap enforcement. Every candidate in each
    pool's initial capacity must enter the operation or block on a real limiter.
    Queued candidates are accounted for only after that entire wave is observed.
    """

    def __init__(self, probe: ConcurrencyProbe, *, calls: int) -> None:
        assert probe.held, "Cap checks require held workers"
        self.probe = probe
        probe.observation = self
        self.calls = calls
        self._condition = Condition()
        self._local = local()
        self._candidates: set[object] = set()
        self._entered: set[object] = set()
        self._blocked: set[object] = set()
        self._pools: list[tuple[int, list[object]]] = []

    def register(self, candidate: object) -> None:
        with self._condition:
            self._candidates.add(candidate)
            self._condition.notify_all()

    def blocked(self, candidate: object | None = None) -> None:
        if candidate is None:
            candidate = getattr(self._local, "candidate", None)
        if candidate is not None:
            with self._condition:
                self._candidates.add(candidate)
                self._blocked.add(candidate)
                self._condition.notify_all()

    def entered(self) -> None:
        candidate = self._local.candidate
        with self._condition:
            self._entered.add(candidate)
            self._condition.notify_all()

    def semaphore_factory(self, original: Any) -> Any:
        observation = self

        class ObservedSemaphore(original):
            def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
                if not blocking:
                    return super().acquire(blocking=blocking, timeout=timeout)
                if super().acquire(blocking=False):
                    return True
                observation.blocked()
                return super().acquire(blocking=blocking, timeout=timeout)

            # threading.Semaphore aliases __enter__ to its own acquire function.
            # Bind the override too, so ExitStack observes the same real contention.
            __enter__ = acquire

        return ObservedSemaphore

    def executor_factory(
        self,
        original: Any,
        *,
        candidate_key: Callable[[], object] = object,
        include: Callable[[tuple[Any, ...]], bool] = lambda _args: True,
    ) -> Any:
        observation = self

        class ObservedExecutor(original):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.candidates: list[object] = []
                with observation._condition:
                    observation._pools.append((self._max_workers, self.candidates))

            def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
                if not include(args):
                    return super().submit(fn, *args, **kwargs)
                candidate = candidate_key()
                with observation._condition:
                    self.candidates.append(candidate)
                    observation._candidates.add(candidate)
                    observation._condition.notify_all()

                def observed() -> Any:
                    previous = getattr(observation._local, "candidate", None)
                    observation._local.candidate = candidate
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        observation._local.candidate = previous

                return super().submit(observed)

        return ObservedExecutor

    def _ready(self) -> bool:
        if len(self._candidates) != self.calls:
            return False
        settled = self._entered | self._blocked
        queued: set[object] = set()
        for capacity, candidates in self._pools:
            wave = set(candidates[:capacity])
            if not wave <= settled:
                return False
            queued.update(candidates[capacity:])
        return self._candidates <= settled | queued and len(self._entered) >= self.probe.expected

    def run(self, operation: Callable[[], Any]) -> Any:
        """Release in finally before bounded join; propagate operation failures."""
        future: Future[Any] = Future()

        def invoke() -> None:
            try:
                future.set_result(operation())
            # Forward the original exception to the calling thread, including interrupts.
            except BaseException as error:  # noqa: BLE001
                future.set_exception(error)
            finally:
                with self._condition:
                    self._condition.notify_all()

        controller = Thread(target=invoke, daemon=True)
        controller.start()
        try:
            with self._condition:
                observed = self._condition.wait_for(
                    lambda: future.done() or self._ready(), timeout=self.probe.timeout
                )
                if future.done():
                    future.result()
                    assert self._ready(), "Operation finished before expected workers entered"
                assert observed, "Initial scheduling wave was not observed"
                assert self.probe.maximum == self.probe.expected, (
                    f"Expected concurrency {self.probe.expected}, observed {self.probe.maximum}"
                )
        finally:
            self.probe.release.set()
            controller.join(timeout=WAIT_SECONDS)
            assert not controller.is_alive(), "Consumer did not finish after release"
        result = future.result(timeout=self.probe.timeout)
        self.probe.assert_complete(calls=self.calls)
        return result


def observe_executor_cap(
    monkeypatch: pytest.MonkeyPatch, module: Any, probe: ConcurrencyProbe, *, calls: int
) -> SchedulingObservation:
    """Domain helpers use this for executor-limited consumers."""
    observation = SchedulingObservation(probe, calls=calls)
    monkeypatch.setattr(
        module, "ThreadPoolExecutor", observation.executor_factory(module.ThreadPoolExecutor)
    )
    return observation


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
    error_types = () if after_error is None else (after_error,)

    def observed(*args: Any, **kwargs: Any) -> Any:
        if after_error is None and after_result is _ANY_RESULT:
            entered.set()
        try:
            result = original(*args, **kwargs)
        except error_types:
            entered.set()
            raise
        if after_result is not _ANY_RESULT and result == after_result:
            entered.set()
        return result

    monkeypatch.setattr(target, name, observed)
    return entered
