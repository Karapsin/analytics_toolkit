from __future__ import annotations

from tests.sql._support.backend_helpers import (
    Any,
    InvalidSqlInputError,
    RecordingConnection,
    RecordingCursor,
    RoutingClickHouseConnection,
    ch_metadata,
    ch_wait,
    gp_ddl,
    pd,
    pytest,
    trino_operations,
)


def test_clickhouse_shard_stats_handles_bad_rows_and_cluster_failures() -> None:
    good = ch_metadata.ClickHouseShardTable("core", "analytics", "events_shard")
    bad = ch_metadata.ClickHouseShardTable("broken", "analytics", "bad_shard")

    def read_sql(_connection_key: str, query: str) -> pd.DataFrame:
        if "'broken'" in query:
            message = "cluster unavailable"
            raise RuntimeError(message)
        return pd.DataFrame(
            [
                {"shard_database": 1, "shard_table": None},
                {
                    "shard_database": "analytics",
                    "shard_table": "events_shard",
                    "row_count": 7,
                    "table_size_bytes": 9,
                },
            ]
        )

    stats = ch_metadata.read_clickhouse_shard_stats(
        "ch",
        {good, bad},
        read_sql=read_sql,
    )
    assert stats[good] == (7, 9)
    assert bad not in stats


def test_clickhouse_wait_table_and_schema_timeout_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = RoutingClickHouseConnection(lambda _sql: [])
    with pytest.raises(TimeoutError, match="not visible on every host"):
        ch_wait._wait_for_ch_table_on_cluster(
            empty,
            "analytics.events",
            ch_cluster="core",
            timeout_seconds=0,
        )

    failing = RoutingClickHouseConnection(lambda _sql: RuntimeError("metadata failed"))
    with pytest.raises(TimeoutError) as table_error:
        ch_wait._wait_for_ch_table_on_cluster(
            failing,
            "analytics.events",
            ch_cluster="core",
            timeout_seconds=0,
        )
    assert isinstance(table_error.value.__cause__, RuntimeError)

    with monkeypatch.context() as patch:
        patch.setattr(ch_wait, "normalize_table_schema", lambda *_args, **_kwargs: {})
        ch_wait._wait_for_ch_table_schema_on_cluster(
            empty,
            "analytics.events",
            expected_column_types={},
            ch_cluster="core",
        )

    def schema_route(sql: str) -> list[tuple[Any, ...]]:
        if "SELECT name, type, count()" in sql:
            return [("id", "String", 1), ("extra", "UInt8", 1), ("short",)]
        if "system.clusters" in sql or "system, one" in sql:
            return [(1,)]
        return [(0,)]

    schema_connection = RoutingClickHouseConnection(schema_route)
    with pytest.raises(TimeoutError, match="Schema mismatch details"):
        ch_wait._wait_for_ch_table_schema_on_cluster(
            schema_connection,
            "analytics.events",
            expected_column_types={"id": "UInt64", "missing": "String"},
            ch_cluster="core",
            timeout_seconds=0,
        )


def test_gp_ddl_helper_error_and_formatting_branches() -> None:
    row = pd.Series({"value": [1, 2]})
    assert gp_ddl.optional_metadata_value(row, "missing") is None
    assert gp_ddl.is_missing_value([1, 2]) is False
    with pytest.raises(ValueError, match="No metadata field"):
        gp_ddl.require_metadata_value(pd.Series(dtype=object), "oid", "events")

    assert (
        gp_ddl.format_gp_constraint_definition(pd.Series({"constraint_def": "CHECK (id > 0)"}))
        == "CHECK (id > 0)"
    )
    assert gp_ddl.format_gp_inherits_clause(pd.DataFrame()) == ""
    assert gp_ddl.format_gp_storage_clause(None) == ""
    assert gp_ddl.format_optional_statement(None) == ""
    assert (
        gp_ddl.format_gp_column_comment(
            '"public"."events"',
            pd.Series({"column_name": "id", "column_comment": None}),
        )
        == ""
    )
    assert gp_ddl.format_gp_distribution_clause(pd.DataFrame(), pd.DataFrame()) == ""
    assert (
        gp_ddl.format_gp_distribution_clause(
            pd.DataFrame([{"policy_type": "p", "attrnums": "{}"}]),
            pd.DataFrame(),
        )
        == "DISTRIBUTED RANDOMLY"
    )
    assert (
        gp_ddl.format_gp_distribution_clause(
            pd.DataFrame([{"policy_type": "p", "attrnums": "{9}"}]),
            pd.DataFrame([{"attnum": 1, "column_name": "id"}]),
        )
        == "DISTRIBUTED RANDOMLY"
    )

    assert gp_ddl.first_optional_value(pd.DataFrame(), "value") is None
    assert gp_ddl.first_optional_value(pd.DataFrame([{"other": 1}]), "value") is None
    assert gp_ddl.first_optional_value(pd.DataFrame([{"value": pd.NA}]), "value") is None
    assert gp_ddl.metadata_bool(pd.Series(dtype=object), "missing") is False
    assert gp_ddl.metadata_bool(pd.Series({"flag": pd.NA}), "flag") is False
    assert gp_ddl.metadata_bool(pd.Series({"flag": "yes"}), "flag") is True
    assert gp_ddl.parse_pg_array_text("['a', \"b\"]") == ["a", "b"]
    assert gp_ddl.parse_pg_array_text("plain") == ["plain"]
    assert gp_ddl.parse_attrnums("{-2,0,3}") == [3]

    class UndefinedFunction(Exception):  # noqa: N818
        pass

    assert gp_ddl.is_missing_pg_get_tabledef_error(UndefinedFunction("pg_get_tabledef unavailable"))
    assert not gp_ddl.is_missing_pg_get_tabledef_error(UndefinedFunction("different function"))
    assert gp_ddl.exception_text(Exception()) == ""


def test_trino_operation_cursor_and_error_paths() -> None:
    with pytest.raises(InvalidSqlInputError, match="Greenplum"):
        trino_operations.validate_drop_partitions_options(
            object(),
            partition_column="event_date",
            gp_truncate=True,
        )
    with pytest.raises(InvalidSqlInputError, match="partition_column"):
        trino_operations.validate_drop_partitions_options(
            object(),
            partition_column=None,
            gp_truncate=False,
        )
    with pytest.raises(InvalidSqlInputError, match="partition_column"):
        trino_operations.build_drop_partitions_sqls(
            object(),
            "hive.analytics.events",
            ["2025-01-01"],
        )

    cursor = RecordingCursor(rows=[("tmp_a",), (2,)])
    connection = RecordingConnection(cursor)
    assert trino_operations.query_transfer_stage_table_names(
        object(),
        connection,
        connection_key="trino",
        transfer_staging_schema="hive.stage",
        table_pattern="tmp_%",
    ) == ["tmp_a", "2"]
    assert cursor.closed is True

    with pytest.raises(ValueError, match="No DDL"):
        trino_operations._first_result_value(pd.DataFrame(), "events")
    with pytest.raises(ValueError, match="No DDL"):
        trino_operations._first_result_value(pd.DataFrame({"ddl": [pd.NA]}), "events")
