from __future__ import annotations

from tests.sql._support.backend_helpers import (
    Any,
    InvalidSqlInputError,
    RoutingClickHouseConnection,
    SimpleNamespace,
    builtins,
    ch_metadata,
    ch_operations,
    ch_wait,
    exp,
    gp_adapter_module,
    gp_ddl,
    gp_insert,
    importlib,
    pd,
    pytest,
    sys,
)


def test_clickhouse_engine_argument_and_parser_edges() -> None:
    assert ch_metadata.normalize_clickhouse_distributed_database_arg("", "db") is None
    assert ch_metadata.normalize_clickhouse_engine_arg("   ") is None
    assert ch_metadata.normalize_clickhouse_engine_arg("'it''s'") == "it's"
    assert ch_metadata.normalize_clickhouse_engine_arg('"a""b"') == 'a"b'
    assert ch_metadata.extract_clickhouse_function_args("Distributed(", "Distributed") is None
    assert (
        ch_metadata.find_clickhouse_function_call(
            "'Distributed(ignored)' Distributed ('core', 'db', 'table')",
            "Distributed",
        )
        == 23
    )
    assert (
        ch_metadata.find_clickhouse_function_call(
            "`Distributed` and fooDistributed('x')",
            "Distributed",
        )
        is None
    )
    assert ch_metadata.find_matching_paren("not a call", 0) is None
    assert ch_metadata.find_matching_paren("('a\\'b'", 0) is None
    assert ch_metadata.split_top_level_args("'a,b', nested(1, 2), , `x``y`") == [
        "'a,b'",
        "nested(1, 2)",
        "`x``y`",
    ]
    assert ch_metadata.skip_whitespace("  x", 0) == 2
    assert ch_metadata.is_clickhouse_identifier_boundary("a", -1)
    assert not ch_metadata.is_clickhouse_identifier_boundary("a", 0)


def test_clickhouse_metadata_application_and_macro_fallbacks() -> None:
    tables = pd.DataFrame({"name": ["events"]})
    assert (
        ch_metadata.apply_clickhouse_shard_stats(
            "ch",
            tables,
            read_sql=lambda *_args: pd.DataFrame(),
        )
        is tables
    )

    distributed = pd.DataFrame(
        [
            {
                "schema": "analytics",
                "engine": "Distributed",
                "engine_full": "Distributed('{cluster}', currentDatabase(), events_shard)",
            },
            {
                "schema": "analytics",
                "engine": "MergeTree",
                "engine_full": "MergeTree()",
            },
        ]
    )
    unchanged = ch_metadata.apply_clickhouse_shard_stats(
        "ch",
        distributed,
        read_sql=lambda *_args: pd.DataFrame(),
    )
    assert unchanged is distributed

    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: (_ for _ in ()).throw(RuntimeError("unavailable")),
        )
        == "{cluster}"
    )
    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: pd.DataFrame({"other": ["core"]}),
        )
        == "{cluster}"
    )
    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: pd.DataFrame({"cluster_name": [pd.NA]}),
        )
        == "{cluster}"
    )
    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: pd.DataFrame({"cluster_name": ["  "]}),
        )
        == "{cluster}"
    )


def test_clickhouse_operations_prepare_existing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = importlib.import_module("analytics_toolkit.sql.backends.ch.lifecycle")
    pair_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        lifecycle,
        "drop_ch_distributed_table_pair",
        lambda *_args, **kwargs: pair_calls.append(kwargs),
    )
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda *_args, **_kwargs: None,
    )

    adapter = SimpleNamespace(
        table_exists=lambda *_args, **_kwargs: True,
        drop_table=lambda *_args, **_kwargs: None,
    )
    assert (
        ch_operations.prepare_existing_target_for_create_from_sql(
            adapter,
            object(),
            "analytics.events",
            drop_target_if_exists=True,
            ch_only_shard=True,
        )
        is False
    )
    assert (
        ch_operations.prepare_existing_target_for_create_from_sql(
            adapter,
            object(),
            "analytics.events",
            drop_target_if_exists=True,
            connection_key=None,
        )
        is True
    )
    assert pair_calls[0]["per_host_connection_factory"] is None


