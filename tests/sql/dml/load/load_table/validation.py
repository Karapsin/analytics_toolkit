from __future__ import annotations

from tests.sql._support.load_table import (
    FakeDbapiConnection,
    SimpleNamespace,
    ch_wait_module,
    load_df_module,
    pd,
    pytest,
    table_ops_module,
)


def test_clickhouse_post_create_rejects_routing_schema_mismatch() -> None:
    class RoutingSchemaClient:
        def query(self, sql: str) -> object:
            if "system, one" in sql or "FROM system.clusters" in sql or "system, tables" in sql:
                rows = [(2,)]
            elif "GROUP BY name, type" in sql:
                rows = [("event_id", "UInt8", 2)]
            elif "system, columns" in sql:
                rows = [(0,)]
            else:
                message = f"Unexpected query: {sql}"
                raise AssertionError(message)
            return SimpleNamespace(result_rows=rows)

    with pytest.raises(
        ch_wait_module.SqlConfigError,
        match=r"schema is not ready.*observed 0/2",
    ) as exc_info:
        ch_wait_module._validate_ch_shard_routing_cluster(
            RoutingSchemaClient(),
            "analytics.events_shard",
            ch_cluster="core",
            shard_on_cluster="core",
            expected_column_types={"event_id": "Int64"},
        )

    assert "observed UInt8 on 2 host" in str(exc_info.value)


def test_clickhouse_post_create_rejects_uncovered_routing_cluster() -> None:
    class MismatchedRoutingClient:
        def query(self, sql: str) -> object:
            if sql.startswith("SELECT getMacro("):
                rows = [("routing",)]
            elif sql.startswith("EXISTS TABLE "):
                rows = [(1,)]
            elif "SELECT DISTINCT host_name" in sql:
                rows = [(f"host-{index:02d}",) for index in range(22)]
            elif "FROM system.clusters" in sql or "system, one" in sql:
                rows = [(22 if "'routing'" in sql else 2,)]
            elif "system, tables" in sql and sql.startswith("SELECT hostName()"):
                rows = [
                    ("host-00", "analytics", "events_shard", "ReplicatedMergeTree"),
                    ("host-01", "analytics", "events_shard", "ReplicatedMergeTree"),
                ]
            elif "system, tables" in sql:
                is_shard = "events_shard" in sql
                count = 2 if is_shard and "'routing'" in sql else 22
                if is_shard and "'core'" in sql:
                    count = 2
                rows = [(count,)]
            elif "system, columns" in sql:
                rows = [(2,)]
            else:
                message = f"Unexpected query: {sql}"
                raise AssertionError(message)
            return SimpleNamespace(result_rows=rows)

    policy = SimpleNamespace(
        shard_on_cluster="core",
        distributed_on_cluster="{cluster}",
        distributed_cluster="{cluster}",
        ddl_ready_timeout_seconds=17.0,
    )

    with pytest.raises(
        ch_wait_module.SqlConfigError,
        match=r"routing cluster 'routing'.*2/22.*shard DDL uses cluster 'core'",
    ) as exc_info:
        ch_wait_module.after_create_table(
            object(),
            MismatchedRoutingClient(),
            "analytics.events",
            ch_distributed_table=True,
            expected_column_types={"event_id": "Int64"},
            ch_creation_policy=policy,
        )

    assert "host-02" in str(exc_info.value)
    assert "ch_distributed_cluster" in str(exc_info.value)


@pytest.mark.parametrize(
    ("options", "target_types", "message"),
    [
        (
            {"s3_transfer_staging_schema": None, "s3_transfer_staging_location": "s3://x"},
            {"id": "BIGINT"},
            "s3_transfer_staging_schema",
        ),
        (
            {"s3_transfer_staging_schema": "stage", "s3_transfer_staging_location": None},
            {"id": "BIGINT"},
            "s3_transfer_staging_location",
        ),
        (
            {
                "s3_transfer_staging_schema": "stage",
                "s3_transfer_staging_location": "s3://x",
            },
            None,
            "Could not resolve target schema",
        ),
    ],
)
def test_create_load_parquet_stage_requires_complete_configuration(
    options: dict[str, object],
    target_types: dict[str, str] | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        load_df_module._create_load_parquet_stage_table(
            load_df_module.LoadOptions(
                connection_key="trino",
                connection_backend="trino",
                destination_table="sandbox.target",
                s3_transfer_staging_schema=options["s3_transfer_staging_schema"],
                s3_transfer_staging_location=options["s3_transfer_staging_location"],
            ),
            load_df_module.LoadState(
                target_exists=True,
                original_target_exists=True,
                target_column_types=target_types,
            ),
            object(),
        )


def test_dataframe_key_uniqueness_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match=r"Duplicate key values.*id, region"):
        load_df_module._validate_dataframe_key_uniqueness(
            pd.DataFrame(
                {
                    "id": [1, 1],
                    "region": ["north", "north"],
                }
            ),
            ["id", "region"],
        )


