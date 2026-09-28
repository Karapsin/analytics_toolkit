from __future__ import annotations

import importlib
from contextvars import ContextVar
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

execute_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
create_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_create")
read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")

HELPERS = ["insert", "execute_insert", "execute_create", "execute_read"]


def call(name: str, query: Any, **kwargs: Any) -> Any:
    if name == "execute_read":
        return sql.execute_read("gp", query, **kwargs)
    target = kwargs.pop("table_name", "mart.target")
    return getattr(sql, name)("gp", target, query, **kwargs)


@pytest.mark.parametrize("name", HELPERS)
@pytest.mark.parametrize("query", [[], ["SELECT 1", ""], ["SELECT 1", 2], ("SELECT 1",)])
def test_invalid_batches_do_not_connect(name: str, query: Any) -> None:
    with pytest.raises((TypeError, InvalidSqlInputError)):
        call(name, query, concurrency=2)


@pytest.mark.parametrize("name", HELPERS)
@pytest.mark.parametrize(
    "kwargs",
    [
        {"concurrency": True},
        {"concurrency": 0},
        {"soft_concurrency_cap": 0},
        {"hard_concurrency_cap": False},
        {"concurrency": 6},
        {"concurrency": 2, "hard_concurrency_cap": 1},
    ],
)
def test_invalid_caps(name: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        call(name, ["SELECT 1"], **kwargs)


@pytest.mark.parametrize("name", HELPERS[:-1])
@pytest.mark.parametrize("plan_option", ["dry_run", "return_sql"])
def test_ordered_plans_and_target_lists(name: str, plan_option: str) -> None:
    plans = call(
        name,
        ["SELECT 1 AS id", "SELECT 2 AS id"],
        table_name=["mart.first", "mart.second"],
        concurrency=20,
        soft_concurrency_cap=2,
        **{plan_option: True},
    )
    assert [plan.target_table for plan in plans] == ["mart.first", "mart.second"]
    assert "SELECT 1" in plans[0].sqls[-1]
    assert "SELECT 2" in plans[1].sqls[-1]
    assert all(plan.operation == name for plan in plans)
    scalar = call(name, "SELECT 1 AS id", concurrency=20, **{plan_option: True})
    assert scalar.operation == name


@pytest.mark.parametrize("name", HELPERS[:-1])
@pytest.mark.parametrize(
    ("query", "targets"),
    [("SELECT 1", ["mart.a"]), (["SELECT 1", "SELECT 2"], ["mart.a"])],
)
def test_target_list_shape(name: str, query: Any, targets: list[str]) -> None:
    with pytest.raises(InvalidSqlInputError, match="same length"):
        call(name, query, table_name=targets)


@pytest.mark.parametrize("name", HELPERS[:-1])
def test_validation_of_later_item_prevents_earlier_write(name: str) -> None:
    with pytest.raises(InvalidSqlInputError, match="final statement"):
        call(name, ["SELECT 1 AS id", "DELETE FROM mart.target"], concurrency=2)
    with pytest.raises(TypeError, match="table_name"):
        call(name, ["SELECT 1 AS id", "SELECT 2 AS id"], table_name=["mart.a", 4])


@pytest.mark.parametrize("name", HELPERS)
def test_parallel_helpers_preserve_order_connections_and_context(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    barrier = Barrier(2)
    second_finished = Event()
    lock = Lock()
    connections: list[Any] = []
    context = ContextVar("query_batch_test", default="missing")
    context.set("caller")

    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    def connect(_key: str) -> Any:
        connection = Connection()
        with lock:
            connections.append(connection)
        return connection

    def execute(_backend: str, _connection: Any, query: Any, **_kwargs: Any) -> Any:
        assert context.get() == "caller"
        statement = query[-1] if isinstance(query, list) else query
        value = 1 if "SELECT 1" in statement else 2
        barrier.wait(timeout=5)
        if value == 1:
            assert second_finished.wait(timeout=5)
        else:
            second_finished.set()
        if name == "execute_read":
            return pd.DataFrame({"id": [value]})
        return SimpleNamespace(rowcount=value)

    monkeypatch.setattr(execute_module, "get_sql_connection", connect)
    monkeypatch.setattr(read_module, "get_sql_connection", connect)
    monkeypatch.setattr(create_module, "get_sql_connection", connect)
    monkeypatch.setattr(execute_module, "_execute_backend", execute)
    monkeypatch.setattr(read_module, "_execute_read_backend", execute)
    monkeypatch.setattr(
        create_module,
        "_execute_create_attempt",
        lambda options, _adapter, connection, _state: (
            execute(options.backend, connection, options.source_sql).rowcount
        ),
    )
    result = call(
        name,
        ["SELECT 1 AS id", "SELECT 2 AS id"],
        concurrency=8,
        soft_concurrency_cap=2,
        return_metadata=True,
    )
    if name == "execute_read":
        assert [item.data.iloc[0, 0] for item in result] == [1, 2]
    else:
        assert [item.rows for item in result] == [1, 2]
    assert len(connections) == 2
    assert connections[0] is not connections[1]
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("name", HELPERS)
def test_sequential_failure_reports_partial_results(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    attempts: list[str] = []

    def fail_connect(key: str) -> Any:
        attempts.append(key)
        message = "unavailable"
        raise OSError(message)

    for module in (execute_module, read_module, create_module):
        monkeypatch.setattr(module, "get_sql_connection", fail_connect)
    with pytest.raises(sql.SqlBatchExecutionError) as caught:
        call(name, ["SELECT 1 AS id", "SELECT 2 AS id"], retry_cnt=2, timeout_increment=0)
    assert attempts == ["gp", "gp"]
    assert caught.value.failed_indexes == (0,)
    assert caught.value.cancelled_indexes == (1,)
    assert caught.value.items[0].attempts == 2
    assert caught.value.safe_to_retry_queries == ("SELECT 1 AS id", "SELECT 2 AS id")
