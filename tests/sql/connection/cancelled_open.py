from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)

from tests.sql._support.connection_scope import FailingCloseConnection, connection_module


@pytest.mark.parametrize("per_host", [False, True])
def test_cancelled_connection_open_preserves_cancellation_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    per_host: bool,
) -> None:
    backend = "ch" if per_host else "gp"
    scope = SqlCancellationScope()
    original_error = AsyncSqlCancelled("cancelled while opening")
    connection = FailingCloseConnection()
    opened: list[str] = []

    def open_connection(config: Any, **_kwargs: Any) -> FailingCloseConnection:
        opened.append(config.connection_key)
        scope.request_cancel()
        return connection

    def check_cancellation() -> None:
        if scope.cancelled:
            raise original_error

    monkeypatch.setattr(scope, "raise_if_cancelled", check_cancellation)
    monkeypatch.setattr(connection_module.get_backend(backend), "open_connection", open_connection)

    def open_selected_connection() -> Any:
        if per_host:
            return connection_module.get_ch_connection_for_host("ch", "host1")
        return sql.get_sql_connection("gp")

    with activate_cancellation_scope(scope), pytest.raises(AsyncSqlCancelled) as caught:
        open_selected_connection()

    assert caught.value is original_error
    assert opened == [backend]
    assert connection.close_calls == 1