def test_dataframe_key_uniqueness_rejects_null_keys() -> None:
    with pytest.raises(ValueError, match=r"Null key values.*id"):
        load_df_module._validate_dataframe_key_uniqueness(
            pd.DataFrame({"id": [1, None], "value": ["one", "missing"]}),
            ["id"],
        )


def test_finalize_stage_table_upsert_missing_target_creates_and_inserts() -> None:
    connection = FakeDbapiConnection()
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="gp",
        connection=connection,
        stage_table="sandbox.target__stage",
        target_table="sandbox.target",
        replace_target_table=True,
        target_exists=False,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        insert_column_types={"id": "BIGINT", "score": "INTEGER"},
        target_column_types={"id": "BIGINT", "score": "INTEGER"},
    )

    assert connection.executed[0].startswith("CREATE TABLE sandbox.target")
    assert connection.executed[1].startswith('INSERT INTO sandbox.target ("id", "score") ')


def test_load_df_rejects_non_dataframe_before_connection_lookup() -> None:
    with pytest.raises(TypeError, match="pandas DataFrame"):
        load_df_module.load_df("gp", "sandbox.target", [{"id": 1}])


def test_load_df_upsert_missing_target_creates_and_inserts(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    create_calls: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda connection_type, connection, table_name, *args, **kwargs: create_calls.append(
            table_name
        ),
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    result = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
        write_mode="upsert",
        key_columns=["id"],
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 2
    assert create_calls == ["sandbox.target"]


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_load_df_validates_progress(progress: object) -> None:
    with pytest.raises(ValueError, match="progress"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            dry_run=True,
            progress=progress,
        )


def test_load_lifecycle_remaining_validation_and_metadata_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    append_options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        append=True,
    )
    state = load_df_module.LoadState(True, True)
    assert (
        load_df_module._handle_empty_dataframe_load(
            append_options,
            state,
            operation_metadata=load_df_module.SqlOperationMetadata(),
            return_metadata=False,
        )
        == 0
    )

    missing_distribution = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        gp_distributed_by_key=["missing"],
    )
    with pytest.raises(ValueError, match="missing"):
        load_df_module._validate_load_dataframe(
            missing_distribution,
            pd.DataFrame({"id": [1]}),
        )

    labelled = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        query_label="candidate-9",
    )
    create_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *_args, **kwargs: create_calls.append(kwargs),
    )
    monkeypatch.setattr(
        load_df_module.get_backend_adapter("gp"),
        "build_load_target_create_kwargs",
        lambda **_kwargs: {},
    )
    load_df_module._create_load_target_table(
        labelled,
        load_df_module.LoadState(False, False),
        object(),
        pd.DataFrame({"id": [1]}),
    )
    assert create_calls == [{"connection_key": "gp", "query_label": "candidate-9"}]

    analyze_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "analyze_table",
        lambda **kwargs: analyze_calls.append(kwargs),
    )
    load_df_module._analyze_load_target(labelled, object())
    assert analyze_calls[0]["query_label"] == "candidate-9"

    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("count failed")),
    )
    result_metadata = load_df_module._build_load_metadata(
        options=labelled,
        state=state,
        source_rows=2,
        inserted_rows=2,
        operation_metadata=load_df_module.SqlOperationMetadata(),
    )
    assert result_metadata.final_target_rows is None
    assert result_metadata.inserted_rows == 2


def test_load_write_mode_and_clickhouse_shard_validation() -> None:
    assert (
        load_df_module._resolve_load_write_mode(
            "gp",
            append=False,
            write_mode=None,
        )
        == "replace"
    )
    assert (
        load_df_module._resolve_load_write_mode(
            "gp",
            append=True,
            write_mode=None,
        )
        == "append"
    )
    with pytest.raises(ValueError, match="append=True cannot be combined"):
        load_df_module._resolve_load_write_mode(
            "gp",
            append=True,
            write_mode="replace",
        )
    with pytest.raises(ValueError, match="ch_only_shard must be a boolean"):
        load_df_module._normalize_only_shard(1)
