"""Nested synchronous batches lend their capacity lease while waiting."""

from __future__ import annotations

from threading import Barrier, Lock, Semaphore
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)
from analytics_toolkit.sql.orchestration import capacity
from analytics_toolkit.sql.orchestration.capacity import (
    HELD_CAPACITY,
    run_with_capacity,
    suspend_capacity,
)

from tests.sql._support.tasks import patch_task_operations


@pytest.mark.parametrize("cap", [1, 2])
def test_fully_saturated_nested_batches_share_capacity_across_loops(
    monkeypatch: pytest.MonkeyPatch, cap: int
) -> None:
    barrier = Barrier(cap, timeout=5)
    lock = Lock()
    active = maximum = calls = 0

    def read(**kwargs: Any) -> str:
        nonlocal active, maximum, calls
        with lock:
            active += 1
            maximum = max(active, maximum)
            calls += 1
        try:
            return kwargs["query"]
        finally:
            with lock:
                active -= 1

    patch_task_operations(monkeypatch, mode="async", read=read)

    def recurse(depth: int) -> Any:
        if depth == 0:
            return sql.async_sql(
                [
                    {"name": str(i), "type": "read", "db_key": "gp", "query": str(i)}
                    for i in range(3)
                ],
                concurrency=3,
                soft_concurrency_cap=1,
            )
        return sql.async_sql(
            [
                {
                    "name": "nested",
                    "type": "custom_sql_pipeline",
                    "steps": [lambda _ctx: recurse(depth - 1)],
                }
            ],
            concurrency=1,
        )

    def parent(_context: Any) -> Any:
        barrier.wait()
        return recurse(2)

    result = sql.async_sql(
        [{"name": str(i), "type": "custom_sql_pipeline", "steps": [parent]} for i in range(cap)],
        concurrency=cap,
        soft_concurrency_cap=cap,
    )
    assert len(result) == cap
    assert calls == 3 * cap
    assert 1 <= maximum <= cap
    assert active == 0
    assert HELD_CAPACITY.get() == ()


def test_nested_failure_restores_the_parent_capacity_lease() -> None:
    semaphore = Semaphore(1)

    def parent() -> None:
        assert HELD_CAPACITY.get() == (semaphore,)

        def child() -> None:
            with suspend_capacity():
                assert HELD_CAPACITY.get() == ()
                run_with_capacity((semaphore,), lambda: (_ for _ in ()).throw(ValueError("nested")))

        with pytest.raises(ValueError, match="nested"):
            child()
        assert HELD_CAPACITY.get() == (semaphore,)
        assert not semaphore.acquire(blocking=False)

    run_with_capacity((semaphore,), parent)
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_capacity_wait_rechecks_cancellation_and_restores_acquired_permits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class DelayedSemaphore:
        def __init__(self) -> None:
            self.calls = self.released = 0

        def acquire(self, *, timeout: float) -> bool:
            assert timeout == 0.1
            self.calls += 1
            return self.calls > 1

        def release(self) -> None:
            self.released += 1

    semaphore = DelayedSemaphore()
    assert run_with_capacity((semaphore,), lambda: 7) == 7
    assert semaphore.calls == 2
    assert semaphore.released == 1

    scope = SqlCancellationScope()
    with activate_cancellation_scope(scope):
        scope.request_cancel()
        with pytest.raises(AsyncSqlCancelled):
            run_with_capacity((semaphore,), lambda: None)
    assert semaphore.released == 2
    assert capacity.HELD_CAPACITY.get() == ()
