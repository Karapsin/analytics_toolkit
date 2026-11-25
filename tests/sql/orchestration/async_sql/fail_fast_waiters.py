"""A failed batch stops SQL workers already waiting for shared capacity."""

from __future__ import annotations

import asyncio
import importlib
from concurrent.futures import Future
from threading import Event, Semaphore, Thread
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.cancellation import current_cancellation_scope

from tests._support.concurrency import WAIT_SECONDS
from tests.sql._support.tasks import patch_task_operations


def _observed_semaphore(waiting_scopes: list[Any], waiter_started: Event) -> type[Semaphore]:
    class ObservedSemaphore(Semaphore):
        def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
            if not blocking:
                return super().acquire(blocking=False)
            if super().acquire(blocking=False):
                return True
            waiting_scopes.append(current_cancellation_scope())
            waiter_started.set()
            return super().acquire(timeout=timeout)

    return ObservedSemaphore


def test_failed_nested_batch_cancels_waiting_writes_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("analytics_toolkit.sql.orchestration.async_sql")
    holder_started, release_holder, waiter_started, cleanup_started = (Event() for _ in range(4))
    waiting_scopes: list[Any] = []
    writes: list[str] = []
    failure = ValueError("planned sibling failure")

    original_gather = asyncio.gather

    async def observed_gather(*tasks: Any, **kwargs: Any) -> Any:
        if kwargs.get("return_exceptions"):
            cleanup_started.set()
        return await original_gather(*tasks, **kwargs)

    monkeypatch.setattr(module, "Semaphore", _observed_semaphore(waiting_scopes, waiter_started))
    monkeypatch.setattr(asyncio, "gather", observed_gather)

    def holder(**kwargs: Any) -> str:
        holder_started.set()
        assert release_holder.wait(WAIT_SECONDS)
        return "holder"

    patch_task_operations(
        monkeypatch, mode="async", read=holder, execute=lambda **kwargs: writes.append("write")
    )

    async def fail(_context: Any) -> None:
        assert await asyncio.to_thread(waiter_started.wait, WAIT_SECONDS)
        raise failure

    async def child(_context: Any) -> Any:
        assert holder_started.wait(WAIT_SECONDS)
        return sql.async_sql(
            [
                {"name": "waiting write", "type": "execute", "db_key": "gp", "query": "INSERT"},
                {"name": "failure", "type": "custom_sql_pipeline", "steps": [fail]},
            ],
            concurrency=2,
        )

    outcome: Future[Any] = Future()

    def run() -> None:
        try:
            outcome.set_result(
                sql.async_sql(
                    [
                        {"name": "holder", "type": "read", "db_key": "gp", "query": "SELECT 1"},
                        {"name": "nested", "type": "custom_sql_pipeline", "steps": [child]},
                    ],
                    concurrency=2,
                    soft_concurrency_cap=1,
                    hard_concurrency_cap=1,
                )
            )
        except BaseException as exc:  # noqa: BLE001 -- propagate the original worker outcome.
            outcome.set_exception(exc)

    controller = Thread(target=run, daemon=True)
    controller.start()
    try:
        assert cleanup_started.wait(WAIT_SECONDS)
        assert waiting_scopes
        assert waiting_scopes[0] is not None
        assert waiting_scopes[0].cancelled
    finally:
        release_holder.set()
        controller.join(WAIT_SECONDS)
        assert not controller.is_alive()
    with pytest.raises(ValueError, match="planned sibling failure") as caught:
        outcome.result(timeout=WAIT_SECONDS)
    assert caught.value is failure
    assert writes == []
