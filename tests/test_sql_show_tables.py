from __future__ import annotations

import importlib

import pandas as pd
import pytest


show_tables_module = importlib.import_module("analytics_toolkit.sql.show_tables")
sql_module = importlib.import_module("analytics_toolkit.sql")


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def _capture_read_sql(
    monkeypatch: pytest.MonkeyPatch,
    result: pd.DataFrame | None = None,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    returned = (
        result
        if result is not None
        else pd.DataFrame(
            {
                "schema": ["mart"],
                "table_name": ["orders"],
                "db": ["warehouse"],
                "extra": ["ignored"],
            }
        )
    )

    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        calls.append((connection_type, query))
        return returned.copy()

    monkeypatch.setattr(show_tables_module, "read_sql", fake_read_sql)
    return calls


def test_show_tables_is_public_api() -> None:
    assert sql_module.show_tables is show_tables_module.show_tables
    assert "show_tables" in sql_module.__all__
    assert "show_tables" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.show_tables, "__sql_public_timing__", False)


def test_show_tables_greenplum_builds_metadata_sql_and_normalizes_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(monkeypatch)

    result = show_tables_module.show_tables(
        "gp",
        schema="mart",
        conditions="table_name ILIKE '%collections%'",
    )

    assert calls[0][0] == "gp"
    assert _compact(calls[0][1]) == _compact(
        """
        SELECT
            current_database() AS db,
            table_schema AS schema,
            table_name
        FROM information_schema.tables
        WHERE 1 = 1
          AND table_schema = 'mart'
          AND (table_name ILIKE '%collections%')
        ORDER BY table_schema, table_name
        """
    )
    assert list(result.columns) == ["db", "schema", "table_name"]


def test_show_tables_clickhouse_filters_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(monkeypatch)

    show_tables_module.show_tables("ch", schema="analytics")

    assert calls[0][0] == "ch"
    assert _compact(calls[0][1]) == _compact(
        """
        SELECT
            database AS db,
            database AS schema,
            name AS table_name
        FROM system.tables
        WHERE 1 = 1
          AND database = 'analytics'
        ORDER BY database, name
        """
    )


def test_show_tables_trino_uses_catalog_and_schema_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(monkeypatch)

    show_tables_module.show_tables("TRINO", schema="sandbox")

    assert calls[0][0] == "trino"
    assert _compact(calls[0][1]) == _compact(
        """
        SELECT
            table_catalog AS db,
            table_schema AS schema,
            table_name
        FROM iceberg.information_schema.tables
        WHERE 1 = 1
          AND table_schema = 'sandbox'
        ORDER BY table_schema, table_name
        """
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"schema": ""},
        {"schema": "   "},
        {"conditions": ""},
        {"conditions": "   "},
    ],
)
def test_show_tables_rejects_empty_schema_or_conditions(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, str],
) -> None:
    monkeypatch.setattr(
        show_tables_module,
        "read_sql",
        lambda *_args, **_kwargs: pytest.fail("read_sql should not be called"),
    )

    with pytest.raises(
        show_tables_module.InvalidSqlInputError,
        match="must not be empty",
    ):
        show_tables_module.show_tables("gp", **kwargs)


def test_show_tables_rejects_multi_statement_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        show_tables_module,
        "read_sql",
        lambda *_args, **_kwargs: pytest.fail("read_sql should not be called"),
    )

    with pytest.raises(
        show_tables_module.InvalidSqlInputError,
        match="conditions must contain exactly one SQL expression",
    ):
        show_tables_module.show_tables(
            "gp",
            conditions="table_name = 'orders'; SELECT 1",
        )
