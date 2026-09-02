from __future__ import annotations

from tests.sql._support.adapters import (
    Any,
    Decimal,
    FakeClickHouseResult,
    RecordingClickHouseClient,
    SimpleNamespace,
    StageFinalizationRequest,
    StageTargetTableRequest,
    TargetWriteModeRequest,
    UnsupportedConnectionTypeError,
    ch_adapter_module,
    ch_ddl_backend_module,
    ch_insert_backend_module,
    ch_lifecycle_backend_module,
    ch_lifecycle_module,
    get_backend_adapter,
    importlib,
    pd,
    pytest,
)


def test_clickhouse_adapter_execute_and_read_failures_report_last_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    messages: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    command_error = RuntimeError("command failed")
    read_error = RuntimeError("read failed")

    class FailingClient:
        def command(self, sql: str) -> None:
            if sql == "SELECT 2":
                raise command_error

        def query(self, sql: str, *, column_oriented: bool) -> pd.DataFrame:
            assert column_oriented is True
            raise read_error

    with pytest.raises(RuntimeError, match="command failed"):
        adapter.execute_sql(
            FailingClient(),
            "SELECT 1; SELECT 2",
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    with pytest.raises(RuntimeError, match="read failed"):
        adapter.execute_read_sql(
            FailingClient(),
            ["SELECT broken"],
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert "Failed SQL:\nSELECT 2" in messages
    assert "Failed SQL:\nSELECT broken" in messages


def test_clickhouse_adapter_maintenance_delete_and_progress_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    client = RecordingClickHouseClient()
    adapter.truncate_table(client, "analytics.events", ch_cluster="cluster one")
    assert client.commands[-1][0] == (
        "TRUNCATE TABLE IF EXISTS analytics.events ON CLUSTER 'cluster one'"
    )
    with pytest.raises(UnsupportedConnectionTypeError, match="does not support ANALYZE"):
        adapter.analyze_table_sql("analytics.events")
    assert adapter.analyze_table(client, "analytics.events") is None

    delete_sql = adapter._build_delete_matching_stage_sql(
        "analytics.events",
        "analytics.events_stage",
        ["id", "group"],
        ch_cluster="{cluster}",
    )
    assert "tuple(isNull(`id`), ifNull(toString(`id`), '')" in delete_sql
    assert "SELECT tuple(isNull(`id`)" in delete_sql

    progress: list[int] = []
    frame = pd.DataFrame({"id": [1, 2]})
    client.insert_df = lambda **kwargs: None  # type: ignore[attr-defined]
    client.insert = lambda **kwargs: None  # type: ignore[attr-defined]
    adapter._insert_dataframe_batch(client, "events", frame, progress.append)
    adapter._insert_rows(
        client,
        "events",
        ["id"],
        [(1,), (2,), (3,)],
        {"id": "UInt64"},
        progress.append,
    )
    adapter._insert_rows(client, "events", ["id"], [(4,)], None)
    assert progress == [2, 3]


def test_clickhouse_adapter_stage_finalization_and_existing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        adapter,
        "ensure_distributed_target_pair",
        lambda *args, **kwargs: calls.append(("ensure", kwargs.get("target_exists"))),
    )
    monkeypatch.setattr(
        adapter,
        "insert_from_table",
        lambda *args, **kwargs: calls.append(("insert", args[2])),
    )
    request = StageTargetTableRequest(
        connection=object(),
        target_table="events",
        sample_batch=pd.DataFrame({"id": [1]}),
        target_column_types={"id": "UInt64"},
        gp_distributed_by_key=None,
        partition_by=None,
        order_by=["id"],
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        query_label=None,
        connection_key="ch",
    )
    assert adapter.ensure_stage_target_table(request) is True
    assert calls == [("ensure", False)]

    calls.clear()
    adapter.finalize_stage_table(
        StageFinalizationRequest(
            connection=object(),
            stage_table="events_stage",
            target_table="events",
            replace_target_table=False,
            target_exists=False,
            sample_batch=pd.DataFrame({"id": [1]}),
            target_column_types={"id": "UInt64"},
            write_mode="upsert",
        )
    )
    assert calls == [("ensure", False), ("insert", "events_stage")]

    metadata_adapter = ch_adapter_module.ClickHouseAdapter()
    monkeypatch.setattr(
        metadata_adapter,
        "get_table_column_types",
        lambda *args, **kwargs: {"stored_id": "UInt32"},
    )
    create_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.ddl.api"),
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: create_calls.append(kwargs),
    )
    metadata_adapter.ensure_distributed_target_pair(
        object(),
        "events",
        pd.DataFrame({"id": [1]}),
        target_exists=True,
        target_column_types=None,
        insert_column_types=None,
        gp_distributed_by_key=None,
        partition_by=None,
        order_by=None,
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="rand()",
        query_label=None,
        connection_key="ch",
    )
    assert create_calls[0]["table_schema"] == {"stored_id": "UInt32"}


