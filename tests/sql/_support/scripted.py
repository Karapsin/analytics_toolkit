"""Bounded DBAPI scripts that fail on unexpected SQL, parameters or connections."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.sql._support.assertions import assert_sql_equal
from tests.sql._support.fakes import FakeDbapiConnection, FakeDbapiCursor

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class SqlResponse:
    sql: str
    rows: tuple[tuple[Any, ...], ...] = ()
    columns: tuple[str, ...] = ()
    params: tuple[Any, ...] | None = None
    error: Exception | None = None


class ScriptedCursor(FakeDbapiCursor):
    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        connection = self.connection
        assert isinstance(connection, ScriptedConnection)
        assert connection.responses, f"Unexpected SQL after script exhausted: {sql}"
        response = connection.responses[0]
        assert_sql_equal(sql, response.sql, dialect=connection.dialect)
        assert params == response.params, f"Unexpected SQL parameters: {params!r}"
        connection.responses.popleft()
        super().execute(sql, params)
        self.description = [(column,) for column in response.columns]
        self._rows = list(response.rows)
        if response.error is not None:
            raise response.error


class ScriptedConnection(FakeDbapiConnection):
    def __init__(self, responses: Sequence[SqlResponse], *, dialect: str) -> None:
        super().__init__()
        self.responses = deque(responses)
        self.dialect = dialect
        self.cursors: list[ScriptedCursor] = []

    def cursor(self) -> ScriptedCursor:
        cursor = ScriptedCursor(self)
        self.cursors.append(cursor)
        return cursor

    def assert_complete(self) -> None:
        assert not self.responses, (
            f"Unexecuted SQL: {[response.sql for response in self.responses]}"
        )


class ConnectionSequence:
    """A connection opener with an explicit, finite retry sequence."""

    def __init__(self, connections: Sequence[Any]) -> None:
        self.remaining = deque(connections)
        self.calls: list[str] = []

    def __call__(self, key: str) -> Any:
        self.calls.append(key)
        assert self.remaining, f"Unexpected connection attempt for {key}"
        return self.remaining.popleft()

    def assert_complete(self) -> None:
        assert not self.remaining, f"Unused connections: {len(self.remaining)}"
