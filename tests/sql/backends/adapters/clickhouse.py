from __future__ import annotations

from analytics_toolkit import sql as toolkit_sql
from analytics_toolkit.sql.backends.ch import operations as ch_operations
from analytics_toolkit.sql.backends.ch.layout import ExistingClickHouseLayout
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table

from tests.sql._support.adapters import (
    Any,
    Decimal,
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
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope


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


def test_clickhouse_adapter_does_not_resubmit_a_command_after_type_error() -> None:
    submitted = []
    failure = TypeError("driver failed after accepting CREATE")

    class Client:
        def command(self, statement: str, **_kwargs: object) -> None:
            submitted.append(statement)
            raise failure

    statement = "CREATE TABLE db.stage ON CLUSTER core (id UInt8) ENGINE=Memory"
    with pytest.raises(TypeError) as error:
        get_backend_adapter("ch").execute_command(Client(), statement)
    assert error.value is failure
    assert submitted == [statement]


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
    assert create_calls == []  # Existing storage never provisions a policy-derived companion.


def test_clickhouse_adapter_upsert_partition_and_identifier_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(
        query=lambda text: SimpleNamespace(
            result_rows=[
                ("CREATE TABLE events (id UInt64, month String) ENGINE = MergeTree ORDER BY id",)
            ]
        )
    )
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
            connection=connection,
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
                connection=connection,
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
        ("replace", False, False, True, False),
        ("replace", False, True, True, False),
    ],
)
def test_clickhouse_adapter_write_mode_matrix(
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, bool, bool, bool, bool],
) -> None:
    write_mode, target_exists, only_shard, drop_missing, expected = case
    adapter = get_backend_adapter("ch")
    events: list[str] = []
    layout = ExistingClickHouseLayout(
        logical_table="analytics.events",
        physical_table="analytics.events" if only_shard else "analytics.events_shard",
        create_distributed_pair=not only_shard,
        shard_on_cluster=None if only_shard else "core",
        distributed_on_cluster=None if only_shard else "core",
        distributed_cluster=None if only_shard else "core",
        ddl="verified logical table",
        physical_ddl="verified physical table",
    )
    connection = SimpleNamespace(outage_inspect_existing_layout=lambda _table: layout)
    monkeypatch.setattr(adapter, "clear_table", lambda *args, **kwargs: events.append("clear"))
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "truncate_ch_distributed_table_pair",
        lambda *args, **kwargs: events.append("truncate_pair"),
    )

    def drop_verified(received_adapter, received_connection, table, **options):
        assert received_adapter is adapter
        assert received_connection is connection
        assert table == "analytics.events"
        assert options == {
            "connection_key": "ch",
            "ch_cluster": "{cluster}",
            "ch_wait_for_absence": True,
            "ch_retry_per_host_drops": True,
            "if_exists": True,
            "query_label": None,
        }
        events.append("drop_pair" if layout.create_distributed_pair else "drop")

    monkeypatch.setattr(ch_operations, "drop_table_with_options", drop_verified)
    result = adapter.apply_target_write_mode(
        TargetWriteModeRequest(
            connection=connection,
            table_name="analytics.events",
            write_mode=write_mode,
            target_exists=target_exists,
            replace_existing_non_ch="clear",
            drop_missing_ch_truncate_target=drop_missing,
            ch_only_shard=only_shard,
        )
    )
    assert result is expected
    if write_mode == "replace" and target_exists:
        assert events == (["drop"] if only_shard else ["drop_pair"])
    elif write_mode == "truncate_insert" and target_exists:
        assert events == (["clear"] if only_shard else ["truncate_pair"])
    else:
        assert events == []


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
    batch = pd.DataFrame({"value": pd.Series([Decimal("1.25"), None], dtype=object)})
    normalized = ch_insert_backend_module.normalize_batch(batch)
    assert normalized.to_dict("records") == [{"value": Decimal("1.25")}, {"value": None}]
    nested = [Decimal("1.5"), (Decimal("2.5"),), {Decimal("3.5"): Decimal("4.5")}]
    assert ch_insert_backend_module.normalize_scalar(nested) == nested
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
    ) == (
        1,
        Decimal("1.25"),
        '{"v":1,"backend":"","type":"","value":["map",[[["str","x"],["str","я"]]]]}',
    )
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