def test_clickhouse_adapter_upsert_partition_and_identifier_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    monkeypatch.setattr(adapter, "ensure_distributed_target_pair", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        adapter, "fetch_upsert_partition_values", lambda *args, **kwargs: ["2026-01"]
    )
    monkeypatch.setattr(
        adapter, "build_upsert_stage_sqls", lambda *args, **kwargs: ["ALTER TABLE x"]
    )
    commands: list[str] = []
    monkeypatch.setattr(adapter, "execute_command", lambda _connection, sql: commands.append(sql))
    adapter.finalize_stage_table(
        StageFinalizationRequest(
            connection=object(),
            stage_table="events_stage",
            target_table="events",
            replace_target_table=False,
            target_exists=True,
            sample_batch=pd.DataFrame({"id": [1], "month": ["2026-01"]}),
            target_column_types={"id": "UInt64", "month": "String"},
            write_mode="upsert",
            upsert_partition_column="month",
        )
    )
    assert commands == ["ALTER TABLE x"]
    with pytest.raises(ValueError, match="upsert_partition_column is required"):
        adapter.finalize_stage_table(
            StageFinalizationRequest(
                connection=object(),
                stage_table="stage",
                target_table="events",
                replace_target_table=False,
                target_exists=True,
                sample_batch=pd.DataFrame({"id": [1]}),
                write_mode="upsert",
            )
        )
    assert "system.processes" in adapter.running_query_ids_sql()
    assert adapter.cancel_status(pd.DataFrame()) == (True, "submitted")
    assert ch_adapter_module.ch_cluster_clause(None) == ""
    with pytest.raises(ValueError, match="must not be empty"):
        ch_adapter_module.ch_cluster_clause("  ")
    assert ch_adapter_module.format_ch_cluster_name("`quoted`") == "`quoted`"
    assert ch_adapter_module.is_simple_identifier("") is False


@pytest.mark.parametrize(
    "case",
    [
        ("append", True, False, True, True),
        ("append", False, False, True, False),
        ("truncate_insert", True, True, True, True),
        ("truncate_insert", False, True, False, False),
        ("replace", True, True, True, False),
        ("truncate_insert", True, False, True, True),
        ("truncate_insert", False, False, False, False),
        ("replace", True, False, True, False),
    ],
)
def test_clickhouse_adapter_write_mode_matrix(
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, bool, bool, bool, bool],
) -> None:
    write_mode, target_exists, only_shard, drop_missing, expected = case
    adapter = get_backend_adapter("ch")
    events: list[str] = []
    monkeypatch.setattr(adapter, "clear_table", lambda *args, **kwargs: events.append("clear"))
    monkeypatch.setattr(adapter, "drop_table", lambda *args, **kwargs: events.append("drop"))
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "truncate_ch_distributed_table_pair",
        lambda *args, **kwargs: events.append("truncate_pair"),
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "drop_ch_distributed_table_pair",
        lambda *args, **kwargs: events.append("drop_pair"),
    )
    result = adapter.apply_target_write_mode(
        TargetWriteModeRequest(
            connection=object(),
            table_name="analytics.events",
            write_mode=write_mode,
            target_exists=target_exists,
            replace_existing_non_ch="clear",
            drop_missing_ch_truncate_target=drop_missing,
            ch_only_shard=only_shard,
        )
    )
    assert result is expected
    if write_mode == "replace":
        assert events == (["drop"] if only_shard else ["drop_pair"])


