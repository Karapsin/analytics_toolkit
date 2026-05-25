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
                "row_count": [1200],
                "table_size_bytes": [1536],
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
            table_name,
            CASE
                WHEN c.reltuples >= 0 THEN c.reltuples::bigint
                ELSE NULL
            END AS row_count,
            CASE
                WHEN c.relkind IN ('r', 'm', 'p') THEN pg_total_relation_size(c.oid)
                ELSE NULL
            END AS table_size_bytes
        FROM information_schema.tables AS t
        LEFT JOIN pg_catalog.pg_namespace AS n
          ON n.nspname = t.table_schema
        LEFT JOIN pg_catalog.pg_class AS c
          ON c.relnamespace = n.oid
          AND c.relname = t.table_name
        WHERE 1 = 1
          AND table_schema = 'mart'
          AND (table_name ILIKE '%collections%')
        ORDER BY table_schema, table_name
        """
    )
    assert list(result.columns) == [
        "db",
        "schema",
        "table_name",
        "row_count",
        "table_size",
    ]
    assert result.loc[0, "row_count"] == 1200
    assert result.loc[0, "table_size"] == "1.50 KiB"


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
            name AS table_name,
            total_rows AS row_count,
            total_bytes AS table_size_bytes
        FROM system.tables
        WHERE 1 = 1
          AND database = 'analytics'
        ORDER BY database, name
        """
    )


def test_show_tables_filters_single_table_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(monkeypatch)

    show_tables_module.show_tables("ch", table_name="events")

    assert _compact(calls[0][1]) == _compact(
        """
        SELECT
            database AS db,
            database AS schema,
            name AS table_name,
            total_rows AS row_count,
            total_bytes AS table_size_bytes
        FROM system.tables
        WHERE 1 = 1
          AND name = 'events'
        ORDER BY database, name
        """
    )


def test_show_tables_filters_schema_qualified_table_name_when_schema_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(monkeypatch)

    show_tables_module.show_tables(
        "ch",
        schema="pa_core_stage",
        table_name="pa_core_stage.funnels_dash_cvmoffers_daily",
    )

    assert _compact(calls[0][1]) == _compact(
        """
        SELECT
            database AS db,
            database AS schema,
            name AS table_name,
            total_rows AS row_count,
            total_bytes AS table_size_bytes
        FROM system.tables
        WHERE 1 = 1
          AND database = 'pa_core_stage'
          AND name = 'funnels_dash_cvmoffers_daily'
        ORDER BY database, name
        """
    )


def test_show_tables_clickhouse_distributed_tables_use_shard_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        calls.append((connection_type, query))
        if "FROM system.tables" in query:
            return pd.DataFrame(
                {
                    "db": ["pa_core_stage", "pa_core_stage", "pa_core_stage"],
                    "schema": ["pa_core_stage", "pa_core_stage", "pa_core_stage"],
                    "table_name": ["events", "events_copy", "local_table"],
                    "row_count": [None, None, 7],
                    "table_size_bytes": [0, 0, 32],
                    "engine": ["Distributed", "Distributed", "MergeTree"],
                    "engine_full": [
                        "Distributed('core', 'pa_core_stage', 'events_shard', cityHash64(id))",
                        "Distributed('core', 'pa_core_stage', 'events_shard', cityHash64(id))",
                        "MergeTree ORDER BY tuple()",
                    ],
                }
            )
        if "FROM cluster('core', system, tables)" in query:
            return pd.DataFrame(
                {
                    "shard_database": ["pa_core_stage"],
                    "shard_table": ["events_shard"],
                    "row_count": [1200],
                    "table_size_bytes": [2048],
                }
            )
        raise AssertionError(f"Unexpected query:\n{query}")

    monkeypatch.setattr(show_tables_module, "read_sql", fake_read_sql)

    result = show_tables_module.show_tables(
        "ch",
        schema="pa_core_stage",
        table_name=["events", "events_copy", "local_table"],
        ch_distributed_table_stats=True,
    )

    assert [call[0] for call in calls] == ["ch", "ch"]
    assert "engine_full" in calls[0][1]
    assert _compact(calls[1][1]) == _compact(
        """
        SELECT
            database AS shard_database,
            name AS shard_table,
            sum(ifNull(total_rows, 0)) AS row_count,
            sum(ifNull(total_bytes, 0)) AS table_size_bytes
        FROM cluster('core', system, tables)
        WHERE (database, name) IN (('pa_core_stage', 'events_shard'))
        GROUP BY database, name
        ORDER BY database, name
        """
    )
    assert result["row_count"].tolist() == [1200, 1200, 7]
    assert result["table_size"].tolist() == ["2.00 KiB", "2.00 KiB", "32 B"]
    assert list(result.columns) == [
        "db",
        "schema",
        "table_name",
        "row_count",
        "table_size",
    ]


