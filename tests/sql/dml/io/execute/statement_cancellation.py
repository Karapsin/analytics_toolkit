"""Every submitted statement stays visible to scoped cancellation."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.cancellation import (
    SqlCancellationScope,
    activate_cancellation_scope,
    cancellation_query_comments,
)
from clickhouse_driver.errors import ServerException

from tests.sql.dml.io.execute.outcomes import CountConnection, CountCursor, execute_module


class DrainedCursor(CountCursor):
    def fetchmany(self, size: int = 1000) -> list[Any]:
        return self.fetchall()


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("operation", ["execute", "execute_insert"])
def test_public_multistatement_writes_label_every_statement_without_explicit_label(
    monkeypatch: pytest.MonkeyPatch, backend: str, operation: str
) -> None:
    connection = CountConnection(backend, 1)
    connection.cursor_obj = DrainedCursor(connection)
    monkeypatch.setattr(execute_module, "get_sql_connection", lambda _key: connection)
    with activate_cancellation_scope(SqlCancellationScope()):
        markers = cancellation_query_comments()
        if operation == "execute":
            sql.execute(
                backend,
                "CREATE TEMPORARY TABLE setup (id INTEGER); INSERT INTO public.target SELECT 1",
                gp_break_query=True,
                retry_cnt=1,
            )
        else:
            sql.execute_insert(
                backend,
                "public.target",
                "CREATE TEMPORARY TABLE setup (id INTEGER); SELECT 1",
                gp_break_query=True,
                retry_cnt=1,
            )
    assert len(connection.queries) == 2
    assert all(marker in statement for marker in markers for statement in connection.queries)
    assert connection.closed == 1


def test_public_clickhouse_unknown_database_does_not_replay_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = ServerException(
        message="Database missing does not exist. (UNKNOWN_DATABASE)", code=81
    )
    opened: list[CountConnection] = []

    class MissingDatabaseConnection(CountConnection):
        def command(self, query: str) -> dict[str, Any]:
            result = super().command(query)
            if "missing.events" in query:
                raise failure
            return result

    def open_connection(_key: str) -> CountConnection:
        connection = MissingDatabaseConnection("ch", 0)
        opened.append(connection)
        return connection

    monkeypatch.setattr(execute_module, "get_sql_connection", open_connection)
    with pytest.raises(ServerException) as caught:
        sql.execute(
            "ch",
            "CREATE TEMPORARY TABLE setup (id UInt8); INSERT INTO missing.events SELECT 1",
            retry_cnt=3,
            timeout_increment=0,
        )
    assert caught.value is failure
    assert len(opened) == 1
    assert len(opened[0].queries) == 2
    assert opened[0].closed == 1
