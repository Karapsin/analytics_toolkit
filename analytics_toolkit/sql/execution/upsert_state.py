"""Retain backend certificates throughout one prepared upsert and its retries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

    from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint


@dataclass(frozen=True)
class _UpsertState:
    connection: object
    certificates: dict[str, Any]


_STATE: ContextVar[_UpsertState | None] = ContextVar("sql_upsert_backend_state", default=None)
_Function = TypeVar("_Function", bound=Callable[..., Any])


@contextmanager
def bind_upsert_state(connection: object, checkpoint: UpsertCheckpoint | None) -> Iterator[None]:
    certificates = {} if checkpoint is None else checkpoint.backend_state
    token = _STATE.set(_UpsertState(connection, certificates))
    try:
        yield
    finally:
        _STATE.reset(token)


def current_upsert_state(connection: object | None = None) -> dict[str, Any] | None:
    state = _STATE.get()
    return (
        state.certificates
        if state is not None and (connection is None or state.connection is connection)
        else None
    )


def retain_upsert_state(function: _Function) -> _Function:
    @wraps(function)
    def bound(*args: Any, **kwargs: Any) -> Any:
        connection = kwargs.get("connection", args[1] if len(args) > 1 else None)
        with bind_upsert_state(connection, kwargs.get("checkpoint")):
            return function(*args, **kwargs)

    return cast("_Function", bound)
