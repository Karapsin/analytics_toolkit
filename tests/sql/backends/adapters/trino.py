from __future__ import annotations

from tests.sql._support.adapters import (
    Any,
    Decimal,
    SimpleNamespace,
    SourceColumn,
    TrinoRecordingCursor,
    date,
    datetime,
    get_backend_adapter,
    importlib,
    pd,
    pytest,
    trino_adapter_module,
)


def test_trino_adapter_execute_and_read_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("trino")
    messages: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )
    cursor = TrinoRecordingCursor()
    adapter.execute_sql(
        SimpleNamespace(cursor=lambda: cursor),
        "SELECT 1; SELECT 2",
        print_queries=False,
        gp_break_query=False,
        gp_commit_each_statement=False,
        progress=False,
    )
    assert cursor.executed == ["SELECT 1", "SELECT 2"]
    assert cursor.closed is True
    failing = TrinoRecordingCursor(fail_on="SELECT 2")
    with pytest.raises(RuntimeError, match="trino query failed"):
        adapter.execute_sql(
            SimpleNamespace(cursor=lambda: failing),
            "SELECT 1; SELECT 2",
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert failing.closed is True
    assert "Failed SQL:\nSELECT 2" in messages
    read_cursor = TrinoRecordingCursor()
    result = adapter.execute_read_sql(
        SimpleNamespace(cursor=lambda: read_cursor),
        ["SET SESSION x = 1", "SELECT 7"],
        print_queries=False,
        gp_break_query=False,
        gp_commit_each_statement=False,
        progress=False,
    )
    assert result.to_dict("records") == [{"answer": 7}]
    assert read_cursor.closed is True
    broken_read = TrinoRecordingCursor(fail_on="SELECT broken")
    with pytest.raises(RuntimeError, match="trino query failed"):
        adapter.execute_read_sql(
            SimpleNamespace(cursor=lambda: broken_read),
            ["SELECT broken"],
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert broken_read.closed is True
    assert "Failed SQL:\nSELECT broken" in messages


def test_trino_adapter_insert_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = get_backend_adapter("trino")
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        adapter,
        "_insert_dataframe_batch",
        lambda *args, **kwargs: calls.append(("frame", args, kwargs)),
    )
    monkeypatch.setattr(
        adapter,
        "_insert_rows",
        lambda *args, **kwargs: calls.append(("rows", args, kwargs)),
    )
    frame = pd.DataFrame({"id": [1]})
    common = {
        "target_column_types": {"id": "bigint"},
        "gp_insert_chunk_size": 99,
        "connection_type": "warehouse",
        "query_label": "load",
        "on_progress": None,
    }
    adapter.insert_dataframe_batch(object(), "target", frame, trino_insert_chunk_size=3, **common)
    adapter.insert_rows_batch(
        object(), "target", ["id"], [(1,)], trino_insert_chunk_size=4, **common
    )
    assert calls[0][2]["trino_insert_chunk_size"] == 3
    assert calls[1][2]["trino_insert_chunk_size"] == 4
    calls.clear()
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.backends.trino.insert"),
        "insert_rows",
        lambda *args, **kwargs: calls.append(("module", args, kwargs)),
    )
    adapter = trino_adapter_module.TrinoAdapter()
    adapter._insert_dataframe_batch(object(), "target", frame)
    assert calls[0][1][3] == pd.Index(["id"])
    assert calls[0][1][4] == [(1,)]


def test_trino_adapter_partition_template_validation() -> None:
    adapter = get_backend_adapter("trino")
    with pytest.raises(ValueError, match="requires upsert_partition_drop"):
        adapter.build_drop_upsert_partition_sqls(
            "target", partition_column="day", partition_values=[date(2026, 1, 2)]
        )
    with pytest.raises(ValueError, match="unsupported placeholder"):
        trino_adapter_module._validate_trino_partition_drop_template(
            "ALTER TABLE {table} DROP {unknown}"
        )
    with pytest.raises(ValueError, match="must contain placeholders"):
        trino_adapter_module._validate_trino_partition_drop_template(
            "ALTER TABLE {table} DROP {partition_column}"
        )
    template = "ALTER TABLE {table} DROP ({partition_column} = {partition_value})"
    assert (
        "<affected partition value>"
        in adapter.build_drop_upsert_partition_sqls(
            "target",
            partition_column="day",
            partition_values=None,
            trino_partition_drop_sql_template=template,
        )[0]
    )


def test_trino_adapter_query_states_and_properties() -> None:
    adapter = get_backend_adapter("trino")
    assert "system.runtime.queries" in adapter.running_query_ids_sql()
    queries = adapter.show_queries_sqls(user="O'Reilly", states=["active", "finished", "failed"])
    assert [query["history"] for query in queries] == [False, True]
    assert "\"user\" = 'O''Reilly'" in queries[0]["sql"]
    assert "state in ('FINISHED', 'FAILED')" in queries[1]["sql"]
    assert adapter.show_queries_sqls(user=None, states=[]) == []
    with pytest.raises(ValueError, match="Unsupported Trino history state"):
        trino_adapter_module._trino_history_state("cancelled")
    properties = trino_adapter_module._build_trino_table_properties(
        partition_by="event_date", order_by=["id", "created_at"]
    )
    assert "partitioning = ARRAY['event_date']" in properties
    assert "sorted_by = ARRAY['id', 'created_at']" in properties
    with pytest.raises(ValueError, match="must not be empty"):
        trino_adapter_module._normalize_trino_property_entries([], "order_by")
    with pytest.raises(ValueError, match="duplicate"):
        trino_adapter_module._normalize_trino_property_entries(["id", "id"], "order_by")