def test_clickhouse_lifecycle_drop_leftovers_mentions_per_host_retry(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections
) -> None:
    state = install_drop_scope(monkeypatch, write_sql_connections)
    state.delay_cluster_drops.add(("storage", "analytics.events_shard"))
    connection = toolkit_sql.get_sql_connection("ch")
    before = state.snapshot()
    try:
        with pytest.raises(TimeoutError) as exc_info:
            ch_lifecycle_module.drop_ch_distributed_table_pair(
                connection,
                "analytics.events",
                ch_cluster="storage",
                wait_for_absence=True,
                wait_timeout_seconds=0,
                wait_poll_interval_seconds=0,
                ch_retry_per_host_drops=False,
            )
    finally:
        connection.close()

    message = str(exc_info.value)
    assert state.host_endpoints[state.remote][1] in message
    assert "analytics.events_shard (MergeTree)" in message
    assert "ch_retry_per_host_drops=True" in message
    assert (
        state.snapshot()[state.coordinator]["analytics.events_shard"]
        == (before[state.coordinator]["analytics.events_shard"])
    )
    assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
    assert all(client.close_calls == 1 for client in state.opened)


def test_clickhouse_lifecycle_drop_modes_and_wait_routing(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections
) -> None:
    assert len(ch_lifecycle_backend_module.build_drop_ch_table_sqls("db.t", None)) == 1
    state = install_drop_scope(monkeypatch, write_sql_connections)
    for host, table in [
        (state.coordinator, "analytics.local_events"),
        (state.remote, "analytics.clustered_events"),
    ]:
        state._create(
            host,
            parse_create_table(
                f"CREATE TABLE {table} (id UInt64) ENGINE=MergeTree ORDER BY id", table
            ),
            [(7,), (8,)],
        )
    connection = toolkit_sql.get_sql_connection("ch")
    try:
        ch_lifecycle_backend_module.drop_ch_table(
            connection, "analytics.local_events", ch_cluster=None, wait_for_absence=True
        )
        ch_lifecycle_backend_module.drop_ch_table(
            connection,
            "analytics.clustered_events",
            ch_cluster="storage",
            wait_for_absence=True,
        )
    finally:
        connection.close()
    assert state.commands == [
        "DROP TABLE IF EXISTS analytics.local_events",
        "DROP TABLE IF EXISTS analytics.clustered_events ON CLUSTER storage",
    ]
    assert "analytics.local_events" not in state.host_tables[state.coordinator]
    assert "analytics.clustered_events" not in state.host_tables[state.remote]
    assert any(
        completed == 1 and strip_query_label(statement) == "EXISTS TABLE analytics.local_events"
        for _host, completed, statement in state.query_events
    )
    assert any(
        completed == 2
        and "clusterAllReplicas('storage', system, tables)" in statement
        and "name = 'clustered_events'" in statement
        for _host, completed, statement in state.query_events
    )
    assert all(client.close_calls == 1 for client in state.opened)
    executed: list[str] = []
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_execute_ch_sqls",
        lambda _connection, sqls: executed.extend(sqls),
    )
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


def test_clickhouse_lifecycle_executes_on_cluster_settings(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections
) -> None:
    state = install_drop_scope(monkeypatch, write_sql_connections)
    owner = toolkit_sql.get_sql_connection("ch")
    connection = state.opened[-1]
    before = state.snapshot()
    try:
        ch_lifecycle_module.drop_ch_distributed_table_pair(
            connection, "analytics.events", ch_cluster="storage"
        )
    finally:
        owner.close()

    assert list(zip(state.commands, state.command_settings)) == [
        ("DROP TABLE IF EXISTS analytics.events", None),
        (
            'DROP TABLE IF EXISTS "analytics"."events_shard" ON CLUSTER storage',
            {
                "distributed_ddl_task_timeout": 0,
                "distributed_ddl_output_mode": "none",
            },
        ),
    ]
    assert state.queries
    assert (
        state.snapshot()[state.coordinator]["analytics.events_shard"]
        == (before[state.coordinator]["analytics.events_shard"])
    )
    assert all(client.close_calls == 1 for client in state.opened)
