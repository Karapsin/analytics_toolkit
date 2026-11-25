from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.execution.cursor_cleanup import cursor_scope

from tests.sql._support.cursor_cleanup import (
    RecordingConnection,
    TerminalCursorError,
    arrange_connection,
    record_bulk_values,
)
from tests.sql._support.retries import execute_sql_module, retry_module
from tests.sql._support.retry_flow import arrange_transfer_attempt
from tests.sql._support.row_batches import load_sql_table_module

if TYPE_CHECKING:
    from typing import Any


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_execute_does_not_replay_committed_writes_when_cursor_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    connection = RecordingConnection(close_error=OSError("cursor transport cleanup failed"))
    opens = arrange_connection(monkeypatch, connection)
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    result = sql.execute(
        backend,
        "UPDATE counters SET value = value + 1; INSERT INTO audit VALUES (1)",
        gp_break_query=True,
        retry_cnt=3,
        timeout_increment=5,
    )

    assert result is None
    assert len(connection.executed) == 2
    assert opens == [backend]
    assert sleeps == []
    assert connection.commit_calls == (1 if backend == "gp" else 0)
    assert connection.cursors[0].close_calls == 1
    assert connection.cursors[0].exit_calls == (1 if backend == "gp" else 0)


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_execute_preserves_primary_sql_error_when_cursor_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    original = TerminalCursorError(backend)
    connection = RecordingConnection(
        execute_error=original,
        close_error=OSError("cursor cleanup must not hide the SQL error"),
    )
    opens = arrange_connection(monkeypatch, connection)
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    with pytest.raises(TerminalCursorError) as caught:
        sql.execute(backend, "UPDATE missing SET value = 1", retry_cnt=3, timeout_increment=5)

    assert caught.value is original
    assert opens == [backend]
    assert len(connection.executed) == 1
    assert connection.commit_calls == 0
    assert connection.cursors[0].close_calls == 1
    if backend == "gp":
        assert connection.cursors[0].exit_error is original
    assert sleeps == []


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("output_type", ["df", "scalar"])
def test_read_keeps_real_adapter_result_when_cursor_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    output_type: str,
) -> None:
    connection = RecordingConnection(close_error=OSError("cursor cleanup failed"))
    opens = arrange_connection(monkeypatch, connection)

    result = sql.read(
        backend, "SELECT 1 AS value", output_type=output_type, retry_cnt=3, timeout_increment=0
    )

    if output_type == "df":
        assert result.to_dict("list") == {"value": [1]}
    else:
        assert result == 1
    assert opens == [backend]
    assert len(connection.executed) == 1
    assert connection.cursors[0].close_calls == 1


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_execute_read_does_not_repeat_setup_after_cursor_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    connection = RecordingConnection(close_error=OSError("cursor cleanup failed"))
    opens = arrange_connection(monkeypatch, connection)

    result = sql.execute_read(
        backend,
        "UPDATE counters SET value = value + 1; SELECT 1 AS value",
        retry_cnt=3,
        timeout_increment=0,
    )

    assert result.to_dict("list") == {"value": [1]}
    assert opens == [backend]
    assert len(connection.executed) == 2
    assert connection.commit_calls == (1 if backend == "gp" else 0)
    assert connection.cursors[0].close_calls == 1


