from __future__ import annotations

from tests.sql._support.adapters import (
    FakeDbapiConnection,
    RecordingClickHouseClient,
    execute_read_module,
    execute_sql_module,
    get_backend_adapter,
    inspect,
    load_sql_table_module,
    pd,
    pytest,
    read_sql_module,
    sql_module,
)


def test_backend_adapters_execute_operations_like_existing_table_ops() -> None:
    gp_connection = FakeDbapiConnection(rows=[(5,)])
    get_backend_adapter("gp").clear_table(gp_connection, "schema.target")
    assert gp_connection.executed == ["TRUNCATE TABLE schema.target"]
    assert gp_connection.commit_calls == 1

    trino_connection = FakeDbapiConnection(rows=[(7,)])
    assert (
        get_backend_adapter("trino").count_table_rows(
            trino_connection,
            "schema.target",
        )
        == 7
    )
    assert trino_connection.executed == ["SELECT COUNT(*) FROM schema.target"]
    assert trino_connection.commit_calls == 0

    ch_client = RecordingClickHouseClient()
    get_backend_adapter("ch").drop_table(
        ch_client,
        "db.target",
        ch_cluster="{cluster}",
    )
    assert ch_client.commands == [
        (
            "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'",
            {
                "distributed_ddl_task_timeout": 0,
                "distributed_ddl_output_mode": "none",
            },
        )
    ]
    assert get_backend_adapter("ch").count_table_rows(ch_client, "db.target") == 9
    assert ch_client.queries[-1] == "SELECT count() FROM db.target"


def test_backend_adapters_execute_validation_queries_per_backend() -> None:
    gp_connection = FakeDbapiConnection(rows=[(1,)])
    assert get_backend_adapter("gp").stage_has_duplicate_keys(
        gp_connection,
        "schema.stage",
        ["id"],
    )
    assert gp_connection.executed == [
        'SELECT 1 FROM schema.stage GROUP BY "id" HAVING COUNT(*) > 1 LIMIT 1'
    ]

    ch_client = RecordingClickHouseClient()
    assert (
        get_backend_adapter("ch").stage_keys_overlap_target(
            ch_client,
            "db.stage",
            "db.target",
            ["id"],
        )
        is False
    )
    assert ch_client.queries[-1] == (
        "SELECT 1 FROM db.stage AS stage_src "
        "INNER JOIN db.target AS target_dst ON "
        "(stage_src.`id` = target_dst.`id` "
        "OR (stage_src.`id` IS NULL AND target_dst.`id` IS NULL)) "
        "LIMIT 1"
    )


def test_backend_adapters_own_analyze_support_policy() -> None:
    assert get_backend_adapter("gp").should_analyze_table() is True
    assert get_backend_adapter("trino").should_analyze_table() is True
    assert get_backend_adapter("ch").should_analyze_table() is False


def test_backend_adapters_read_dataframes_for_dbapi_and_clickhouse() -> None:
    gp_connection = FakeDbapiConnection(
        rows=[(1, "ok")],
        description=[("id",), ("label",)],
    )
    printed: list[tuple[str, bool]] = []

    gp_result = get_backend_adapter("gp").read_dataframe(
        gp_connection,
        "select id, label",
        print_queries=True,
        print_query=lambda query, enabled: printed.append((query, enabled)),
        read_dbapi_query=read_sql_module._read_dbapi_query,
    )

    pd.testing.assert_frame_equal(
        gp_result,
        pd.DataFrame(
            {
                "id": pd.array([1], dtype="Int64"),
                "label": pd.array(["ok"], dtype="string"),
            }
        ),
    )
    assert printed == [("select id, label", True)]
    assert gp_connection.executed == ["select id, label"]

    class ReadClickHouseClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query(self, sql: str, *, column_oriented: bool) -> object:
            assert column_oriented is True
            self.queries.append(sql)
            return type(
                "QueryResult",
                (),
                {
                    "column_names": ("value",),
                    "result_columns": ([2],),
                    "row_count": 1,
                },
            )()

    ch_client = ReadClickHouseClient()
    ch_result = get_backend_adapter("ch").read_dataframe(
        ch_client,
        "select value",
        print_queries=False,
        print_query=lambda query, enabled: printed.append((query, enabled)),
        read_dbapi_query=lambda connection, query: pytest.fail("ClickHouse should use query"),
    )

    pd.testing.assert_frame_equal(
        ch_result,
        pd.DataFrame({"value": pd.array([2], dtype="Int64")}),
    )
    assert ch_client.queries == ["select value"]


