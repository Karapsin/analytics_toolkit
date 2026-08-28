from __future__ import annotations

from tests.sql._support.load_table import (
    Any,
    FakeClickHouseClient,
    FakeDbapiConnection,
    SimpleNamespace,
    _write_trino_connections,
    load_df_module,
    load_sql_table_module,
    pd,
    pytest,
)


def test_load_dataframe_passes_explicit_schema_to_direct_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    column_types = {"id": "Int64", "value": "Nullable(String)"}
    options = load_df_module.LoadOptions(
        connection_key="ch",
        connection_backend="ch",
        destination_table="sandbox.target",
        table_schema=column_types,
    )
    insert_kwargs: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda *_args, **kwargs: insert_kwargs.append(kwargs) or 1,
    )

    assert (
        load_df_module._load_dataframe(
            options,
            load_df_module.LoadState(False, False),
            pd.DataFrame({"id": [1], "value": ["one"]}),
        )
        == 1
    )
    assert insert_kwargs[0]["target_column_types"] == column_types


def test_load_df_clickhouse_upsert_existing_target_uses_target_types_and_df_columns(
    monkeypatch,
) -> None:
    client = FakeClickHouseClient()

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: client)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {
            "score": "Int64",
            "id": "UInt64",
            "extra_col": "String",
        },
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: "analytics.target__stage__upsert",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module, "cleanup_stage_table_with_retry", lambda *args, **kwargs: None
    )

    result = load_df_module.load_df(
        "ch",
        "analytics.target",
        pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 2
    assert any(
        "INSERT INTO analytics.target__stage__upsert (`id`, `score`) "
        "SELECT CAST(`id` AS UInt64) AS `id`, CAST(`score` AS Int64) AS `score` "
        "FROM analytics.target__stage__upsert" in sql
        for sql in client.commands
    )


def test_load_df_gp_upsert_existing_target_uses_target_types_and_df_columns(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection()

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {
            "score": "INTEGER",
            "id": "BIGINT",
            "extra_col": "TEXT",
        },
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: "sandbox.target__stage__upsert",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "validate_stage_uniqueness", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module, "cleanup_stage_table_with_retry", lambda *args, **kwargs: None
    )

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
    assert any(
        'INSERT INTO sandbox.target ("id", "score") '
        'SELECT CAST("id" AS BIGINT) AS "id", '
        'CAST("score" AS INTEGER) AS "score" '
        "FROM sandbox.target__stage__upsert" in sql
        for sql in connection.executed
    )


def test_load_df_parquet_runtime_passes_only_parquet_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    options = load_df_module.LoadOptions(
        connection_key="trino",
        connection_backend="trino",
        destination_table="iceberg.sandbox.target",
        table_schema={"id": "BIGINT"},
        s3_transfer_staging_schema="hive.stage",
        s3_transfer_staging_location="s3://bucket/stage",
        staging_ddl_properties={"compression_codec": "'ZSTD'"},
        parquet_ddl_properties={"parquet_marker": 7},
    )
    state = load_df_module.LoadState(target_exists=True, original_target_exists=True)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "build_stage_table_name",
        lambda *_args, **_kwargs: "hive.stage.shared",
    )
    monkeypatch.setattr(
        load_df_module,
        "build_stage_external_location",
        lambda *_args, **_kwargs: "s3://bucket/stage/shared/",
    )

    def build_sql(*_args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "CREATE TABLE hive.stage.shared"

    monkeypatch.setattr(load_df_module, "build_create_parquet_stage_table_sql", build_sql)
    monkeypatch.setattr(
        load_df_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(execute_command=lambda *_args: None),
    )

    load_df_module._create_load_parquet_stage_table(options, state, object())

    assert captured["ddl_properties"] == {"parquet_marker": 7}


def test_load_df_parquet_stage_does_not_inherit_sql_staging_properties(
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        ddl_defaults={
            "regular": {},
            "staging": {"compression_codec": "'ZSTD'"},
            "parquet_staging": {"parquet_marker": 7},
        },
    )

    plan = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1]}),
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )

    create_stage_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_stage"
    )
    assert "compression_codec" not in create_stage_sql
    assert "parquet_marker = 7" in create_stage_sql


