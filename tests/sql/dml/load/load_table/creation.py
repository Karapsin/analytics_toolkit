from __future__ import annotations

from tests.sql._support.load_table import (
    TEST_CH_SHARD_RELATION,
    TEST_CH_SHARD_TABLE,
    TEST_CH_TABLE,
    Decimal,
    FakeClickHouseClient,
    SimpleNamespace,
    ch_wait_module,
    create_sql_table_module,
    date,
    load_df_module,
    pd,
    pytest,
)


def test_build_create_table_sqls_clickhouse_only_shard_creates_local_target() -> None:
    batch = pd.DataFrame(
        {
            "min_month_use": [date(2024, 1, 1)],
            "month_date": [date(2024, 2, 1)],
            "users": [10],
        }
    )

    sqls = create_sql_table_module._build_create_table_sqls(
        backend="ch",
        table_name=TEST_CH_TABLE,
        df=batch,
        ch_distributed_table=True,
        ch_only_shard=True,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert len(sqls) == 1
    create_sql = sqls[0]
    assert create_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
    assert TEST_CH_SHARD_TABLE not in create_sql
    assert "ON CLUSTER" not in create_sql
    assert "ENGINE = Distributed(" not in create_sql
    assert "ENGINE = ReplicatedMergeTree" in create_sql
    assert "PARTITION BY `month_date`" in create_sql
    assert "ORDER BY (`month_date`, `min_month_use`)" in create_sql


def test_build_create_table_sqls_creates_clickhouse_distributed_pair() -> None:
    batch = pd.DataFrame(
        {
            "min_month_use": [date(2024, 1, 1)],
            "month_date": [date(2024, 2, 1)],
            "users": [10],
        }
    )

    sqls = create_sql_table_module._build_create_table_sqls(
        backend="ch",
        table_name=TEST_CH_TABLE,
        df=batch,
        ch_distributed_table=True,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert len(sqls) == 4
    shard_sql, local_shard_sql, distributed_sql, local_distributed_sql = sqls
    assert "SETTINGS index_granularity" not in "\n".join(sqls)
    assert shard_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
    assert "ON CLUSTER '{cluster}'" in shard_sql
    assert "ENGINE = ReplicatedMergeTree" in shard_sql
    assert "PARTITION BY `month_date`" in shard_sql
    assert "ORDER BY (`month_date`, `min_month_use`)" in shard_sql
    assert local_shard_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
    assert "ON CLUSTER" not in local_shard_sql
    assert "UUID '" in local_shard_sql
    assert "ENGINE = ReplicatedMergeTree" in local_shard_sql
    assert distributed_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
    assert f"AS {TEST_CH_SHARD_TABLE}" not in distributed_sql
    assert "`min_month_use` Date" in distributed_sql
    assert "`month_date` Date" in distributed_sql
    assert "ENGINE = Distributed(" in distributed_sql
    assert "    '{cluster}'," in distributed_sql
    assert "    currentDatabase()," in distributed_sql
    assert f"    '{TEST_CH_SHARD_RELATION}'," in distributed_sql
    assert "    cityHash64(month_date, min_month_use)" in distributed_sql
    assert local_distributed_sql.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
    assert "ON CLUSTER" not in local_distributed_sql
    assert "ENGINE = Distributed(" in local_distributed_sql


def test_build_load_options_accepts_scalar_key_columns() -> None:
    options = load_df_module._build_load_options(
        db_key="gp",
        destination_table="sandbox.target",
        append=False,
        write_mode="upsert",
        gp_distributed_by_key=" id ",
        key_columns=" id ",
        trino_insert_chunk_size=None,
    )

    assert options.gp_distributed_by_key == ["id"]
    assert options.key_columns == ["id"]


def test_clickhouse_post_create_wait_shares_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
    observed_timeouts: list[float] = []
    monkeypatch.setattr(ch_wait_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        ch_wait_module,
        "_wait_for_ch_table",
        lambda *_args, timeout_seconds, **_kwargs: observed_timeouts.append(timeout_seconds),
    )
    monkeypatch.setattr(
        ch_wait_module,
        "_wait_for_ch_table_on_cluster",
        lambda *_args, timeout_seconds, **_kwargs: observed_timeouts.append(timeout_seconds),
    )
    monkeypatch.setattr(
        ch_wait_module,
        "_wait_for_ch_table_schema_on_cluster",
        lambda *_args, timeout_seconds, **_kwargs: observed_timeouts.append(timeout_seconds),
    )

    ch_wait_module._wait_for_ch_distributed_table_pair(
        object(),
        "analytics.events",
        timeout_seconds=10,
        expected_column_types={"event_id": "Int64"},
        shard_on_cluster="core",
        distributed_on_cluster="routing",
        routing_cluster=None,
    )

    assert observed_timeouts == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0]


def test_clickhouse_post_create_wait_uses_independent_policy_clusters() -> None:
    class SplitClusterClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                rows = [("routing",)]
            elif sql.startswith("EXISTS TABLE "):
                rows = [(1,)]
            elif "FROM system.clusters" in sql:
                cluster = sql.split("cluster = '", 1)[1].split("'", 1)[0]
                rows = [(22 if cluster == "routing" else 2,)]
            elif "system, one" in sql or "system, tables" in sql or "system, columns" in sql:
                rows = [(22 if "'routing'" in sql else 2,)]
            else:
                message = f"Unexpected query: {sql}"
                raise AssertionError(message)
            return SimpleNamespace(result_rows=rows)

    client = SplitClusterClient()
    policy = SimpleNamespace(
        shard_on_cluster="core",
        distributed_on_cluster="{cluster}",
        distributed_cluster="core",
        ddl_ready_timeout_seconds=17.0,
    )

    ch_wait_module.after_create_table(
        object(),
        client,
        "analytics.events",
        ch_distributed_table=True,
        expected_column_types={"event_id": "Int64"},
        ch_creation_policy=policy,
    )

    cluster_table_queries = [query for query in client.queries if "system, tables" in query]
    cluster_schema_queries = [query for query in client.queries if "system, columns" in query]
    assert any(
        "clusterAllReplicas('routing'" in query and "events'" in query
        for query in cluster_table_queries
    )
    assert any(
        "clusterAllReplicas('core'" in query and "events_shard'" in query
        for query in cluster_table_queries
    )
    assert not any(
        "clusterAllReplicas('routing'" in query and "events_shard'" in query
        for query in cluster_table_queries
    )
    assert any(
        "clusterAllReplicas('routing'" in query and "events'" in query
        for query in cluster_schema_queries
    )
    assert any(
        "clusterAllReplicas('core'" in query and "events_shard'" in query
        for query in cluster_schema_queries
    )


def test_create_sql_table_only_generate_sql_uses_float64_for_decimal_clickhouse_columns() -> None:
    batch = pd.DataFrame(
        {
            "amount": [Decimal("1.20"), Decimal("2.50"), None],
            "label": ["ok", "still ok", None],
        }
    )

    sql = create_sql_table_module.create_table(
        db_key="ch",
        table_name="schema.stage_table",
        df=batch,
        only_generate_sql=True,
    )

    assert "`amount` Nullable(Float64)" in sql
    assert "`label` Nullable(String)" in sql


def test_create_sql_table_only_generate_sql_uses_table_schema() -> None:
    sql = create_sql_table_module.create_table(
        db_key="gp",
        table_name="schema.stage_table",
        table_schema={
            "amount": "NUMERIC(12, 2)",
            "created_at": "TIMESTAMP",
        },
        only_generate_sql=True,
    )

    assert '"amount" NUMERIC(12, 2)' in sql
    assert '"created_at" TIMESTAMP' in sql
    assert "appendonly=true" in sql
    assert "blocksize=32768" in sql
    assert "compresstype=zstd" in sql
    assert "compresslevel=4" in sql
    assert "orientation=column" in sql


def test_empty_dataframe_replace_continues_to_empty_materialization() -> None:
    result = load_df_module._handle_empty_dataframe_load(
        SimpleNamespace(
            append=False,
            write_mode="replace",
            empty_source_policy=None,
            table_schema={"id": "BIGINT"},
            destination_table="sandbox.target",
        ),
        load_df_module.LoadState(
            target_exists=False,
            original_target_exists=False,
        ),
        pd.DataFrame({"id": pd.Series(dtype="int64")}),
        operation_metadata=load_df_module.SqlOperationMetadata(),
        return_metadata=False,
    )

    assert result is None


def test_empty_dataframe_error_policy_fails_before_target_changes() -> None:
    with pytest.raises(load_df_module.EmptySourceError, match="empty_source_policy='error'"):
        load_df_module._handle_empty_dataframe_load(
            SimpleNamespace(
                write_mode="replace",
                empty_source_policy="error",
                destination_table="sandbox.target",
            ),
            load_df_module.LoadState(False, False),
            pd.DataFrame({"id": pd.Series(dtype="int64")}),
            operation_metadata=load_df_module.SqlOperationMetadata(),
            return_metadata=False,
        )


def test_empty_dataframe_keep_returns_metadata_and_object_replace_requires_schema() -> None:
    metadata = load_df_module.SqlOperationMetadata()
    result = load_df_module._handle_empty_dataframe_load(
        SimpleNamespace(
            append=True,
            write_mode="append",
            empty_source_policy=None,
            table_schema=None,
            destination_table="sandbox.target",
        ),
        load_df_module.LoadState(True, True),
        pd.DataFrame(),
        operation_metadata=metadata,
        return_metadata=True,
    )
    assert result.rows == 0
    assert result.metadata.affected_rows == 0

    with pytest.raises(ValueError, match="table_schema is required"):
        load_df_module._handle_empty_dataframe_load(
            SimpleNamespace(
                append=False,
                write_mode="replace",
                empty_source_policy="replace",
                table_schema=None,
                destination_table="sandbox.target",
            ),
            load_df_module.LoadState(True, True),
            pd.DataFrame({"label": pd.Series(dtype="object")}),
            operation_metadata=load_df_module.SqlOperationMetadata(),
            return_metadata=False,
        )


def test_empty_dataframe_dry_run_honors_keep_error_and_replace_policies() -> None:
    empty = pd.DataFrame({"id": pd.Series(dtype="int64")})
    keep = load_df_module.load_df(
        "gp",
        "sandbox.target",
        empty,
        write_mode="append",
        empty_source_policy="keep",
        dry_run=True,
    )
    assert keep.sqls == []

    with pytest.raises(load_df_module.EmptySourceError):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            empty,
            empty_source_policy="error",
            dry_run=True,
        )

    replace = load_df_module.load_df(
        "gp",
        "sandbox.target",
        empty,
        table_schema={"id": "BIGINT"},
        empty_source_policy="replace",
        dry_run=True,
    )
    assert any("DROP TABLE" in sql for sql in replace.sqls)

    object_options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        write_mode="replace",
        empty_source_policy="replace",
    )
    with pytest.raises(ValueError, match="table_schema is required"):
        load_df_module.build_load_df_plan(
            object_options,
            pd.DataFrame({"label": pd.Series(dtype="object")}),
        )