def test_trino_adapter_schema_merge_and_upsert_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("trino")
    with pytest.raises(ValueError, match="positive integer"):
        adapter.build_dataframe_batch_insert_sql("target", ["id"], row_count=0)
    expected = [SourceColumn("id", "bigint")]
    source_schema = importlib.import_module("analytics_toolkit.sql.backends.source_schema")
    monkeypatch.setattr(
        source_schema,
        "inspect_dbapi_source_schema",
        lambda *_args, **_kwargs: expected,
    )
    assert adapter.inspect_source_query_schema(object(), "SELECT id") == expected
    with pytest.raises(ValueError, match="partition_column and final_stage_table"):
        adapter.build_upsert_stage_sqls("target", "stage", columns=["id"], key_columns=["id"])
    with pytest.raises(ValueError, match="partition_column and final_stage_table"):
        adapter.build_upsert_stage_placeholder_sqls("target", "stage", key_columns=["id"])
    merge = adapter._build_merge_sql("target", "stage", columns=["id", "value"], key_columns=["id"])
    placeholder = adapter._build_merge_placeholder_sql("target", "stage", key_columns=["id"])
    null_safe = (
        'target_dst."id" = stage_src."id" OR (target_dst."id" IS NULL AND stage_src."id" IS NULL)'
    )
    assert null_safe in merge
    assert null_safe in placeholder


@pytest.mark.parametrize(
    ("native_type", "expected"),
    [
        ("varbinary", "VARBINARY"),
        ("boolean", "BOOLEAN"),
        ("int8", "TINYINT"),
        ("int16", "SMALLINT"),
        ("integer", "INTEGER"),
        ("uint32", "BIGINT"),
        ("uint64", "DECIMAL(20, 0)"),
        ("int64", "BIGINT"),
        ("real", "REAL"),
        ("double", "DOUBLE"),
        ("numeric(12, 3)", "DECIMAL(12, 3)"),
        ("date", "DATE"),
        ("timestamp with time zone", "TIMESTAMP WITH TIME ZONE"),
        ("timestamp", "TIMESTAMP"),
        ("uuid", "UUID"),
        ("text", "VARCHAR"),
    ],
)
def test_trino_adapter_source_type_mapping(native_type: str, expected: str) -> None:
    assert (
        get_backend_adapter("trino").map_source_type_to_target(SourceColumn("value", native_type))
        == expected
    )


def test_trino_materialization_command_drains_results_before_close() -> None:
    events: list[str] = []

    class Cursor:
        def execute(self, sql: str) -> None:
            events.append(f"execute:{sql}")

        def fetchall(self) -> list[object]:
            events.append("fetchall")
            return []

        def close(self) -> None:
            events.append("close")

    get_backend_adapter("trino").execute_materialization_command(
        SimpleNamespace(cursor=Cursor),
        "CREATE TABLE snapshot AS SELECT 1",
    )

    assert events == [
        "execute:CREATE TABLE snapshot AS SELECT 1",
        "fetchall",
        "close",
    ]


def test_trino_parquet_stage_helpers_are_adapter_owned() -> None:
    adapter = get_backend_adapter("trino")
    create_sql = adapter.build_parquet_stage_table_sql(
        "hive.tmp.stage",
        {
            "id": "BIGINT",
            "amount": "DECIMAL(3, 2)",
            "created_at": "TIMESTAMP(3)",
            "event_ts": "TIMESTAMP(6) WITH TIME ZONE",
            "row_uuid": "UUID",
            "label": "VARCHAR",
        },
        "s3://bucket/stage/target's/",
        query_label="load-parquet",
    )

    assert create_sql == (
        "/* analytics_toolkit query_label=load-parquet */\n"
        'CREATE TABLE hive.tmp.stage ("id" BIGINT, "amount" DECIMAL(3, 2), '
        '"created_at" TIMESTAMP(6), "event_ts" VARCHAR, "row_uuid" VARCHAR, '
        '"label" VARCHAR) '
        "WITH (format = 'PARQUET', "
        "external_location = 's3://bucket/stage/target''s/')"
    )
    assert adapter.parquet_stage_target_table_base("catalog.schema.target") == "target"

    batch = SimpleNamespace(
        columns=[
            "flag",
            "id",
            "ratio",
            "amount",
            "created_at",
            "event_dt",
            "payload",
            "label",
            "empty",
        ],
        rows=[
            (
                True,
                7,
                1.25,
                Decimal("1.23"),
                datetime(2026, 1, 2, 3, 4, 5),
                date(2026, 1, 2),
                b"x",
                "ok",
                None,
            )
        ],
    )
    assert adapter.infer_parquet_stage_column_types_from_rows(batch) == {
        "flag": "BOOLEAN",
        "id": "BIGINT",
        "ratio": "DOUBLE",
        "amount": "DECIMAL(3, 2)",
        "created_at": "TIMESTAMP",
        "event_dt": "DATE",
        "payload": "VARBINARY",
        "label": "VARCHAR",
        "empty": "VARCHAR",
    }
