from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.sql._support.row_batches import retry_module
from tests.sql._support.staged_keyed import (
    BoundedConnectionManager,
    SimpleNamespace,
    staged_keyed_io,
)

if TYPE_CHECKING:
    import pytest


class RetryConnection:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class KeyedRetryProbe:
    def __init__(self, replacement_error: Exception | None = None) -> None:
        self.replacement_error = replacement_error
        self.events: list[str] = []
        self.open_calls = 0
        self.read_calls = 0
        self.connections: list[RetryConnection] = []

    def open_connection(self, _key: str) -> RetryConnection:
        self.open_calls += 1
        if self.open_calls > 1:
            self.events.append("replacement")
            if self.replacement_error is not None:
                raise self.replacement_error
        connection = RetryConnection()
        self.connections.append(connection)
        return connection

    def read(self, *_args: Any, **_kwargs: Any) -> Any:
        self.read_calls += 1
        if self.read_calls == 1:
            self.events.append("failed_read")
            message = "source connection was lost"
            raise ConnectionError(message)
        self.events.append("query")
        return SimpleNamespace(column_names=["id"], columns=[[1]])

    def rollback(self, _connection: Any) -> None:
        self.events.append("rollback")

    def wait(self, _seconds: float) -> None:
        self.events.append("wait")


def arrange_keyed_retry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    replacement_error: Exception | None = None,
) -> tuple[KeyedRetryProbe, BoundedConnectionManager]:
    probe = KeyedRetryProbe(replacement_error)
    monkeypatch.setattr(staged_keyed_io, "_read_backend", probe.read)
    monkeypatch.setattr(staged_keyed_io, "rollback_quietly", probe.rollback)
    monkeypatch.setattr(retry_module.time, "sleep", probe.wait)
    manager = BoundedConnectionManager(
        "source", 1, role="source retry ordering", open_connection=probe.open_connection
    )
    return probe, manager