def test_create_load_target_forwards_regular_ddl_properties(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        table_schema={"id": "BIGINT"},
        regular_ddl_properties={"orientation": "column"},
    )

    load_df_module._create_load_target_table(
        options,
        load_df_module.LoadState(False, False),
        object(),
        pd.DataFrame({"id": [1]}),
    )

    assert captured["ddl_properties"] == {"orientation": "column"}


def test_load_df_rejects_invalid_empty_source_policy() -> None:
    with pytest.raises(ValueError, match="empty_source_policy"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            empty_source_policy="invalid",
            dry_run=True,
        )


def test_load_df_clickhouse_creates_pair_and_loads_distributed_table(monkeypatch) -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": [date(2024, 2, 1)],
            "min_month_use": [date(2024, 1, 1)],
            "users": [10],
        }
    )

    monkeypatch.setattr(
        load_df_module,
        "get_sql_connection",
        lambda connection_type: client,
    )
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)

    inserted_rows = load_df_module.load_df(
        "ch",
        TEST_CH_TABLE,
        batch,
        retry_cnt=1,
        timeout_increment=0,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert inserted_rows == 1
    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE}" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_SHARD_TABLE}" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE} ON CLUSTER '{{cluster}}'" in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_SHARD_TABLE} ON CLUSTER '{{cluster}}'" in client.commands
    assert "SETTINGS index_granularity" not in "\n".join(client.commands)
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
        and "ON CLUSTER '{cluster}'" in command
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
        and "ON CLUSTER" not in command
        for command in client.commands
    )
    assert client.calls[0]["table"] == TEST_CH_TABLE
    assert client.close_calls == 4
