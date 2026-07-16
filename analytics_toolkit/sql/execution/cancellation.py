from __future__ import annotations

import contextvars
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from threading import Event, Lock
from typing import Any, Iterator
from uuid import uuid4

from analytics_toolkit.general import time_print

_INTERRUPT_CLEANUP_TIMEOUT_SECONDS = 10.0
_CANCELLATION_POLL_SECONDS = 0.1
_INTERRUPTED_MESSAGE = "async_sql was interrupted"


class AsyncSqlCancelled(BaseException):
    """Stop work that outlived an interrupted ``async_sql`` awaiter."""


class SqlCancellationScope:
    def __init__(self, parent: SqlCancellationScope | None = None) -> None:
        self.parent = parent
        self.marker = f"async_sql={uuid4().hex}"
        self._cancelled = Event()
        self._lock = Lock()
        self._aliases: set[str] = set()
        self._executors: set[ThreadPoolExecutor] = set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set() or bool(self.parent is not None and self.parent.cancelled)

    @property
    def markers(self) -> tuple[str, ...]:
        parent_markers = () if self.parent is None else self.parent.markers
        return (*parent_markers, self.marker)

    @property
    def aliases(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._aliases))

    def register_alias(self, connection_key: str) -> None:
        with self._lock:
            self._aliases.add(connection_key)
        if self.parent is not None:
            self.parent.register_alias(connection_key)

    def register_executor(self, executor: ThreadPoolExecutor) -> None:
        with self._lock:
            self._executors.add(executor)
        if self.cancelled:
            shutdown_executor(executor, wait=False, cancel_futures=True)

    def unregister_executor(self, executor: ThreadPoolExecutor) -> None:
        with self._lock:
            self._executors.discard(executor)

    def request_cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            executors = tuple(self._executors)
        for executor in executors:
            shutdown_executor(executor, wait=False, cancel_futures=True)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise AsyncSqlCancelled(_INTERRUPTED_MESSAGE)

    def wait(self, timeout: float) -> None:
        self._cancelled.wait(timeout)


_CANCELLATION_SCOPE: contextvars.ContextVar[SqlCancellationScope | None] = contextvars.ContextVar(
    "analytics_toolkit_sql_cancellation", default=None
)


def current_cancellation_scope() -> SqlCancellationScope | None:
    return _CANCELLATION_SCOPE.get()


@contextmanager
def activate_cancellation_scope(
    scope: SqlCancellationScope | None,
) -> Iterator[None]:
    token = _CANCELLATION_SCOPE.set(scope)
    try:
        yield
    finally:
        _CANCELLATION_SCOPE.reset(token)


def cancellation_query_comments() -> tuple[str, ...]:
    scope = current_cancellation_scope()
    if scope is None:
        return ()
    return tuple(f"/* analytics_toolkit {marker} */" for marker in scope.markers)


def register_connection_alias(connection_key: str) -> None:
    scope = current_cancellation_scope()
    if scope is not None:
        scope.register_alias(connection_key)


def raise_if_cancelled() -> None:
    scope = current_cancellation_scope()
    if scope is not None:
        scope.raise_if_cancelled()


def wait_or_raise_if_cancelled(timeout: float) -> None:
    scope = current_cancellation_scope()
    if scope is None:
        time.sleep(timeout)
        return
    deadline = time.monotonic() + timeout
    while True:
        scope.raise_if_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        scope.wait(min(_CANCELLATION_POLL_SECONDS, remaining))


def cancel_scope_queries(
    scope: SqlCancellationScope,
    *,
    timeout: float = _INTERRUPT_CLEANUP_TIMEOUT_SECONDS,
) -> None:
    aliases = scope.aliases
    if not aliases:
        return

    deadline = time.monotonic() + timeout
    executor = ThreadPoolExecutor(
        max_workers=len(aliases),
        thread_name_prefix="async-sql-cancel",
    )
    futures: dict[Future[tuple[str, tuple[int | str, ...], str | None]], str] = {
        executor.submit(_cancel_alias_until_clear, alias, scope.marker, deadline): alias
        for alias in aliases
    }
    done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    shutdown_executor(executor, wait=False, cancel_futures=True)

    for future in done:
        alias, remaining_ids, error = future.result()
        if error is not None:
            _log_cancellation_warning(alias, error)
        elif remaining_ids:
            _log_cancellation_warning(
                alias,
                f"queries still active after cancellation: {list(remaining_ids)!r}",
            )
    for future in pending:
        _log_cancellation_warning(
            futures[future],
            f"cancellation did not finish within {timeout:g} seconds",
        )


def _cancel_alias_until_clear(
    connection_key: str,
    marker: str,
    deadline: float,
) -> tuple[str, tuple[int | str, ...], str | None]:
    with activate_cancellation_scope(None):
        try:
            while time.monotonic() < deadline:
                query_ids = _matching_query_ids(connection_key, marker)
                if not query_ids:
                    return connection_key, (), None
                _cancel_query_ids(connection_key, query_ids)
                remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(_CANCELLATION_POLL_SECONDS, remaining))
            return connection_key, tuple(_matching_query_ids(connection_key, marker)), None
        except Exception as exc:  # noqa: BLE001 - cleanup must report driver failures.
            return connection_key, (), f"{type(exc).__name__}: {exc}"


def _matching_query_ids(connection_key: str, marker: str) -> list[int | str]:
    from analytics_toolkit.sql.metadata.show_queries import show_queries  # noqa: PLC0415

    active = show_queries(
        connection_key,
        state="active",
        retry_cnt=1,
        timeout_increment=0,
    )
    if active.empty or "query" not in active or "query_id" not in active:
        return []
    marker_comment = f"/* analytics_toolkit {marker} */"
    matches = active[active["query"].astype(str).str.contains(marker_comment, regex=False)]
    return list(matches["query_id"])


def _cancel_query_ids(connection_key: str, query_ids: list[int | str]) -> None:
    from analytics_toolkit.sql.dml.io.cancel_queries import cancel_queries  # noqa: PLC0415

    cancel_queries(
        connection_key,
        query_ids,
        concurrency=min(4, len(query_ids)),
        retry_cnt=1,
        timeout_increment=0,
    )


def _log_cancellation_warning(connection_key: str, detail: str) -> None:
    time_print(
        f"Could not confirm interrupted async_sql queries were cancelled: {detail}",
        level="warning",
        connection=connection_key,
        operation="async_sql",
        phase="cancel",
    )


def shutdown_executor(
    executor: ThreadPoolExecutor,
    *,
    wait: bool,
    cancel_futures: bool,
) -> None:
    try:
        shutdown: Any = executor.shutdown
        shutdown(wait=wait, cancel_futures=cancel_futures)
    except TypeError:  # Python 3.8 has no cancel_futures argument.
        executor.shutdown(wait=wait)


__all__ = [
    "AsyncSqlCancelled",
    "SqlCancellationScope",
    "activate_cancellation_scope",
    "cancel_scope_queries",
    "cancellation_query_comments",
    "current_cancellation_scope",
    "raise_if_cancelled",
    "register_connection_alias",
    "shutdown_executor",
    "wait_or_raise_if_cancelled",
]