def test_load_df_trino_parquet_stage_routes_through_external_table(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        s3_transfer_staging_schema="hive.pa_core_stage",
    )
    connection = FakeDbapiConnection()
    writes: list[dict[str, object]] = []
    inserts: list[tuple[str, str]] = []
    cleaned_locations: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(
        load_df_module,
        "table_exists",
        lambda connection_type, connection, table_name, **kwargs: (
            table_name == "iceberg.sandbox.target"
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {"id": "BIGINT", "label": "VARCHAR"},
    )
    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )
    monkeypatch.setattr(
        load_df_module,
        "write_dataframe_to_parquet_stage",
        lambda df, **kwargs: (
            writes.append(
                {
                    "rows": len(df),
                    "location": kwargs["stage_external_location"],
                    "row_group_size": kwargs["row_group_size"],
                    "pa": kwargs["pa"],
                    "pq": kwargs["pq"],
                    "fsspec": kwargs["fsspec_module"],
                }
            )
            or len(df)
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "insert_from_table",
        lambda connection_type, connection, target, stage, **kwargs: inserts.append(
            (target, stage)
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Parquet load_df must not use row inserts")
        ),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "cleanup_parquet_stage_location",
        lambda location: cleaned_locations.append(location),
    )

    rows = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1, 2], "label": ["a", "b"]}),
        append=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert rows == 2
    assert writes == [
        {
            "rows": 2,
            "location": cleaned_locations[0],
            "row_group_size": 50_000,
            "pa": "pa",
            "pq": "pq",
            "fsspec": "fsspec",
        }
    ]
    assert inserts[0][0] == "iceberg.sandbox.target"
    assert inserts[0][1].startswith("hive.pa_core_stage.daf6958bfec1c9f7__")
    assert cleaned_locations[0].startswith("s3://bucket/tmp/analytics_toolkit_transfer/target/")
    assert "__analytics_toolkit_target_user__stage__" in cleaned_locations[0]
    assert any(
        sql.startswith("CREATE TABLE hive.pa_core_stage.daf6958bfec1c9f7__")
        and "WITH (format = 'PARQUET', external_location = 's3://bucket/tmp/" in sql
        for sql in connection.executed
    )
    assert any(
        sql.startswith("DROP TABLE IF EXISTS hive.pa_core_stage.daf6958bfec1c9f7__")
        for sql in connection.executed
    )


def test_load_df_trino_parquet_upsert_uses_merge_from_stage(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
    )
    connection = FakeDbapiConnection()
    uniqueness_checks: list[tuple[str, list[str]]] = []
    upserts: list[dict[str, object]] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(
        load_df_module,
        "table_exists",
        lambda connection_type, connection, table_name, **kwargs: (
            table_name == "iceberg.sandbox.target"
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *args, **kwargs: {"id": "BIGINT", "score": "INTEGER"},
    )
    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )
    monkeypatch.setattr(
        load_df_module,
        "write_dataframe_to_parquet_stage",
        lambda df, **kwargs: len(df),
    )
    monkeypatch.setattr(
        load_df_module,
        "validate_stage_uniqueness",
        lambda connection_type, connection, stage_table, key_columns: uniqueness_checks.append(
            (stage_table, list(key_columns))
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "upsert_stage_table",
        lambda connection_type, connection, target, stage, columns, key_columns, **kwargs: (
            upserts.append(
                {
                    "target": target,
                    "stage": stage,
                    "columns": list(columns),
                    "key_columns": list(key_columns),
                }
            )
        ),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "cleanup_parquet_stage_location", lambda *args: None)

    rows = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1, 2], "score": [10, 20]}),
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert rows == 2
    assert uniqueness_checks[0][1] == ["id"]
    assert upserts == [
        {
            "target": "iceberg.sandbox.target",
            "stage": uniqueness_checks[0][0],
            "columns": ["id", "score"],
            "key_columns": ["id"],
        }
    ]