def test_clickhouse_ddl_expression_cluster_and_uuid_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ch_ddl_backend_module._normalize_ch_expression(" toYYYYMM(day) ", "x") == (
        "toYYYYMM(day)"
    )
    assert ch_ddl_backend_module._normalize_ch_expression(["day"], "x") == "`day`"
    with pytest.raises(ValueError, match="must not be empty when provided"):
        ch_ddl_backend_module._normalize_ch_expression([], "x")
    with pytest.raises(ValueError, match="duplicate"):
        ch_ddl_backend_module._normalize_ch_expression(["day", "day"], "x")
    with pytest.raises(ValueError, match="must not be empty"):
        ch_ddl_backend_module._normalize_non_empty_string("  ", "x")

    assert ch_ddl_backend_module._format_ch_cluster_name("  ") == ""
    assert ch_ddl_backend_module._format_ch_cluster_name("`core-prod`") == "`core-prod`"
    assert ch_ddl_backend_module._format_ch_cluster_name("core-prod") == "'core-prod'"
    assert ch_ddl_backend_module._is_simple_identifier("") is False
    assert ch_ddl_backend_module._is_simple_identifier("9core") is False
    assert ch_ddl_backend_module._is_simple_identifier("_core9") is True

    commands: list[str] = []
    monkeypatch.setattr(
        get_backend_adapter("ch"),
        "execute_command",
        lambda _connection, sql: commands.append(sql),
    )
    ch_ddl_backend_module._execute_ch_command(object(), "SELECT 1")
    assert commands == ["SELECT 1"]

    unchanged = [
        "CREATE TABLE x ON CLUSTER core\n(id UInt64) ENGINE = ReplicatedMergeTree",
        "CREATE TABLE x\n(id UInt64) ENGINE = MergeTree",
        "CREATE TABLE x\nUUID 'fixed'\n(id UInt64) ENGINE = ReplicatedMergeTree",
        "CREATE TABLE x ENGINE = ReplicatedMergeTree",
    ]
    for sql in unchanged:
        assert ch_ddl_backend_module.add_explicit_ch_uuid_to_local_replicated_create(sql) == sql
    monkeypatch.setattr(ch_ddl_backend_module.uuid, "uuid4", lambda: "fixed-uuid")
    rewritten = ch_ddl_backend_module.add_explicit_ch_uuid_to_local_replicated_create(
        "CREATE TABLE x\n(id UInt64) ENGINE = ReplicatedMergeTree"
    )
    assert "UUID 'fixed-uuid'" in rewritten


