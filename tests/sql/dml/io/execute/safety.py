from __future__ import annotations

# ruff: noqa: EM101, PYI034, PYI036, TRY003
import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.dml.io.execute_safety import (
    ExecuteAttemptState,
    SqlBatchExecutionError,
    SqlBatchItemResult,
    TrackingConnection,
    is_read_only_sql,
)

execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
sql_module = importlib.import_module("analytics_toolkit.sql")


class _Connection:
    def __init__(self, *, commit_error: Exception | None = None) -> None:
        self.commit_error = commit_error
        self.executed: list[str] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, query: str) -> None:
        self.connection.executed.append(query)


def test_safe_mutation_does_not_replay_ambiguous_nontransactional_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    attempts: list[str] = []
    monkeypatch.setattr(execute_sql_module, "get_sql_connection", lambda _key: connection)
    adapter = execute_sql_module.get_backend_adapter("trino")

    def fail(_connection: Any, query: str, **_kwargs: Any) -> None:
        attempts.append(query)
        raise OSError("connection reset after submission")

    monkeypatch.setattr(adapter, "execute_sql", fail)

    with pytest.raises(sql_module.AmbiguousSqlMutationError) as caught:
        sql_module.execute(
            "trino",
            "INSERT INTO sandbox.events VALUES (1)",
            retry_cnt=3,
            timeout_increment=0,
        )

    assert len(attempts) == 1
    assert isinstance(caught.value.original_error, OSError)


def test_safe_read_retries_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = [_Connection(), _Connection()]
    attempts: list[str] = []
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda _key: connections[len(attempts)],
    )
    adapter = execute_sql_module.get_backend_adapter("trino")

    def execute(_connection: Any, query: str, **_kwargs: Any) -> None:
        attempts.append(query)
        if len(attempts) == 1:
            raise OSError("temporary read failure")

    monkeypatch.setattr(adapter, "execute_sql", execute)

    sql_module.execute("trino", "SELECT 1", retry_cnt=2, timeout_increment=0)

    assert attempts == ["SELECT 1", "SELECT 1"]


def test_safe_greenplum_commit_failure_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(commit_error=OSError("commit acknowledgement lost"))
    monkeypatch.setattr(execute_sql_module, "get_sql_connection", lambda _key: connection)

    with pytest.raises(sql_module.AmbiguousSqlMutationError):
        sql_module.execute(
            "gp",
            "INSERT INTO sandbox.events VALUES (1)",
            retry_cnt=3,
            timeout_increment=0,
        )

    assert len(connection.executed) == 1
    assert connection.commit_calls == 1


def test_always_policy_retains_explicit_legacy_mutation_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = [_Connection(), _Connection()]
    attempts: list[str] = []
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda _key: connections[len(attempts)],
    )
    adapter = execute_sql_module.get_backend_adapter("trino")

    def execute(_connection: Any, query: str, **_kwargs: Any) -> None:
        attempts.append(query)
        if len(attempts) == 1:
            raise OSError("temporary mutation failure")

    monkeypatch.setattr(adapter, "execute_sql", execute)

    sql_module.execute(
        "trino",
        "INSERT INTO sandbox.events VALUES (1)",
        retry_policy="always",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert len(attempts) == 2


def test_retry_policy_is_validated_before_connection_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execute_sql_module,
        "get_connection_config",
        lambda _key: pytest.fail("connection config should not be read"),
    )

    with pytest.raises(ValueError, match="retry_policy"):
        sql_module.execute("gp", "SELECT 1", retry_policy="sometimes")


def test_batch_error_exposes_every_outcome_group() -> None:
    items = [
        SqlBatchItemResult(3, "cancel", "cancelled", 0),
        SqlBatchItemResult(0, "ok", "success", 1, result="done"),
        SqlBatchItemResult(2, "maybe", "ambiguous", 1),
        SqlBatchItemResult(1, "retry", "failed", 2),
    ]

    error = SqlBatchExecutionError("batch", items)

    assert error.successful_indexes == (0,)
    assert error.failed_indexes == (1,)
    assert error.ambiguous_indexes == (2,)
    assert error.cancelled_indexes == (3,)
    assert error.safe_to_retry_indexes == (1, 3)
    assert error.safe_to_retry_queries == ("retry", "cancel")
    assert "0=success" in str(error)


def test_tracking_connection_delegates_and_tracks_cursor_lifecycle() -> None:
    connection = _Connection()
    connection.marker = "delegated"
    state = ExecuteAttemptState()
    tracked = TrackingConnection(connection, state)

    assert tracked.marker == "delegated"
    with tracked.cursor() as cursor:
        cursor.execute("INSERT INTO events VALUES (1)")
    tracked.commit()

    assert state.submitted is True
    assert state.commit_started is True
    assert state.committed is True
    assert connection.executed == ["INSERT INTO events VALUES (1)"]


def test_tracking_cursor_closes_cursor_without_context_protocol() -> None:
    class PlainCursor:
        def __init__(self) -> None:
            self.closed = False
            self.marker = "cursor"

        def execute(self, _query: str) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    cursor = PlainCursor()
    connection = SimpleNamespace(cursor=lambda: cursor)
    tracked = TrackingConnection(connection, ExecuteAttemptState()).cursor()

    assert tracked.marker == "cursor"
    with tracked:
        pass
    assert cursor.closed is True


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("SELECT 1; SELECT 2", True),
        ("", False),
        ("SELECT 1; INSERT INTO t VALUES (1)", False),
    ],
)
def test_read_only_classification(query: str, expected: bool) -> None:
    assert is_read_only_sql(query) is expected


def test_connection_open_and_nonretryable_mutation_errors_are_not_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda _key: (_ for _ in ()).throw(ValueError("open failed")),
    )
    with pytest.raises(ValueError, match="open failed"):
        sql_module.execute("gp", "INSERT INTO t VALUES (1)", retry_cnt=1)

    connection = _Connection()
    monkeypatch.setattr(execute_sql_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(execute_sql_module, "is_non_retryable_sql_error", lambda _exc: True)
    adapter = execute_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(
        adapter,
        "execute_sql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid SQL")),
    )
    with pytest.raises(ValueError, match="invalid SQL"):
        sql_module.execute("gp", "INSERT INTO t VALUES (1)", retry_cnt=1)
    assert connection.rollback_calls == 1


def test_transactional_presubmission_failure_is_safe_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    monkeypatch.setattr(execute_sql_module, "get_sql_connection", lambda _key: connection)
    adapter = execute_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(
        adapter,
        "execute_sql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("before execute")),
    )

    with pytest.raises(OSError, match="before execute"):
        sql_module.execute("gp", "INSERT INTO t VALUES (1)", retry_cnt=1)
    assert connection.rollback_calls == 1


def test_failed_rollback_and_close_helpers_return_safe_outcomes() -> None:
    class BrokenConnection:
        def rollback(self) -> None:
            raise OSError("rollback failed")

        def close(self) -> None:
            raise OSError("close failed")

    options = execute_sql_module.ExecuteSqlOptions("gp", "gp", "SELECT 1")
    assert execute_sql_module._rollback_confirmed(BrokenConnection()) is False
    execute_sql_module._close_execute_connection(BrokenConnection(), options)
