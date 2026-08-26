from __future__ import annotations

from tests.sql._support.adapters import (
    BACKEND_ADAPTERS,
    BACKEND_REGISTRY,
    FakeDbapiConnection,
    RecordingClickHouseClient,
    _SourceCountCursor,
    backend_source_count_module,
    get_backend_adapter,
    get_backend_names,
    pytest,
    table_basic_ops_module,
    table_ops_module,
)


def test_backend_adapter_insert_from_query_returns_backend_row_counts() -> None:
    class RowCountCursorConnection(FakeDbapiConnection):
        def __init__(self) -> None:
            super().__init__()
            self.insert_rowcount = 4

    gp_connection = RowCountCursorConnection()
    assert (
        get_backend_adapter("gp").insert_from_query(
            gp_connection,
            "schema.target",
            "select id from source",
            {"id": "BIGINT"},
        )
        == 4
    )
    assert gp_connection.commit_calls == 1

    ch_client = RecordingClickHouseClient()
    assert (
        get_backend_adapter("ch").insert_from_query(
            ch_client,
            "db.target",
            "select id from source",
            {"id": "Nullable(Int64)"},
        )
        == 3
    )
    assert ch_client.commands[-1][0] == (
        "INSERT INTO db.target (`id`) "
        "SELECT CAST(`id` AS Nullable(Int64)) AS `id` "
        "FROM (select id from source) AS source_query"
    )


def test_backend_adapter_registry_renders_existing_sql_shapes() -> None:
    expected_backends = set(get_backend_names())
    assert set(BACKEND_ADAPTERS) == expected_backends
    assert BACKEND_ADAPTERS is BACKEND_REGISTRY
    assert expected_backends == set(BACKEND_REGISTRY)

    assert get_backend_adapter("gp").clear_table_sqls("schema.target") == [
        "TRUNCATE TABLE schema.target"
    ]
    assert get_backend_adapter("trino").clear_table_sqls("schema.target") == [
        "DELETE FROM schema.target"
    ]
    assert get_backend_adapter("ch").clear_table_sqls("db.target") == [
        "TRUNCATE TABLE IF EXISTS db.target"
    ]
    assert (
        get_backend_adapter("ch").drop_table_sql(
            "db.target",
            ch_cluster="{cluster}",
        )
        == "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'"
    )
    assert (
        get_backend_adapter("gp").build_insert_from_table_sql(
            "schema.target",
            "schema.stage",
            {"id": "BIGINT", "amount": "NUMERIC(12, 2)"},
        )
        == 'INSERT INTO schema.target ("id", "amount") '
        'SELECT CAST("id" AS BIGINT) AS "id", '
        'CAST("amount" AS NUMERIC(12, 2)) AS "amount" FROM schema.stage'
    )
    assert (
        get_backend_adapter("ch").count_table_rows_sql("db.target")
        == "SELECT count() FROM db.target"
    )
    assert get_backend_adapter("trino").build_dataframe_batch_insert_sql(
        "schema.stage",
        ["id", "name"],
        row_count=2,
    ) == ('INSERT INTO schema.stage ("id", "name") VALUES (?, ?), (?, ?)')
    assert get_backend_adapter("gp").build_stage_duplicate_keys_sql(
        "schema.stage",
        ["id", "dt"],
    ) == ('SELECT 1 FROM schema.stage GROUP BY "id", "dt" HAVING COUNT(*) > 1 LIMIT 1')
    assert get_backend_adapter("ch").build_stage_target_key_overlap_sql(
        "db.stage",
        "db.target",
        ["id"],
    ) == (
        "SELECT 1 FROM db.stage AS stage_src "
        "INNER JOIN db.target AS target_dst ON "
        "(stage_src.`id` = target_dst.`id` "
        "OR (stage_src.`id` IS NULL AND target_dst.`id` IS NULL)) "
        "LIMIT 1"
    )
    assert get_backend_adapter("gp").quote_identifier('a"b') == '"a""b"'
    assert get_backend_adapter("trino").quote_identifier('a"b') == '"a""b"'
    assert get_backend_adapter("ch").quote_identifier("a`b") == "`a``b`"


def test_source_count_helpers_cover_cursor_shapes_and_labels() -> None:
    assert get_backend_adapter("gp").strip_query_semicolon(" SELECT 1;  ") == "SELECT 1"
    assert backend_source_count_module.fetch_first_row(
        _SourceCountCursor(fetchone=lambda: (7,))
    ) == (7,)
    assert backend_source_count_module.fetch_first_row(
        _SourceCountCursor(fetchall=lambda: [(8,), (9,)])
    ) == (8,)
    assert backend_source_count_module.fetch_first_row(_SourceCountCursor(fetchall=list)) is None
    assert backend_source_count_module.fetch_first_row(_SourceCountCursor(rows=[(10,)])) == (10,)
    assert backend_source_count_module.fetch_first_row(_SourceCountCursor(rows=[])) is None
    with pytest.raises(TypeError, match="Cursor must provide"):
        backend_source_count_module.fetch_first_row(object())

    assert get_backend_adapter("gp").build_source_count_sql(
        "SELECT * FROM source;",
        query_label="count */ safely",
    ) == (
        "/* analytics_toolkit query_label=count * / safely */\n"
        "SELECT COUNT(*) FROM (SELECT * FROM source) AS source_count_probe"
    )


def test_table_ops_compatibility_helpers_remain_importable() -> None:
    helper_names = {
        "build_analyze_table_sql",
        "build_clear_table_sqls",
        "build_count_table_rows_sql",
        "build_drop_ch_distributed_table_pair_sqls",
        "build_drop_table_sql",
        "build_insert_from_query_sql",
        "build_insert_from_table_sql",
        "clear_target_table",
        "count_table_rows",
        "drop_table",
        "finalize_stage_table",
        "get_table_column_types",
        "get_trino_table_column_types",
        "insert_from_query",
        "insert_from_table",
        "table_exists",
        "_build_typed_insert_select_sql",
        "_ch_cluster_clause",
        "_execute_ch_command",
        "_gp_table_exists",
        "_trino_table_exists",
    }

    for name in helper_names:
        assert callable(getattr(table_ops_module, name))


def test_table_ops_reexports_split_basic_helpers() -> None:
    helper_names = {
        "build_analyze_table_sql",
        "build_clear_table_sqls",
        "build_count_table_rows_sql",
        "build_drop_ch_distributed_table_pair_sqls",
        "build_drop_table_sql",
        "build_insert_from_query_sql",
        "build_insert_from_table_sql",
        "count_table_rows",
        "get_table_column_types",
        "get_trino_table_column_types",
        "insert_from_query",
        "insert_from_table",
        "quote_qualified_table_name",
        "split_trino_table_name",
        "table_exists",
        "_build_typed_insert_select_sql",
        "_ch_cluster_clause",
        "_execute_ch_command",
        "_gp_table_exists",
        "_trino_table_exists",
    }

    for name in helper_names:
        assert getattr(table_ops_module, name) is getattr(table_basic_ops_module, name)