def test_clickhouse_insert_legacy_collections_types_and_null_edges() -> None:
    class LegacyFrame:
        def applymap(self, function: Any) -> pd.DataFrame:
            return pd.DataFrame({"value": [function(Decimal("1.25")), function(None)]})

    normalized = ch_insert_backend_module.normalize_batch(
        LegacyFrame()  # type: ignore[arg-type]
    )
    assert normalized.to_dict("records") == [{"value": 1.25}, {"value": None}]
    assert ch_insert_backend_module.normalize_scalar(
        [Decimal("1.5"), (Decimal("2.5"),), {Decimal("3.5"): Decimal("4.5")}]
    ) == [1.5, (2.5,), {3.5: 4.5}]
    assert ch_insert_backend_module.column_type_names(["a"], None) is None
    assert ch_insert_backend_module.column_type_names(
        ["a", "b"], {"a": "UInt8", "b": "String"}
    ) == ["UInt8", "String"]
    with pytest.raises(ValueError, match="Missing explicit SQL type for column 'b'"):
        ch_insert_backend_module.column_type_names(["a", "b"], {"a": "UInt8"})
    assert ch_insert_backend_module.normalize_row(([1, 2], None)) == ([1, 2], None)
    assert ch_insert_backend_module.normalize_typed_row(
        ["id", "amount", "payload"],
        [1.0, 1.25, {"x": "я"}],
        {"id": "Nullable(Int64)", "amount": "Decimal(10,2)", "payload": "String"},
    ) == (1, Decimal("1.25"), '{"x":"я"}')
    assert ch_insert_backend_module.normalize_typed_row(
        ["raw", "mutable"],
        [memoryview(b"\x00\xff"), bytearray(b"\x01\x80")],
        {"raw": "Nullable(String)", "mutable": "LowCardinality(String)"},
    ) == (b"\x00\xff", b"\x01\x80")
    assert ch_insert_backend_module.normalize_typed_row(["value"], ["plain"], None) == ("plain",)
    assert ch_insert_backend_module.normalize_rows(
        ["value"],
        [[True]],
        {"value": "Bool"},
    ) == [(True,)]
    inserted: list[dict[str, Any]] = []
    native = SimpleNamespace(
        is_native_transport=True,
        insert=lambda **kwargs: inserted.append(kwargs),
    )
    ch_insert_backend_module.insert_dataframe_batch(
        native,
        "events",
        pd.DataFrame({"id": [1.0, None]}),
        {"id": "Nullable(Int64)"},
    )
    assert inserted[0]["data"] == [(1,), (None,)]
    http_inserts: list[dict[str, Any]] = []
    http = SimpleNamespace(
        is_native_transport=False,
        insert_df=lambda **kwargs: http_inserts.append(kwargs),
    )
    ch_insert_backend_module.insert_dataframe_batch(
        http,
        "events",
        pd.DataFrame(
            {
                "raw": [memoryview(b"\x00\xff")],
                "mutable": [bytearray(b"\x01\x80")],
            }
        ),
        {"raw": "String", "mutable": "Nullable(String)"},
    )
    assert http_inserts[0]["df"].to_dict("records") == [
        {"raw": b"\x00\xff", "mutable": b"\x01\x80"}
    ]
    assert ch_insert_backend_module._is_null_like(None) is True
    assert ch_insert_backend_module._is_null_like([1, 2]) is False


def test_clickhouse_lifecycle_builds_distributed_pair_sql_in_order() -> None:
    assert ch_lifecycle_module.build_drop_ch_distributed_table_pair_sqls(
        "db.target",
        ch_cluster="{cluster}",
    ) == [
        "DROP TABLE IF EXISTS db.target",
        "DROP TABLE IF EXISTS db.target_shard",
        "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'",
        "DROP TABLE IF EXISTS db.target_shard ON CLUSTER '{cluster}'",
    ]

    assert ch_lifecycle_module.build_truncate_ch_distributed_table_pair_sqls(
        "db.target",
        ch_cluster="analytics",
    ) == [
        "TRUNCATE TABLE IF EXISTS db.target_shard ON CLUSTER analytics",
        "TRUNCATE TABLE IF EXISTS db.target",
    ]

    create_sqls = ch_lifecycle_module.build_create_ch_distributed_table_pair_sqls(
        table_name="db.target",
        joined_columns="`id` UInt64",
        partition_by=["id"],
        order_by=["id"],
        ch_cluster="{cluster}",
        ch_sharding_key="cityHash64(id)",
    )
    assert len(create_sqls) == 4
    assert create_sqls[0].startswith("CREATE TABLE IF NOT EXISTS db.target_shard")
    assert "ON CLUSTER '{cluster}'" in create_sqls[0]
    assert create_sqls[1].startswith("CREATE TABLE IF NOT EXISTS db.target_shard")
    assert "ON CLUSTER" not in create_sqls[1]
    assert "UUID '" in create_sqls[1]
    assert create_sqls[2].startswith("CREATE TABLE IF NOT EXISTS db.target")
    assert "ON CLUSTER '{cluster}'" in create_sqls[2]
    assert create_sqls[3].startswith("CREATE TABLE IF NOT EXISTS db.target")
    assert "ON CLUSTER" not in create_sqls[3]