def test_show_tables_clickhouse_distributed_stats_support_current_database_macro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        calls.append((connection_type, query))
        if "FROM system.tables" in query:
            return pd.DataFrame(
                {
                    "db": ["analytics"],
                    "schema": ["analytics"],
                    "table_name": ["events"],
                    "row_count": [None],
                    "table_size_bytes": [0],
                    "engine": ["Distributed"],
                    "engine_full": [
                        "Distributed('{cluster}', currentDatabase(), 'events_shard')",
                    ],
                }
            )
        if "getMacro('cluster')" in query:
            return pd.DataFrame({"cluster_name": ["core"]})
        if "FROM cluster('core', system, tables)" in query:
            return pd.DataFrame(
                {
                    "shard_database": ["analytics"],
                    "shard_table": ["events_shard"],
                    "row_count": [42],
                    "table_size_bytes": [1024],
                }
            )
        raise AssertionError(f"Unexpected query:\n{query}")

    monkeypatch.setattr(show_tables_module, "read_sql", fake_read_sql)

    result = show_tables_module.show_tables(
        "ch",
        schema="analytics",
        ch_distributed_table_stats=True,
    )

    assert [call[0] for call in calls] == ["ch", "ch", "ch"]
    assert "WHERE (database, name) IN (('analytics', 'events_shard'))" in calls[2][1]
    assert result.loc[0, "row_count"] == 42
    assert result.loc[0, "table_size"] == "1.00 KiB"


def test_show_tables_clickhouse_distributed_stats_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        calls.append((connection_type, query))
        if "FROM system.tables" in query:
            return pd.DataFrame(
                {
                    "db": ["pa_core_stage"],
                    "schema": ["pa_core_stage"],
                    "table_name": ["events"],
                    "row_count": [None],
                    "table_size_bytes": [0],
                    "engine": ["Distributed"],
                    "engine_full": [
                        "Distributed('core', 'pa_core_stage', 'events_shard')",
                    ],
                }
            )
        raise RuntimeError("cluster metadata unavailable")

    monkeypatch.setattr(show_tables_module, "read_sql", fake_read_sql)

    result = show_tables_module.show_tables(
        "ch",
        table_name="events",
        ch_distributed_table_stats=True,
    )

    assert len(calls) == 2
    assert result.loc[0, "row_count"] is None
    assert result.loc[0, "table_size"] == "0 B"


def test_show_tables_filters_multiple_table_names_and_escapes_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(monkeypatch)

    show_tables_module.show_tables(
        "gp",
        schema="mart",
        table_name=["orders", "customer's"],
        conditions="table_type = 'BASE TABLE'",
    )

    assert _compact(calls[0][1]) == _compact(
        """
        SELECT
            current_database() AS db,
            table_schema AS schema,
            table_name,
            CASE
                WHEN c.reltuples >= 0 THEN c.reltuples::bigint
                ELSE NULL
            END AS row_count,
            CASE
                WHEN c.relkind IN ('r', 'm', 'p') THEN pg_total_relation_size(c.oid)
                ELSE NULL
            END AS table_size_bytes
        FROM information_schema.tables AS t
        LEFT JOIN pg_catalog.pg_namespace AS n
          ON n.nspname = t.table_schema
        LEFT JOIN pg_catalog.pg_class AS c
          ON c.relnamespace = n.oid
          AND c.relname = t.table_name
        WHERE 1 = 1
          AND table_schema = 'mart'
          AND table_name IN ('orders', 'customer''s')
          AND (table_type = 'BASE TABLE')
        ORDER BY table_schema, table_name
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
            table_name,
            CAST(NULL AS BIGINT) AS row_count,
            CAST(NULL AS BIGINT) AS table_size_bytes
        FROM iceberg.information_schema.tables
        WHERE 1 = 1
          AND table_schema = 'sandbox'
        ORDER BY table_schema, table_name
        """
    )


def test_show_tables_returns_expected_columns_for_empty_columnless_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_read_sql(monkeypatch, pd.DataFrame())

    result = show_tables_module.show_tables(
        "ch",
        schema="pa_core_stage",
        conditions="table_name = 'pa_core_stage.funnels_dash_cvmoffers_daily'",
    )

    assert result.empty
    assert list(result.columns) == [
        "db",
        "schema",
        "table_name",
        "row_count",
        "table_size",
    ]


def test_show_tables_formats_byte_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        pd.DataFrame(
            {
                "db": ["db", "db", "db", "db"],
                "schema": ["mart", "mart", "mart", "mart"],
                "table_name": ["empty", "small", "medium", "unknown"],
                "row_count": [0, 1000.0, -1, None],
                "table_size_bytes": [0, 1024, 2_621_440, None],
            }
        ),
    )

    result = show_tables_module.show_tables("gp")

    assert calls
    assert result["row_count"].tolist() == [0, 1000, None, None]
    assert result["table_size"].tolist() == [
        "0 B",
        "1.00 KiB",
        "2.50 MiB",
        None,
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"schema": ""},
        {"schema": "   "},
        {"table_name": ""},
        {"table_name": "   "},
        {"table_name": []},
        {"table_name": ["orders", ""]},
        {"conditions": ""},
        {"conditions": "   "},
    ],
)
def test_show_tables_rejects_empty_schema_table_name_or_conditions(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
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


@pytest.mark.parametrize(
    "table_name",
    [
        123,
        ["orders", 123],
    ],
)
def test_show_tables_rejects_invalid_table_name_type(
    monkeypatch: pytest.MonkeyPatch,
    table_name: object,
) -> None:
    monkeypatch.setattr(
        show_tables_module,
        "read_sql",
        lambda *_args, **_kwargs: pytest.fail("read_sql should not be called"),
    )

    with pytest.raises(TypeError, match="table_name"):
        show_tables_module.show_tables("gp", table_name=table_name)


def test_show_tables_rejects_invalid_ch_distributed_table_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        show_tables_module,
        "read_sql",
        lambda *_args, **_kwargs: pytest.fail("read_sql should not be called"),
    )

    with pytest.raises(TypeError, match="ch_distributed_table_stats"):
        show_tables_module.show_tables(
            "ch",
            ch_distributed_table_stats="yes",
        )


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
