"""Thread-safe capacity leases shared by synchronous and async batch workers."""

from __future__ import annotations

import contextvars
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any, Iterator, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Semaphore

from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

HELD_CAPACITY: contextvars.ContextVar[tuple[Semaphore, ...]] = contextvars.ContextVar(
    "analytics_toolkit_sql_held_capacity", default=()
)
CONCURRENCY_STATE: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "analytics_toolkit_sql_concurrency", default=None
)
_StateT = TypeVar("_StateT")


def build_concurrency_state(  # noqa: PLR0913 -- keep independent legacy constructor patch points.
    *,
    concurrency: int,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
    default_hard_cap: int,
    state_factory: Callable[..., _StateT],
    semaphore_factory: Callable[[int], Semaphore],
) -> _StateT:
    """Resolve inherited limits for both synchronous batch entrypoints."""
    active = CONCURRENCY_STATE.get()
    if active is None:
        hard_cap = hard_concurrency_cap
        soft_cap = concurrency if soft_concurrency_cap is None else soft_concurrency_cap
        semaphores = (semaphore_factory(soft_cap),)
        effective_concurrency = concurrency
    else:
        hard_cap = (
            active.hard_cap if hard_concurrency_cap == default_hard_cap else hard_concurrency_cap
        )
        soft_cap = active.soft_cap
        semaphores = active.semaphores
        if soft_concurrency_cap is not None and soft_concurrency_cap < soft_cap:
            soft_cap = soft_concurrency_cap
            semaphores = (*semaphores, semaphore_factory(soft_cap))
        effective_concurrency = active.effective_concurrency * concurrency

    actual_worker_ceiling = min(effective_concurrency, soft_cap)
    if actual_worker_ceiling > hard_cap:
        message = (
            "effective concurrency exceeds hard_concurrency_cap "
            f"({actual_worker_ceiling} > {hard_cap}). Reduce concurrency, set "
            "soft_concurrency_cap at or below hard_concurrency_cap, or increase "
            "hard_concurrency_cap."
        )
        raise ValueError(message)
    return state_factory(
        effective_concurrency=effective_concurrency,
        hard_cap=hard_cap,
        soft_cap=soft_cap,
        semaphores=semaphores,
    )


def run_with_capacity(semaphores: tuple[Semaphore, ...], func: Any, *args: Any) -> Any:
    """Acquire in the worker, so permits never belong to a foreign event loop."""
    with ExitStack() as stack:
        acquired: list[Semaphore] = []
        for semaphore in reversed(semaphores):
            while not semaphore.acquire(timeout=0.1):
                raise_if_cancelled()
            acquired.append(semaphore)
            stack.callback(semaphore.release)
        token = HELD_CAPACITY.set((*HELD_CAPACITY.get(), *acquired))
        try:
            raise_if_cancelled()
            return func(*args)
        finally:
            HELD_CAPACITY.reset(token)


@contextmanager
def suspend_capacity() -> Iterator[None]:
    """Lend a waiting parent's permits to children, restoring its lease on exit."""
    held = HELD_CAPACITY.get()
    token = HELD_CAPACITY.set(())
    for semaphore in held:
        semaphore.release()
    try:
        yield
    finally:
        # Reacquire in the same order used by the original lease. Restoration is
        # mandatory even on cancellation: its enclosing lease releases these.
        for semaphore in held:
            semaphore.acquire()
        HELD_CAPACITY.reset(token)
