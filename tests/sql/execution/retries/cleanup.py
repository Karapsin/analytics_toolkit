from __future__ import annotations

from analytics_toolkit import sql
from analytics_toolkit.sql.execution.retry_cleanup import cleanup_preserving_error, mark_sql_error

from tests.sql._support.retries import (
    CloseFailureConnection,
    FakeConnection,
    FakeUndefinedTableError,
    operation_runner_module,
    pd,
    pytest,
    read_sql_module,
    retry_module,
)


def test_cleanup_preserves_errors_on_runtimes_without_exception_notes() -> None:
    class LegacyError(Exception):
        add_note = None

    error = LegacyError("primary failure")

    def fail_cleanup() -> None:
        message = "cleanup details"
        raise OSError(message)

    cleanup_preserving_error(fail_cleanup, error=error, phase="close")

    assert getattr(error, "__notes__", []) == ["SQL close also failed: OSError"]


def test_source_error_origin_preserves_interrupts_without_classifying_them() -> None:
    error = KeyboardInterrupt()

    mark_sql_error(error, "gp")

    assert getattr(error, "analytics_toolkit_sql_backend", None) == "gp"
    assert not hasattr(error, "analytics_toolkit_sql_retry_safe")


def test_operation_runner_uses_custom_connection_cleanup() -> None:
    connection = FakeConnection("custom")
    cleaned: list[dict[str, FakeConnection]] = []

    result = operation_runner_module.run_connection_operation(
        operation_name="custom cleanup",
        connection_key="gp",
        backend="gp",
        retry_cnt=1,
        timeout_increment=0,
        open_connection=lambda _key: connection,
        operation=lambda connection_ref, _attempt: connection_ref["connection"].name,
        context_factory=lambda attempt: operation_runner_module.SqlOperationContext(
            operation="custom cleanup",
            retry_attempt=attempt,
        ),
        cleanup=cleaned.append,
    )

    assert result == "custom"
    assert cleaned == [{"connection": connection}]
    assert connection.close_calls == 0


def test_run_with_fresh_connection_returns_after_final_cleanup() -> None:
    connection = FakeConnection("fresh")
    seen_refs: list[dict[str, FakeConnection]] = []

    result = retry_module.run_with_fresh_connection(
        "warehouse",
        "target",
        lambda connection_ref: seen_refs.append(connection_ref) or "ok",
        open_connection=lambda connection_key: connection,
    )

    assert result == "ok"
    assert seen_refs == [{"connection": connection}]
    assert connection.close_calls == 1


def test_read_preserves_terminal_error_when_rollback_and_close_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = CloseFailureConnection("failed")
    original_error = FakeUndefinedTableError("missing relation")
    attempts: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def fail_read(conn: FakeConnection, *_args: object, **_kwargs: object) -> pd.DataFrame:
        attempts.append(conn.name)
        raise original_error

    def fail_rollback(conn: FakeConnection) -> None:
        conn.rollback_calls += 1
        message = "rollback credential-secret"
        raise RuntimeError(message)

    adapter = read_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(adapter, "read_dataframe", fail_read)
    monkeypatch.setattr(adapter, "rollback_quietly", fail_rollback)

    with pytest.raises(FakeUndefinedTableError) as caught:
        sql.read("gp", "SELECT * FROM missing", retry_cnt=3, timeout_increment=5)

    assert caught.value is original_error
    assert attempts == ["failed"]
    assert connection.rollback_calls == 1
    assert connection.close_calls == 1
    assert sleeps == []
    output = capsys.readouterr().out
    assert "Connection rollback failed after SQL outcome was established: RuntimeError" in output
    assert "Connection close failed after SQL outcome was established: RuntimeError" in output
    assert "credential-secret" not in output


def test_read_returns_established_result_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = CloseFailureConnection("success")
    expected = pd.DataFrame({"value": [1]})
    attempts: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def read(conn: FakeConnection, *_args: object, **_kwargs: object) -> pd.DataFrame:
        attempts.append(conn.name)
        return expected

    monkeypatch.setattr(read_sql_module.get_backend_adapter("gp"), "read_dataframe", read)

    result = sql.read("gp", "SELECT 1 AS value", retry_cnt=3, timeout_increment=5)

    assert result.equals(expected)
    assert attempts == ["success"]
    assert connection.close_calls == 1
    assert connection.rollback_calls == 0
    assert sleeps == []


def test_read_retries_original_transient_failure_despite_failed_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = CloseFailureConnection("first")
    second = FakeConnection("second")
    connections = [first, second]
    attempts: list[str] = []
    sleeps: list[float] = []
    expected = pd.DataFrame({"value": [2]})
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connections.pop(0))
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def read(conn: FakeConnection, *_args: object, **_kwargs: object) -> pd.DataFrame:
        attempts.append(conn.name)
        if conn is first:
            message = "temporary connection failure"
            raise ConnectionError(message)
        return expected

    monkeypatch.setattr(read_sql_module.get_backend_adapter("gp"), "read_dataframe", read)

    result = sql.read("gp", "SELECT 2 AS value", retry_cnt=2, timeout_increment=5)

    assert result.equals(expected)
    assert attempts == ["first", "second"]
    assert first.rollback_calls == 1
    assert first.close_calls == 1
    assert second.close_calls == 1
    assert sleeps == [5]


def test_operation_runner_does_not_replay_success_for_custom_cleanup_failure() -> None:
    connection = FakeConnection("custom")
    attempts: list[int] = []

    def cleanup(_connection_ref: object) -> None:
        message = "custom cleanup failure"
        raise RuntimeError(message)

    result = operation_runner_module.run_connection_operation(
        operation_name="custom cleanup",
        connection_key="gp",
        backend="gp",
        retry_cnt=3,
        timeout_increment=0,
        open_connection=lambda _key: connection,
        operation=lambda _ref, attempt: attempts.append(attempt) or "ok",
        context_factory=lambda attempt: operation_runner_module.SqlOperationContext(
            operation="custom cleanup", retry_attempt=attempt
        ),
        cleanup=cleanup,
    )

    assert result == "ok"
    assert attempts == [1]
