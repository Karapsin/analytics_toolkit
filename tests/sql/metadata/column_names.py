from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, wrap_client
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)
from analytics_toolkit.sql.metadata import column_names


@pytest.mark.parametrize(
    ("key", "table", "qualified"),
    [
        ("gp", "events", '"public"."events"'),
        ("gp_sandbox", '"odd.schema"."Mixed Name"', '"odd.schema"."Mixed Name"'),
        ("trino", "events", '"iceberg"."sandbox"."events"'),
        ("trino", "sales.events", '"iceberg"."sales"."events"'),
        ("trino", '"other.catalog"."sales"."Mixed Name"', '"other.catalog"."sales"."Mixed Name"'),
    ],
)
@pytest.mark.parametrize("empty", [False, True])
def test_dbapi_probe_preserves_names_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch, key: str, table: str, qualified: str, empty: bool
) -> None:
    calls = []
    cursor = SimpleNamespace(
        description=[("ID",), ("display name",)],
        execute=calls.append,
        fetchall=lambda: [] if empty else [(1, "discard me")],
        close=lambda: calls.append("cursor closed"),
    )
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: calls.append("closed"))

    def connect(db_key: str) -> SimpleNamespace:
        assert db_key == key
        return connection

    monkeypatch.setattr(column_names, "get_sql_connection", connect)
    assert column_names.table_column_names(key, table) == ("ID", "display name")
    assert calls == [f"SELECT * FROM {qualified} LIMIT 1", "cursor closed", "closed"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_ch_probe_includes_computed_columns_and_empty_metadata(
    monkeypatch: pytest.MonkeyPatch, native: bool, empty: bool
) -> None:
    calls = []
    names = ["id", "label", "bucket", "alias_id"]

    def execute(query: str, **kwargs: Any) -> Any:
        calls.append((query, kwargs))
        assert kwargs["settings"] == {
            "asterisk_include_materialized_columns": 1,
            "asterisk_include_alias_columns": 1,
        }
        rows = [] if empty else [(1, "discard me", 1, 1)]
        if native:
            assert kwargs["with_column_types"] is True
            return rows, [(name, "String") for name in names]
        assert kwargs["fmt"] == "JSON"
        return json.dumps({"meta": [{"name": name} for name in names], "data": rows}).encode()

    raw = SimpleNamespace(execute=execute, raw_query=execute)
    connection = NativeClickHouseClient(raw) if native else raw
    connection.close = lambda: calls.append("closed")
    monkeypatch.setattr(column_names, "get_sql_connection", lambda _: connection)
    assert column_names.table_column_names("ch", "`odd.db`.`Mixed Name`") == tuple(names)
    assert len(calls) == 2
    assert calls[0][0] == "SELECT * FROM `odd.db`.`Mixed Name` LIMIT 1"
    assert calls[-1] == "closed"


@pytest.mark.parametrize("key", ["gp", "trino", "ch"])
def test_probe_errors_close_without_retry_or_metadata_fallback(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    calls = []

    def fail(query: str, **_kwargs: Any) -> None:
        calls.append(query)
        message = "SELECT denied"
        raise PermissionError(message)

    cursor = SimpleNamespace(execute=fail, close=lambda: calls.append("cursor closed"))
    connection = SimpleNamespace(
        cursor=lambda: cursor, raw_query=fail, close=lambda: calls.append("closed")
    )
    monkeypatch.setattr(column_names, "get_sql_connection", lambda _: connection)
    with pytest.raises(PermissionError, match="SELECT denied"):
        column_names.table_column_names(key, "events")
    assert sum(call.startswith("SELECT") for call in calls) == 1
    assert calls[-1] == "closed"
    if key != "ch":
        assert calls[-2] == "cursor closed"


@pytest.mark.parametrize("key", ["gp", "ch"])
def test_missing_result_metadata_is_an_error(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    closed = []
    cursor = SimpleNamespace(description=None, execute=lambda _: None, close=lambda: None)
    connection = SimpleNamespace(
        cursor=lambda: cursor,
        raw_query=lambda *_args, **_kwargs: b'{"meta": [], "data": []}',
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(column_names, "get_sql_connection", lambda _: connection)
    with pytest.raises(ValueError, match="no result metadata"):
        column_names.table_column_names(key, "events")
    assert closed == [True]


def test_ch_http_probe_retains_cluster_routing_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    scope = SqlCancellationScope()

    def query(statement: str, **kwargs: Any) -> bytes:
        calls.append((statement, kwargs))
        assert scope.marker in statement
        assert "cluster(" in statement
        assert "LIMIT 1" in statement
        return b'{"meta": [{"name": "id"}], "data": []}'

    raw = SimpleNamespace(raw_query=query, close=lambda: calls.append("closed"))
    config = SimpleNamespace(cluster_routing=ChClusterRouting("test_cluster"), database="default")
    client = wrap_client(raw, config)
    # Pair discovery is existing routing behavior; this fake has no managed pair.
    monkeypatch.setattr(client._managed_pairs, "resolve", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(column_names, "get_sql_connection", lambda _: client)
    with activate_cancellation_scope(scope):
        assert column_names.table_column_names("ch", "events") == ("id",)
    assert len(calls) == 2
    assert calls[0][1]["fmt"] == "JSON"
    assert calls[-1] == "closed"


@pytest.mark.parametrize("cancel_before_connect", [False, True])
def test_cancelled_probe_does_not_return_columns(
    monkeypatch: pytest.MonkeyPatch, cancel_before_connect: bool
) -> None:
    scope = SqlCancellationScope()
    calls = []

    def query(*_args: Any, **_kwargs: Any) -> bytes:
        scope.request_cancel()
        return b'{"meta": [{"name": "id"}], "data": []}'

    def connect(_key: str) -> SimpleNamespace:
        calls.append("connected")
        return SimpleNamespace(raw_query=query, close=lambda: calls.append("closed"))

    monkeypatch.setattr(column_names, "get_sql_connection", connect)
    if cancel_before_connect:
        scope.request_cancel()
    with activate_cancellation_scope(scope), pytest.raises(AsyncSqlCancelled):
        column_names.table_column_names("ch", "events")
    assert calls == ([] if cancel_before_connect else ["connected", "closed"])