def test_clickhouse_operations_small_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(InvalidSqlInputError, match="only supported for Trino"):
        ch_operations.build_show_tables_query(
            object(),
            object(),
            None,
            None,
            None,
            trino_catalog="hive",
        )

    connection = RoutingClickHouseConnection(lambda _sql: [("one",), (2,)])
    assert ch_operations.query_transfer_stage_table_names(
        object(),
        connection,
        connection_key="ch",
        transfer_staging_schema="staging",
        table_pattern="tmp_%",
    ) == ["one", "2"]
    assert (
        ch_operations.qualify_transfer_stage_table_name(
            object(),
            "ch",
            "staging",
            "tmp_1",
        )
        == "staging.tmp_1"
    )

    with pytest.raises(ValueError, match="ch_drop_shard must be True"):
        ch_operations.build_drop_tables_sqls(
            object(),
            "analytics.events_shard",
            ch_drop_shard=False,
        )

    with pytest.raises(ValueError, match="No DDL"):
        ch_operations._first_result_value(pd.DataFrame(), "events")
    with pytest.raises(ValueError, match="No DDL"):
        ch_operations._first_result_value(pd.DataFrame({"ddl": [pd.NA]}), "events")

    monkeypatch.setattr(ch_operations, "parse_one", lambda *_args, **_kwargs: exp.Column())
    assert ch_operations._is_default_ch_shard_table_name("events_shard") is False


def test_clickhouse_wait_cluster_absence_empty_and_timeout_details() -> None:
    connection = RoutingClickHouseConnection(
        lambda sql: (
            [(1,)] if "count()" in sql else [("host-a", "analytics", "events", "Distributed")]
        )
    )
    ch_wait._wait_for_ch_tables_absence_on_cluster(
        connection,
        ["", "   "],
        ch_cluster="core",
    )
    with pytest.raises(TimeoutError, match="Leftover table"):
        ch_wait._wait_for_ch_tables_absence_on_cluster(
            connection,
            ["analytics.events"],
            ch_cluster="core",
            timeout_seconds=0,
        )

    failing = RoutingClickHouseConnection(lambda _sql: RuntimeError("metadata failed"))
    with pytest.raises(TimeoutError, match="still visible") as error:
        ch_wait._wait_for_ch_tables_absence_on_cluster(
            failing,
            ["analytics.events"],
            ch_cluster="core",
            timeout_seconds=0,
        )
    assert isinstance(error.value.__cause__, RuntimeError)


def test_clickhouse_wait_local_timeout_and_absence_paths() -> None:
    absent = RoutingClickHouseConnection(lambda _sql: [])
    with pytest.raises(TimeoutError, match="not visible"):
        ch_wait._wait_for_ch_table(absent, "analytics.events", timeout_seconds=0)

    present = RoutingClickHouseConnection(lambda _sql: [(1,)])
    with pytest.raises(TimeoutError, match="still visible"):
        ch_wait._wait_for_ch_table_absence(
            present,
            "analytics.events",
            timeout_seconds=0,
        )

    ch_wait._wait_for_ch_distributed_table_pair_absence(
        absent,
        "analytics.events",
        ch_cluster=None,
        timeout_seconds=0,
    )
    assert absent.queries[-2:] == [
        "EXISTS TABLE analytics.events",
        "EXISTS TABLE analytics.events_shard",
    ]


def test_clickhouse_wait_query_and_format_helpers() -> None:
    empty = RoutingClickHouseConnection(lambda _sql: [])
    assert ch_wait._query_ch_count(empty, "SELECT count()") == 0
    assert (
        ch_wait._query_ch_cluster_table_rows(
            empty,
            table_names=[],
            ch_cluster="core",
        )
        == []
    )

    rows = [("short",), *[(f"host-{index}", "db", "table", "MergeTree") for index in range(11)]]
    formatted = ch_wait._format_ch_cluster_table_rows(rows)
    assert formatted.endswith("...")
    assert "host-0: db.table" in formatted
    assert ch_wait._format_ch_cluster_table_rows([("short",)]) == ""

    calls = 0

    def host_route(_sql: str) -> list[tuple[int]] | RuntimeError:
        nonlocal calls
        calls += 1
        if calls == 2:
            return RuntimeError("system.clusters unavailable")
        return [(2,)]

    host_connection = RoutingClickHouseConnection(host_route)
    assert ch_wait._query_ch_cluster_host_counts(
        host_connection,
        cluster_name="core",
        remote_hosts_sql="remote",
    ) == (2, 2)


