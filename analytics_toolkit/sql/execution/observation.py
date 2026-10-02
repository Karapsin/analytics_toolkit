"""Opt-in observation of SQL submissions; ordinary connections remain untouched."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager
    from types import TracebackType


class SqlObserver(Protocol):
    def submission(self, sql: str) -> AbstractContextManager[None]: ...


_OBSERVER: ContextVar[SqlObserver | None] = ContextVar("sql_observer", default=None)


@contextmanager
def observe_sql(observer: SqlObserver) -> Iterator[None]:
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def observed_connection(connection: Any) -> Any:
    observer = _OBSERVER.get()
    if observer is None or getattr(connection, "_sql_observed", False) is True:
        return connection
    return _ObservedConnection(connection, observer)


class _ObservedConnection:
    _sql_observed = True

    def __init__(self, target: Any, observer: SqlObserver) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_observer", observer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target, name, value)

    def __enter__(self) -> _ObservedConnection:  # noqa: PYI034 -- driver context may return another object.
        return _ObservedConnection(self._target.__enter__(), self._observer)

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        return self._target.__exit__(kind, error, traceback)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._target)

    def cursor(self, *args: Any, **kwargs: Any) -> _ObservedConnection:
        return _ObservedConnection(self._target.cursor(*args, **kwargs), self._observer)

    def _run(self, method: str, *args: Any, **kwargs: Any) -> Any:
        statement = args[0] if args else kwargs.get("query", kwargs.get("sql", ""))
        with self._observer.submission(str(statement)):
            result = getattr(self._target, method)(*args, **kwargs)
        return self if result is self._target else result

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("execute", *args, **kwargs)

    def query(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("query", *args, **kwargs)

    def raw_query(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("raw_query", *args, **kwargs)

    def command(self, *args: Any, **kwargs: Any) -> Any:
        return self._run("command", *args, **kwargs)