def test_clickhouse_lifecycle_concurrent_host_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = ch_lifecycle_backend_module.ch_distributed_table_pair("db.t")
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_configured_cluster_hosts",
        lambda *_: ["host-a"],
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_select_ch_hosts_for_local_drop",
        lambda *args, **kwargs: ["host-a"],
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_drop_ch_distributed_table_pair_on_host",
        lambda *args, **kwargs: "host-a: failed",
    )
    with pytest.raises(TimeoutError, match="host-a: failed"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_cluster_hosts(
            object(),
            pair,
            ch_cluster="core",
            query_label=None,
            per_host_drop_workers=1,
            per_host_connection_factory=lambda _host: object(),
        )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_drop_ch_distributed_table_pair_on_host",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("worker crashed")),
    )
    with pytest.raises(TimeoutError, match="worker crashed"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_cluster_hosts(
            object(),
            pair,
            ch_cluster="core",
            query_label=None,
            per_host_drop_workers=1,
            per_host_connection_factory=lambda _host: object(),
        )


def test_clickhouse_lifecycle_drop_leftovers_mentions_per_host_retry() -> None:
    class LeftoverClient(RecordingClickHouseClient):
        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(1,)])
            if "FROM system.clusters" in sql:
                return FakeClickHouseResult([(1,)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                return FakeClickHouseResult(
                    [("host-a", "db", "target_shard", "ReplicatedMergeTree")]
                )
            return FakeClickHouseResult([])

    with pytest.raises(TimeoutError) as exc_info:
        ch_lifecycle_module.drop_ch_distributed_table_pair(
            LeftoverClient(),
            "db.target",
            ch_cluster="{cluster}",
            wait_for_absence=True,
            wait_timeout_seconds=0,
            wait_poll_interval_seconds=0,
        )

    message = str(exc_info.value)
    assert "host-a: db.target_shard (ReplicatedMergeTree)" in message
    assert "ch_retry_per_host_drops=True" in message


def test_clickhouse_lifecycle_drop_modes_and_wait_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(ch_lifecycle_backend_module.build_drop_ch_table_sqls("db.t", None)) == 1
    executed: list[str] = []
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_execute_ch_sqls",
        lambda _connection, sqls: executed.extend(sqls),
    )
    local_waits: list[str] = []
    cluster_waits: list[str] = []
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_table_absence",
        lambda _connection, table, **_kwargs: local_waits.append(table),
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_table_absence_on_cluster",
        lambda _connection, table, **_kwargs: cluster_waits.append(table),
    )
    ch_lifecycle_backend_module.drop_ch_table(
        object(), "db.local", ch_cluster=None, wait_for_absence=True
    )
    ch_lifecycle_backend_module.drop_ch_table(
        object(), "db.clustered", ch_cluster="core", wait_for_absence=True
    )
    assert local_waits == ["db.local"]
    assert cluster_waits == ["db.clustered"]
    ch_lifecycle_backend_module.truncate_ch_distributed_table_pair(object(), "db.t")
    ch_lifecycle_backend_module.create_ch_distributed_table_pair(
        object(), table_name="db.t", joined_columns="id UInt64", wait_for_table=False
    )
    waited: list[str] = []
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_distributed_table_pair",
        lambda _connection, table, **_kwargs: waited.append(table),
    )
    ch_lifecycle_backend_module.create_ch_distributed_table_pair(
        object(), table_name="db.t", joined_columns="id UInt64", wait_for_table=True
    )
    assert waited == ["db.t"]


def test_clickhouse_lifecycle_executes_on_cluster_settings() -> None:
    client = RecordingClickHouseClient()

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        client,
        "db.target",
        ch_cluster="{cluster}",
    )

    assert client.commands[2] == (
        "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'",
        {
            "distributed_ddl_task_timeout": 0,
            "distributed_ddl_output_mode": "none",
        },
    )
    assert client.queries == []