def test_sql_backend_dispatch_uses_adapter_boundary() -> None:
    dispatch_functions = [
        read_sql_module._read_backend,
        execute_sql_module._execute_backend,
        execute_read_module._execute_read_backend,
        load_sql_table_module._insert_batch_backend,
        load_sql_table_module._insert_rows_backend,
    ]
    for function in dispatch_functions:
        assert "globals()[" not in inspect.getsource(function)

    assert "get_backend_adapter" in inspect.getsource(read_sql_module._read_backend)
    assert "get_backend_adapter" in inspect.getsource(execute_sql_module._execute_backend)
    assert "get_backend_adapter" in inspect.getsource(execute_read_module._execute_read_backend)
    assert not hasattr(read_sql_module, "_READ_BACKENDS")
    assert not hasattr(execute_sql_module, "_EXECUTE_BACKENDS")
    assert not hasattr(execute_read_module, "_EXECUTE_READ_BACKENDS")
    assert not hasattr(load_sql_table_module, "_BATCH_INSERT_BACKENDS")
    assert not hasattr(load_sql_table_module, "_ROW_INSERT_BACKENDS")
    assert "get_backend_adapter" in inspect.getsource(load_sql_table_module._insert_batch_backend)
    assert "get_backend_adapter" in inspect.getsource(load_sql_table_module._insert_rows_backend)


def test_sql_public_api_exports_are_stable() -> None:
    public_names = {
        "async_sql",
        "ch_reconfigure_table",
        "create_sql_table",
        "drop_partitions",
        "drop_tables",
        "execute",
        "execute_read",
        "load_df",
        "parallel_sql",
        "read",
        "table_info",
        "transfer",
    }

    for name in public_names:
        assert name in sql_module.__all__
        assert callable(getattr(sql_module, name))

    assert list(inspect.signature(sql_module.load_df).parameters)[:3] == [
        "db_key",
        "destination_table",
        "df",
    ]
    assert list(inspect.signature(sql_module.transfer).parameters)[:4] == [
        "from_db",
        "to_db",
        "from_sql",
        "to_table",
    ]
    assert "format_plan" in sql_module.__all__
    assert "SqlTableInfo" in sql_module.__all__
    assert "ch_create_table_as" not in sql_module.__all__
    assert not hasattr(sql_module, "ch_create_table_as")
    assert "ch_full_table_move" not in sql_module.__all__
    assert not hasattr(sql_module, "ch_full_table_move")
    assert "create_table_from_sql" not in sql_module.__all__
    assert not hasattr(sql_module, "create_table_from_sql")
    assert "execute_sql" not in sql_module.__all__
    assert "read_sql" not in sql_module.__all__
    assert "drop_table" not in sql_module.__all__
    assert not hasattr(sql_module, "drop_table")
    assert "drop_paritions" not in sql_module.__all__
    assert not hasattr(sql_module, "drop_paritions")
    assert "drop_many_partitions" not in sql_module.__all__
    assert not hasattr(sql_module, "drop_many_partitions")
    assert "transfer_table" not in sql_module.__all__


def test_sql_public_api_functions_are_timed() -> None:
    for name in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES:
        assert getattr(getattr(sql_module, name), "__sql_public_timing__", False)

    assert callable(sql_module.execute)
    assert callable(sql_module.read)
    assert callable(sql_module.transfer)
    assert not hasattr(sql_module, "execute_sql")
    assert not hasattr(sql_module, "read_sql")
    assert not hasattr(sql_module, "transfer_table")