def test_load_df_trino_without_staging_location_keeps_insert_path(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
) -> None:
    _write_trino_connections(write_sql_connections, s3_transfer_staging_location=None)
    connection = FakeDbapiConnection()
    inserted_tables: list[str] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "get_table_column_types", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        load_df_module,
        "insert_table_batch",
        lambda connection_type, connection_ref, table_name, batch, **kwargs: (
            inserted_tables.append(table_name) or len(batch)
        ),
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("Parquet dependencies should not be loaded")),
    )

    rows = load_df_module.load_df(
        "trino_stage",
        "iceberg.sandbox.target",
        pd.DataFrame({"id": [1]}),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert rows == 1
    assert inserted_tables == ["iceberg.sandbox.target"]


def test_load_df_upsert_empty_existing_target_returns_zero(monkeypatch) -> None:
    connection = FakeDbapiConnection()

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: True)

    result = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": []}),
        write_mode="upsert",
        key_columns=["id"],
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 0
    assert connection.executed == []


def test_load_lifecycle_stage_schema_parquet_guards_and_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = load_df_module.LoadOptions(
        connection_key="trino",
        connection_backend="trino",
        destination_table="iceberg.sandbox.target",
        table_schema={"id": "BIGINT"},
        append=True,
        key_columns=["id"],
        transfer_staging_schema="scratch",
        s3_transfer_staging_location="s3://bucket/stage",
    )
    state = load_df_module.LoadState(True, True)
    stage_kwargs: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(
        load_df_module,
        "create_stage_table",
        lambda **kwargs: stage_kwargs.append(kwargs) or "scratch.stage",
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *_a, **_k: 1)
    monkeypatch.setattr(load_df_module, "validate_stage_target_key_overlap", lambda **_k: None)
    monkeypatch.setattr(load_df_module, "insert_from_table", lambda *_a, **_k: None)
    assert load_df_module._load_dataframe(options, state, pd.DataFrame({"id": [1]})) == 1
    assert stage_kwargs[0]["table_schema"] == {"id": "BIGINT"}

    monkeypatch.setattr(
        load_df_module,
        "ensure_parquet_staging_dependencies",
        lambda: (object(), object(), object()),
    )
    monkeypatch.setattr(load_df_module, "_create_load_parquet_stage_table", lambda *_a: None)
    with pytest.raises(RuntimeError, match="not initialized"):
        load_df_module._load_dataframe_via_parquet_stage(
            options=options,
            state=load_df_module.LoadState(True, True),
            df=pd.DataFrame({"id": [1]}),
            on_progress=None,
        )

    with pytest.raises(RuntimeError, match="was not initialized"):
        load_df_module._finalize_loaded_dataframe_stage(
            options=options,
            state=load_df_module.LoadState(True, True),
            connection=object(),
            df=pd.DataFrame({"id": [1]}),
        )

    overlap_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        load_df_module,
        "validate_stage_target_key_overlap",
        lambda **kwargs: overlap_calls.append(kwargs),
    )
    staged = load_df_module.LoadState(
        True,
        True,
        overlap_stage_table="scratch.stage",
    )
    load_df_module._finalize_loaded_dataframe_stage(
        options=options,
        state=staged,
        connection=object(),
        df=pd.DataFrame({"id": [1]}),
    )
    assert overlap_calls[0]["stage_table"] == "scratch.stage"