def test_clickhouse_wait_schema_diagnostics_and_cluster_resolution() -> None:
    failing = RoutingClickHouseConnection(lambda _sql: RuntimeError("unavailable"))
    assert (
        ch_wait._describe_ch_cluster_schema_mismatch(
            failing,
            "analytics.events",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            expected_hosts=1,
        )
        == ""
    )

    matching = RoutingClickHouseConnection(lambda _sql: [("id", "UInt64", 1)])
    assert (
        ch_wait._describe_ch_cluster_schema_mismatch(
            matching,
            "analytics.events",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            expected_hosts=1,
        )
        == ""
    )

    assert ch_wait._resolve_ch_cluster_name_for_wait(matching, "'core'") == "core"
    macro_failure = RoutingClickHouseConnection(lambda _sql: RuntimeError("macro failed"))
    with pytest.raises(ValueError, match="Could not resolve"):
        ch_wait._resolve_ch_cluster_name_for_wait(macro_failure, "{cluster}")
    blank_macro = RoutingClickHouseConnection(lambda _sql: [("",)])
    with pytest.raises(ValueError, match="Could not resolve"):
        ch_wait._resolve_ch_cluster_name_for_wait(blank_macro, "{cluster}")

    assert ch_wait._strip_sql_wrapping_quotes("x") == "x"
    assert ch_wait._strip_sql_wrapping_quotes("'it''s'") == "it's"
    assert ch_wait._strip_sql_wrapping_quotes('"core"') == "core"
    assert ch_wait._extract_ch_macro_name("bad macro") is None


@pytest.mark.parametrize(
    ("data_type", "udt_name", "precision", "scale", "expected"),
    [
        ("numeric", "numeric", 7, None, "NUMERIC(7)"),
        ("character varying", "varchar", None, None, "VARCHAR"),
        ("timestamp without time zone", "timestamp", None, None, "TIMESTAMP"),
        ("timestamp with time zone", "timestamptz", None, None, "TIMESTAMP WITH TIME ZONE"),
        ("integer", "int4", None, None, "INTEGER"),
        ("bigint", "int8", None, None, "BIGINT"),
        ("smallint", "int2", None, None, "SMALLINT"),
        ("boolean", "bool", None, None, "BOOLEAN"),
        ("date", "date", None, None, "DATE"),
        ("text", "text", None, None, "TEXT"),
        ("ARRAY", "_int4", None, None, "_INT4"),
    ],
)
def test_format_gp_information_schema_type_branches(
    data_type: str,
    udt_name: str,
    precision: int | None,
    scale: int | None,
    expected: str,
) -> None:
    assert (
        gp_adapter_module.format_gp_information_schema_type(
            data_type,
            udt_name,
            precision,
            scale,
        )
        == expected
    )


def test_gp_ddl_catalog_reconstruction() -> None:
    def read_sql(_connection_key: str, query: str) -> pd.DataFrame:  # noqa: PLR0911
        if "FROM pg_catalog.pg_class AS c" in query:
            return pd.DataFrame(
                [
                    {
                        "oid": "42",
                        "schema_name": "reporting",
                        "relation_name": "events",
                        "reloptions": "{appendonly=true,orientation=column}",
                        "table_comment": "table note",
                    }
                ]
            )
        if "FROM pg_catalog.pg_attribute AS a" in query:
            return pd.DataFrame(
                [
                    {
                        "attnum": 1,
                        "column_name": "id",
                        "formatted_type": "bigint",
                        "default_expr": "1",
                        "is_not_null": True,
                        "column_comment": "identifier",
                    },
                    {
                        "attnum": 2,
                        "column_name": "payload",
                        "formatted_type": "text",
                        "default_expr": None,
                        "is_not_null": False,
                        "column_comment": None,
                    },
                ]
            )
        if "FROM pg_catalog.pg_constraint" in query:
            return pd.DataFrame(
                [
                    {
                        "constraint_name": "events_pk",
                        "constraint_def": "PRIMARY KEY (id)",
                    },
                    {"constraint_name": "ignored", "constraint_def": None},
                ]
            )
        if "FROM pg_catalog.pg_inherits" in query:
            return pd.DataFrame([{"parent_schema": "base", "parent_table": "parent"}])
        if "FROM pg_catalog.pg_index" in query:
            return pd.DataFrame([{"index_def": "CREATE INDEX events_payload_idx;"}])
        if "has_partkeydef" in query:
            return pd.DataFrame([{"has_partkeydef": True, "has_partition_def": False}])
        if "pg_get_partkeydef" in query:
            return pd.DataFrame([{"partition_def": "RANGE (id)"}])
        if "gp_distribution_policy" in query:
            return pd.DataFrame([{"policy_type": "p", "attrnums": "{1}"}])
        raise AssertionError(query)

    ddl = gp_ddl.extract_greenplum_catalog_ddl(
        "warehouse",
        "reporting.events",
        read_sql=read_sql,
    )
    assert 'CREATE TABLE "reporting"."events"' in ddl
    assert 'CONSTRAINT "events_pk" PRIMARY KEY (id)' in ddl
    assert 'INHERITS ("base"."parent")' in ddl
    assert "PARTITION BY RANGE (id)" in ddl
    assert 'DISTRIBUTED BY ("id")' in ddl
    assert "COMMENT ON TABLE" in ddl
    assert "COMMENT ON COLUMN" in ddl


