"""Thread-safe capacity leases shared by synchronous and async batch workers."""

from __future__ import annotations

import contextvars
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from threading import Semaphore

from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

HELD_CAPACITY: contextvars.ContextVar[tuple[Semaphore, ...]] = contextvars.ContextVar(
    "analytics_toolkit_sql_held_capacity", default=()
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