def test_existing_replace_uses_safe_stage_finalization_for_direct_and_parquet_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        table_schema={"id": "BIGINT"},
        write_mode="replace",
    )
    state = load_df_module.LoadState(
        True,
        True,
        target_column_types={"id": "BIGINT"},
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(load_df_module, "create_stage_table", lambda **_kwargs: "scratch.stage")
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        load_df_module,
        "finalize_stage_table",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    batch = pd.DataFrame({"id": [1]})

    assert load_df_module._load_dataframe(options, state, batch) == 1
    assert calls[0][1]["replace_target_table"] is True

    parquet_state = load_df_module.LoadState(
        True,
        True,
        overlap_stage_table="scratch.parquet_stage",
        target_column_types={"id": "BIGINT"},
    )
    load_df_module._finalize_loaded_dataframe_stage(
        options=options,
        state=parquet_state,
        connection=object(),
        df=batch,
    )
    assert calls[1][0][2] == "scratch.parquet_stage"


def test_existing_replace_target_preparation_uses_incoming_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
        write_mode="replace",
    )
    state = load_df_module.LoadState(True, True)
    monkeypatch.setattr(
        load_df_module,
        "apply_target_write_mode",
        lambda *_args, **_kwargs: pytest.fail("existing target must stay live"),
    )
    monkeypatch.setattr(
        load_df_module,
        "_create_load_target_table",
        lambda *_args, **_kwargs: pytest.fail("replacement must be staged"),
    )
    batch = pd.DataFrame({"id": [1]})

    load_df_module._apply_load_target_write_mode(options, state, object())
    load_df_module._ensure_load_target_table(options, state, object(), batch)
    load_df_module._load_target_column_metadata(options, state, object(), batch)

    assert state.target_column_types == {"id": "BIGINT"}


def test_load_cleanup_forwards_parquet_storage_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = load_df_module.LoadOptions(
        connection_key="trino",
        connection_backend="trino",
        destination_table="sandbox.target",
        parquet_storage_options={"endpoint_url": "http://minio"},
    )
    state = load_df_module.LoadState(
        False,
        False,
        stage_external_location="s3://bucket/stage",
    )
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        load_df_module,
        "cleanup_parquet_stage_location",
        lambda location, **kwargs: calls.append((location, kwargs)),
    )

    load_df_module._cleanup_load(options, state)

    assert calls == [("s3://bucket/stage", {"storage_options": {"endpoint_url": "http://minio"}})]


def test_load_parquet_collision_exhaustion_and_final_stage_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = load_df_module.LoadOptions(
        connection_key="trino",
        connection_backend="trino",
        destination_table="iceberg.sandbox.target",
        table_schema={"id": "BIGINT"},
        transfer_staging_schema="scratch",
        s3_transfer_staging_schema="scratch_s3",
        s3_transfer_staging_location="s3://bucket/stage",
    )
    state = load_df_module.LoadState(False, False)
    messages: list[str] = []
    monkeypatch.setattr(load_df_module, "STAGE_TABLE_NAME_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        load_df_module,
        "build_stage_table_name",
        lambda *_a, **_k: "scratch.collision",
    )
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(load_df_module, "time_print", messages.append)
    with pytest.raises(RuntimeError, match="unique Parquet"):
        load_df_module._create_load_parquet_stage_table(options, state, object())
    assert len(messages) == 2

    df = pd.DataFrame({"id": [1]})
    gp_options = load_df_module.LoadOptions(
        connection_key="gp",
        connection_backend="gp",
        destination_table="sandbox.target",
    )
    load_df_module._ensure_final_upsert_stage_table(gp_options, state, df)
    trino_state = load_df_module.LoadState(True, False)
    load_df_module._ensure_final_upsert_stage_table(options, trino_state, df)
    trino_state.original_target_exists = True
    trino_state.final_upsert_stage_table = "scratch.final"
    load_df_module._ensure_final_upsert_stage_table(options, trino_state, df)


def test_load_table_trino_scalar_normalization_handles_nat_and_target_types() -> None:
    assert load_sql_table_module._normalize_trino_value(pd.NaT, None) is None
    assert load_sql_table_module._normalize_trino_value("7", "bigint") == 7
    assert load_sql_table_module._normalize_trino_value(7, "varchar") == "7"
