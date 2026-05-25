from __future__ import annotations

import importlib

import pandas as pd
import pytest

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


extract_ddl_module = importlib.import_module("analytics_toolkit.sql.ddl.extract_ddl")
ddl_module = importlib.import_module("analytics_toolkit.sql.ddl")
sql_module = importlib.import_module("analytics_toolkit.sql")


def _capture_read_sql(
    monkeypatch: pytest.MonkeyPatch,
    ddl_for_query: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        calls.append((connection_type, query))
        ddl = (
            ddl_for_query[query]
            if ddl_for_query is not None
            else "CREATE TABLE analytics.events (id UInt64)"
        )
        return pd.DataFrame({"ddl": [ddl]})

    monkeypatch.setattr(extract_ddl_module, "read_sql", fake_read_sql)
    return calls


def test_extract_ddl_is_public_api() -> None:
    assert sql_module.extract_ddl is extract_ddl_module.extract_ddl
    assert ddl_module.extract_ddl is extract_ddl_module.extract_ddl
    assert "extract_ddl" in sql_module.__all__
    assert "extract_ddl" in ddl_module.__all__
    assert "extract_ddl" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.extract_ddl, "__sql_public_timing__", False)


def test_extract_ddl_clickhouse_single_table_returns_semicolon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE analytics.events": (
                "CREATE TABLE analytics.events (id UInt64)"
            ),
        },
    )

    result = extract_ddl_module.extract_ddl("ch", "analytics.events")

    assert calls == [("ch", "SHOW CREATE TABLE analytics.events")]
    assert result == "CREATE TABLE analytics.events (id UInt64);"


def test_extract_ddl_preserves_table_order_and_normalizes_semicolons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE mart.orders": " CREATE TABLE mart.orders (id UInt64); ",
            "SHOW CREATE TABLE mart.users": "\nCREATE TABLE mart.users (id UInt64)\n",
        },
    )

    result = extract_ddl_module.extract_ddl(
        "ch",
        ["mart.orders", "mart.users"],
    )

    assert calls == [
        ("ch", "SHOW CREATE TABLE mart.orders"),
        ("ch", "SHOW CREATE TABLE mart.users"),
    ]
    assert result == (
        "CREATE TABLE mart.orders (id UInt64);\n"
        "CREATE TABLE mart.users (id UInt64);"
    )


def test_extract_ddl_trino_resolves_unqualified_table_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE iceberg.sandbox.events": (
                "CREATE TABLE iceberg.sandbox.events (id bigint)"
            ),
        },
    )

    result = extract_ddl_module.extract_ddl("trino", "events")

    assert calls == [("trino", "SHOW CREATE TABLE iceberg.sandbox.events")]
    assert result == "CREATE TABLE iceberg.sandbox.events (id bigint);"


def test_extract_ddl_trino_resolves_schema_qualified_table_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE iceberg.mart.events": (
                "CREATE TABLE iceberg.mart.events (id bigint)"
            ),
        },
    )

    result = extract_ddl_module.extract_ddl("trino", "mart.events")

    assert calls == [("trino", "SHOW CREATE TABLE iceberg.mart.events")]
    assert result == "CREATE TABLE iceberg.mart.events (id bigint);"


def test_extract_ddl_greenplum_uses_pg_get_tabledef(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            (
                "SELECT "
                "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.orders')::oid) "
                "AS ddl"
            ): "CREATE TABLE mart.orders (id bigint)",
        },
    )

    result = extract_ddl_module.extract_ddl("gp", "mart.orders")

    assert calls == [
        (
            "gp",
            "SELECT "
            "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.orders')::oid) "
            "AS ddl",
        )
    ]
    assert result == "CREATE TABLE mart.orders (id bigint);"


def test_extract_ddl_greenplum_escapes_table_name_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            (
                "SELECT "
                "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.o''rders')::oid) "
                "AS ddl"
            ): "CREATE TABLE mart.\"o'rders\" (id bigint)",
        },
    )

    result = extract_ddl_module.extract_ddl("gp", "mart.o'rders")

    assert calls == [
        (
            "gp",
            "SELECT "
            "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.o''rders')::oid) "
            "AS ddl",
        )
    ]
    assert result == "CREATE TABLE mart.\"o'rders\" (id bigint);"


@pytest.mark.parametrize(
    ("tables", "error_type", "match"),
    [
        ([], InvalidSqlInputError, "tables must not be empty"),
        (" ", InvalidSqlInputError, "tables values must not be empty"),
        (["orders", " "], InvalidSqlInputError, "tables values must not be empty"),
        (b"orders", TypeError, "tables must be a string or a sequence of strings"),
        (["orders", 1], TypeError, "tables values must be strings"),
    ],
)
def test_extract_ddl_rejects_invalid_table_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tables: object,
    error_type: type[Exception],
    match: str,
) -> None:
    monkeypatch.setattr(
        extract_ddl_module,
        "read_sql",
        _fail_read_sql,
    )

    with pytest.raises(error_type, match=match):
        extract_ddl_module.extract_ddl("ch", tables)


def test_extract_ddl_raises_when_backend_returns_no_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        del connection_type, query
        return pd.DataFrame({"ddl": [None]})

    monkeypatch.setattr(extract_ddl_module, "read_sql", fake_read_sql)

    with pytest.raises(ValueError, match="No DDL returned for table mart.orders"):
        extract_ddl_module.extract_ddl("ch", "mart.orders")


def _fail_read_sql(_connection_type: str, _query: str) -> pd.DataFrame:
    raise AssertionError("read_sql should not be called")
