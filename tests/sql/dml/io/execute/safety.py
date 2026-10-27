from __future__ import annotations

# ruff: noqa: EM101, PYI034, PYI036, TRY003
import importlib
from typing import Any

import pytest
from analytics_toolkit.sql.connection.errors import SqlOperationContext
from analytics_toolkit.sql.dml.io.execute_safety import (
    SqlBatchExecutionError,
    SqlBatchItemResult,
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


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_default_mutation_exhausts_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    connections = [_Connection() for _ in range(3)]
    attempts: list[str] = []
    monkeypatch.setattr(
        execute_sql_module, "get_sql_connection", lambda _key: connections[len(attempts)]
    )
    adapter = execute_sql_module.get_backend_adapter(backend)

    def fail(_connection: Any, query: str, **_kwargs: Any) -> None:
        attempts.append(query)
        raise OSError("connection reset after submission")

    monkeypatch.setattr(adapter, "execute_sql", fail)
    with pytest.raises(OSError, match="connection reset"):
        sql_module.execute(
            backend, "INSERT INTO sandbox.events VALUES (1)", retry_cnt=3, timeout_increment=0
        )
    assert len(attempts) == 3
    assert all(connection.close_calls == 1 for connection in connections)
    if backend == "gp":
        assert all(connection.rollback_calls == 1 for connection in connections)


def test_read_retries_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_greenplum_commit_failure_retries_with_fresh_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = [_Connection(commit_error=OSError("commit acknowledgement lost")), _Connection()]
    opened: list[_Connection] = []

    def connect(_key: str) -> _Connection:
        connection = connections[len(opened)]
        opened.append(connection)
        return connection

    monkeypatch.setattr(execute_sql_module, "get_sql_connection", connect)
    sql_module.execute(
        "gp", "INSERT INTO sandbox.events VALUES (1)", retry_cnt=2, timeout_increment=0
    )
    assert len(opened) == 2
    assert all(len(connection.executed) == 1 for connection in connections)
    assert all(connection.commit_calls == 1 for connection in connections)
    assert all(connection.close_calls == 1 for connection in connections)
    assert connections[0].rollback_calls == 1


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("name", ["execute", "insert", "execute_insert"])
def test_default_mutation_retries_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    name: str,
) -> None:
    connections = [_Connection(), _Connection()]
    attempts: list[str] = []
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda _key: connections[len(attempts)],
    )
    adapter = execute_sql_module.get_backend_adapter(backend)

    def execute(_connection: Any, query: str, **_kwargs: Any) -> None:
        attempts.append(query)
        if len(attempts) == 1:
            raise OSError("temporary mutation failure")

    monkeypatch.setattr(adapter, "execute_sql", execute)

    args = (
        (backend, "INSERT INTO sandbox.events VALUES (1)")
        if name == "execute"
        else (backend, "sandbox.events", "SELECT 1")
    )
    getattr(sql_module, name)(*args, retry_cnt=2, timeout_increment=0)

    assert len(attempts) == 2


@pytest.mark.parametrize(
    "name", ["execute", "execute_sql", "insert", "execute_insert", "execute_create"]
)
@pytest.mark.parametrize("policy", ["safe", "always", "never", "sometimes", None])
def test_removed_retry_policy_is_rejected_before_connection_lookup(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    policy: Any,
) -> None:
    for module in [
        execute_sql_module,
        importlib.import_module("analytics_toolkit.sql.dml.io.execute_create"),
    ]:
        monkeypatch.setattr(
            module,
            "get_connection_config",
            lambda *_args: pytest.fail("connection config should not be read"),
        )
    args = (
        ("gp", "SELECT 1")
        if name in {"execute", "execute_sql"}
        else ("gp", "sandbox.events", "SELECT 1")
    )
    function = (
        execute_sql_module.execute_sql if name == "execute_sql" else getattr(sql_module, name)
    )
    with pytest.raises(TypeError, match="retry_policy"):
        function(*args, retry_policy=policy)


def test_ambiguous_mutation_error_preserves_public_context() -> None:
    context = SqlOperationContext(operation="execute_sql", alias="gp", backend="gp")
    original = OSError("commit acknowledgement lost")
    error = sql_module.AmbiguousSqlMutationError(
        "mutation outcome unknown",
        context=context,
        original_error=original,
    )
    assert str(error) == "mutation outcome unknown"
    assert error.original_error is original
    assert error.context is context
    assert error.sql_context is context
    assert error.analytics_toolkit_sql_retry_safe is False


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
    adapter = execute_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(
        adapter,
        "execute_sql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SyntaxError("invalid SQL")),
    )
    with pytest.raises(SyntaxError, match="invalid SQL"):
        sql_module.execute("gp", "INSERT INTO t VALUES (1)", retry_cnt=3, timeout_increment=0)
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
        sql_module.execute("gp", "INSERT INTO t VALUES (1)", retry_cnt=3, timeout_increment=0)
    assert connection.rollback_calls == 3


def test_failed_rollback_and_close_helpers_return_safe_outcomes() -> None:
    class BrokenConnection:
        def rollback(self) -> None:
            raise OSError("rollback failed")

        def close(self) -> None:
            raise OSError("close failed")

    options = execute_sql_module.ExecuteSqlOptions("gp", "gp", "SELECT 1")
    execute_sql_module._close_execute_connection(BrokenConnection(), options)