def test_gp_ddl_distribution_catalog_fallbacks() -> None:
    columns = pd.DataFrame([{"attnum": 1, "column_name": "id"}])
    calls = 0

    class UndefinedColumn(Exception):  # noqa: N818
        pass

    def fallback_read(_connection_key: str, _query: str) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        if calls < 3:
            message = "legacy catalog"
            raise UndefinedColumn(message)
        return pd.DataFrame([{"policy_type": "replicated", "attrnums": None}])

    assert (
        gp_ddl.read_gp_distribution_clause(
            "gp",
            "1",
            columns,
            read_sql=fallback_read,
        )
        == "DISTRIBUTED REPLICATED"
    )

    def missing_shape(_connection_key: str, _query: str) -> pd.DataFrame:
        message = "legacy catalog"
        raise UndefinedColumn(message)

    assert (
        gp_ddl.read_gp_distribution_clause(
            "gp",
            "1",
            columns,
            read_sql=missing_shape,
        )
        == ""
    )

    with pytest.raises(RuntimeError, match="permission"):
        gp_ddl.read_gp_distribution_clause(
            "gp",
            "1",
            columns,
            read_sql=lambda *_args: (_ for _ in ()).throw(RuntimeError("permission")),
        )


def test_gp_ddl_minimal_catalog_and_empty_helper_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_partition_clause = gp_ddl.read_gp_partition_clause
    relation = pd.DataFrame(
        [
            {
                "oid": "1",
                "schema_name": "public",
                "relation_name": "events",
                "reloptions": None,
                "table_comment": None,
            }
        ]
    )
    columns = pd.DataFrame(
        [
            {
                "column_name": "id",
                "formatted_type": "integer",
                "default_expr": None,
                "is_not_null": False,
                "column_comment": None,
                "attnum": 1,
            }
        ]
    )

    def read_sql(_connection_key: str, query: str) -> pd.DataFrame:
        if "FROM pg_catalog.pg_class AS c" in query:
            return relation
        if "FROM pg_catalog.pg_attribute AS a" in query:
            return columns
        if any(
            marker in query
            for marker in (
                "FROM pg_catalog.pg_constraint",
                "FROM pg_catalog.pg_inherits",
                "FROM pg_catalog.pg_index",
            )
        ):
            return pd.DataFrame()
        raise AssertionError(query)

    monkeypatch.setattr(gp_ddl, "read_gp_partition_clause", lambda *args, **kwargs: "")
    monkeypatch.setattr(gp_ddl, "read_gp_distribution_clause", lambda *args, **kwargs: "")
    ddl = gp_ddl.extract_greenplum_catalog_ddl("gp", "public.events", read_sql=read_sql)
    assert ddl == 'CREATE TABLE "public"."events" (\n    "id" integer\n);'

    assert (
        read_partition_clause(
            "gp",
            "1",
            read_sql=lambda *_args: pd.DataFrame(
                [{"has_partkeydef": False, "has_partition_def": False}]
            ),
        )
        == ""
    )
    assert gp_ddl.format_gp_partition_clause(None) == ""
    with pytest.raises(ValueError, match="No DDL"):
        gp_ddl.first_result_value(pd.DataFrame(), "events")
    with pytest.raises(ValueError, match="No DDL"):
        gp_ddl.first_result_value(pd.DataFrame({"ddl": [pd.NA]}), "events")
    assert gp_ddl.parse_pg_array_text("   ") == []
    assert gp_ddl.parse_attrnums(None) == []


def test_gp_execute_values_import_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, str, Any, int]] = []

    def fake_execute_values(cursor: Any, sql: str, rows: Any, *, page_size: int) -> str:
        calls.append((cursor, sql, rows, page_size))
        return "inserted"

    monkeypatch.setitem(
        sys.modules,
        "psycopg2.extras",
        SimpleNamespace(execute_values=fake_execute_values),
    )
    cursor = object()
    assert gp_insert.execute_values(cursor, "INSERT", [(1,)], 10) == "inserted"
    assert calls == [(cursor, "INSERT", [(1,)], 10)]

    real_import = builtins.__import__

    def reject_psycopg2(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "psycopg2.extras":
            message = "missing"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_psycopg2)
    with pytest.raises(ImportError, match="required for Greenplum"):
        gp_insert.execute_values(cursor, "INSERT", [(1,)], 10)