def test_execute_still_replays_generic_lost_commit_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = RecordingConnection(
        commit_error=ConnectionError("lost commit acknowledgement"),
        close_error=OSError("cursor cleanup failed"),
    )
    second = RecordingConnection(close_error=OSError("cursor cleanup failed"))
    connections = [first, second]
    opens: list[str] = []
    sleeps: list[float] = []

    def open_connection(key: str) -> RecordingConnection:
        opens.append(key)
        return connections.pop(0)

    monkeypatch.setattr(execute_sql_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    sql.execute("gp", "UPDATE counters SET value = value + 1", retry_cnt=2, timeout_increment=5)

    assert opens == ["gp", "gp"]
    assert len(first.executed) == len(second.executed) == 1
    assert first.commit_calls == second.commit_calls == 1
    assert first.cursors[0].close_calls == second.cursors[0].close_calls == 1
    assert sleeps == [5]


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("dataframe", [False, True])
def test_transfer_does_not_replay_real_batch_insert_after_cursor_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    dataframe: bool,
) -> None:
    connection = RecordingConnection(close_error=OSError("cursor cleanup failed"))
    target_key = "gp_sandbox" if backend == "gp" else "trino"
    attempts: list[int] = []
    gp_insert_module = importlib.import_module("analytics_toolkit.sql.backends.gp.insert")
    monkeypatch.setattr(gp_insert_module, "execute_values", record_bulk_values)

    def transfer_attempt(**_kwargs: Any) -> int:
        attempts.append(1)
        options = {
            "retry_fn": retry_module.run_with_retry,
            "retry_cnt": 3,
            "timeout_increment": 0,
        }
        if dataframe:
            return load_sql_table_module.insert_table_batch(
                backend,
                {"connection": connection},
                "sandbox.stage",
                pd.DataFrame({"id": [1]}),
                **options,
            )
        return load_sql_table_module.insert_rows_batch(
            backend,
            {"connection": connection},
            "sandbox.stage",
            ["id"],
            [(1,)],
            **options,
        )

    arrange_transfer_attempt(
        monkeypatch, transfer_attempt, target_backend=backend, target_key=target_key
    )

    assert sql.transfer("gp", target_key, timeout_increment=0, full_timeout_increment=0) == 1
    assert attempts == [1]
    assert len(connection.executed) == 1
    assert connection.commit_calls == (1 if backend == "gp" else 0)
    assert connection.cursors[0].close_calls == 1


@pytest.mark.parametrize(
    ("backend", "operation"),
    [
        ("gp", "command"),
        ("gp", "commands"),
        ("gp", "insert_query"),
        ("trino", "command"),
        ("trino", "commands"),
        ("trino", "insert_query"),
        ("trino", "materialize"),
    ],
)
def test_transfer_keeps_dbapi_adapter_success_when_cursor_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    operation: str,
) -> None:
    connection = RecordingConnection(close_error=OSError("cursor cleanup failed"))
    adapter = get_backend_adapter(backend)
    target_key = "gp_sandbox" if backend == "gp" else "trino"
    attempts: list[int] = []

    def transfer_attempt(**_kwargs: Any) -> int:
        attempts.append(1)
        if operation == "command":
            adapter.execute_command(connection, "UPDATE target SET value = value + 1")
        elif operation == "commands":
            adapter.execute_commands(connection, ["CREATE TABLE stage AS SELECT 1"])
        elif operation == "insert_query":
            assert (
                adapter.insert_from_query(
                    connection, "sandbox.target", "SELECT 1 AS id", {"id": "bigint"}
                )
                == 1
            )
        else:
            adapter.execute_materialization_command(connection, "CREATE TABLE stage AS SELECT 1")
        return 1

    arrange_transfer_attempt(
        monkeypatch, transfer_attempt, target_backend=backend, target_key=target_key
    )

    assert sql.transfer("gp", target_key, timeout_increment=0, full_timeout_increment=0) == 1
    assert attempts == [1]
    assert len(connection.executed) == 1
    assert connection.cursors[0].close_calls == 1


@pytest.mark.parametrize("managed", [False, True])
def test_cursor_scope_preserves_body_interrupt_and_failed_cleanup(managed: bool) -> None:
    original = KeyboardInterrupt()
    connection = RecordingConnection(close_error=OSError("cleanup failed"))

    with pytest.raises(KeyboardInterrupt) as caught, cursor_scope(
        connection, context_manager=managed
    ):
        raise original

    assert caught.value is original
    assert connection.cursors[0].close_calls == 1
    if managed:
        assert connection.cursors[0].exit_error is original


@pytest.mark.parametrize("managed", [False, True])
def test_cursor_scope_does_not_replace_acquisition_failure(managed: bool) -> None:
    original = ConnectionError("cursor acquisition failed")
    connection = RecordingConnection(cursor_error=original)

    with pytest.raises(ConnectionError) as caught, cursor_scope(
        connection, context_manager=managed
    ):
        pytest.fail("cursor acquisition should have failed")

    assert caught.value is original
    assert connection.cursors == []


def test_cursor_scope_does_not_close_a_manager_that_failed_to_enter() -> None:
    original = ConnectionError("cursor entry failed")
    connection = RecordingConnection(enter_error=original)

    with pytest.raises(ConnectionError) as caught, cursor_scope(connection, context_manager=True):
        pytest.fail("cursor entry should have failed")

    assert caught.value is original
    assert connection.cursors[0].enter_calls == 1
    assert connection.cursors[0].exit_calls == 0
    assert connection.cursors[0].close_calls == 0
