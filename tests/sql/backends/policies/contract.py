from __future__ import annotations

from tests.sql._support.policies import (
    ClickHouseCreationPolicy,
    RowBatch,
    SimpleNamespace,
    _ch_config,
    _trino_config,
    config_module,
    legacy_clickhouse_scope,
    parquet_stage_module,
    pd,
    pytest,
    reconfigure_wait_module,
    resolve_clickhouse_creation_policy,
    storage_module,
    transfer_options_module,
    wait_module,
)


def test_batch_parquet_stage_forwards_storage_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[dict[str, str]] = []
    monkeypatch.setattr(
        parquet_stage_module,
        "row_batch_to_arrow_table",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "write_arrow_table_to_parquet",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "upload_spooled_file",
        lambda *_args, **kwargs: uploads.append(kwargs["storage_options"]),
    )

    written = parquet_stage_module.write_batch_to_parquet_stage(
        RowBatch(columns=["id"], rows=[(1,)]),
        file_index=0,
        stage_external_location="s3://bucket/stage",
        pa=object(),
        pq=object(),
        fsspec_module=object(),
        row_group_size=1,
        storage_options={"key": "access", "secret": "secret"},
    )

    assert written == 1
    assert uploads == [{"key": "access", "secret": "secret"}]


@pytest.mark.parametrize(
    "policy",
    ["wait_all", "wait_shard", "wait_distr", "wait_none"],
)
def test_clickhouse_connection_accepts_wait_policies(
    policy: str,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"ch": _ch_config(ch_ddl_wait_policy=policy)})

    assert config_module.get_connection_config("ch").ch_ddl_wait_policy == policy


def test_clickhouse_physical_wait_checks_expected_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas: list[str] = []
    monkeypatch.setattr(wait_module, "_wait_for_ch_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_table_on_cluster",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_table_schema_on_cluster",
        lambda _connection, table_name, **_kwargs: schemas.append(table_name),
    )

    wait_module._wait_for_ch_physical_table(
        object(),
        "db.target",
        shard_on_cluster="core",
        timeout_seconds=1,
        expected_column_types={"id": "Int64"},
    )

    assert schemas == ["db.target"]


def test_clickhouse_readiness_extensions_exhaust_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[float] = []

    def fail_wait(*_args: object, timeout_seconds: float, **_kwargs: object) -> None:
        attempts.append(timeout_seconds)
        message = "still converging"
        raise TimeoutError(message)

    monkeypatch.setattr(wait_module, "_wait_for_ch_distributed_table_pair", fail_wait)
    policy = ClickHouseCreationPolicy(
        True,
        "ReplicatedMergeTree",
        "core",
        "Distributed({cluster}, {database}, {shard_table})",
        "core",
        "core",
        "rand()",
        3,
        ddl_ready_timeout_extension_cnt=1,
        ddl_ready_timeout_increment_seconds=2,
    )

    with pytest.raises(TimeoutError, match="within 5 second"):
        wait_module.after_create_table(
            object(),
            object(),
            "db.target",
            ch_distributed_table=True,
            ch_creation_policy=policy,
        )

    assert attempts == [3, 2]


@pytest.mark.parametrize(
    ("explicit", "configured", "expected"),
    [
        (None, None, "wait_all"),
        (None, "wait_shard", "wait_shard"),
        ("wait_distr", "wait_shard", "wait_distr"),
        ("wait_none", None, "wait_none"),
    ],
)
def test_clickhouse_wait_policy_precedence(
    explicit: str | None,
    configured: str | None,
    expected: str,
) -> None:
    policy = resolve_clickhouse_creation_policy(
        legacy_clickhouse_scope(),
        ch_engine=None,
        ch_cluster=None,
        ch_sharding_key=None,
        ch_distributed_table=None,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        ch_shard_on_cluster=None,
        ch_distributed_on_cluster=None,
        ch_ddl_wait_policy=explicit,
        connection_ddl_wait_policy=configured,
    )

    assert policy.ddl_wait_policy == expected


def test_dataframe_parquet_stage_forwards_storage_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[dict[str, str]] = []
    monkeypatch.setattr(
        parquet_stage_module,
        "write_arrow_table_to_parquet",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "upload_spooled_file",
        lambda *_args, **kwargs: uploads.append(kwargs["storage_options"]),
    )
    pa = SimpleNamespace(Table=SimpleNamespace(from_pandas=lambda *_a, **_k: object()))

    written = parquet_stage_module.write_dataframe_to_parquet_stage(
        pd.DataFrame({"id": [1]}),
        stage_external_location="s3://bucket/stage",
        pa=pa,
        pq=object(),
        fsspec_module=object(),
        row_group_size=1,
        storage_options={"key": "access", "secret": "secret"},
    )

    assert written == 1
    assert uploads == [{"key": "access", "secret": "secret"}]


@pytest.mark.parametrize(
    ("access_field", "secret_field"),
    [
        ("access_key_id", "secret_access_key"),
        ("aws_access_key_id", "aws_secret_access_key"),
    ],
)
def test_direct_trino_storage_credentials_are_paired_and_redacted(
    access_field: str,
    secret_field: str,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_secret": _trino_config(
                **{access_field: "access-value", secret_field: "secret-value"},
            )
        }
    )

    config = config_module.get_connection_config("trino_secret")

    assert config.access_key_id == "access-value"
    assert config.secret_access_key == "secret-value"
    assert storage_module.parquet_storage_options(config) == {
        "key": "access-value",
        "secret": "secret-value",
    }
    assert "access-value" not in repr(config)
    assert "secret-value" not in repr(config)


@pytest.mark.parametrize("endpoint_field", ["endpoint_url", "aws_endpoint_url"])
def test_direct_trino_storage_endpoint_reaches_fsspec_options(
    endpoint_field: str,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {"trino": _trino_config(**{endpoint_field: "https://storage.yandexcloud.net"})}
    )

    config = config_module.get_connection_config("trino")

    assert storage_module.parquet_storage_options(config) == {
        "client_kwargs": {"endpoint_url": "https://storage.yandexcloud.net"}
    }


@pytest.mark.parametrize(
    ("policy", "expected_tables", "expected_routing"),
    [
        ("wait_all", {"db.target", "db.target_shard"}, True),
        ("wait_shard", {"db.target_shard"}, True),
        ("wait_distr", {"db.target"}, False),
        ("wait_none", set(), False),
    ],
)
def test_distributed_pair_wait_policy_selects_exact_readiness_checks(
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
    expected_tables: set[str],
    expected_routing: bool,
) -> None:
    local: list[str] = []
    cluster: list[str] = []
    schemas: list[str] = []
    routing: list[str] = []
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_table",
        lambda _connection, table_name, **_kwargs: local.append(table_name),
    )
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_table_on_cluster",
        lambda _connection, table_name, **_kwargs: cluster.append(table_name),
    )
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_table_schema_on_cluster",
        lambda _connection, table_name, **_kwargs: schemas.append(table_name),
    )
    monkeypatch.setattr(
        wait_module,
        "_validate_ch_shard_routing_cluster",
        lambda _connection, table_name, **_kwargs: routing.append(table_name),
    )

    wait_module._wait_for_ch_distributed_table_pair(
        object(),
        "db.target",
        shard_on_cluster="core",
        distributed_on_cluster="core",
        routing_cluster="core",
        timeout_seconds=1,
        expected_column_types={"id": "Int64"},
        wait_policy=policy,
    )

    assert set(local) == expected_tables
    assert set(cluster) == expected_tables
    assert set(schemas) == expected_tables
    assert bool(routing) is expected_routing


def test_omitted_trino_storage_credentials_preserve_provider_chain(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"trino": _trino_config()})

    config = config_module.get_connection_config("trino")

    assert storage_module.parquet_storage_options(config) is None


def test_reconfigure_wait_policy_filters_and_caches_replacement_roles() -> None:
    skipped: list[str] = []
    reconfigure_wait_module.wait_for_created_replacements(
        object(),
        SimpleNamespace(
            temporary_table_roles=[
                ("db.shard", None, "shard"),
                ("db.distr", "core", "distributed"),
            ],
            plan=SimpleNamespace(options={"ch_ddl_wait_policy": "wait_none"}),
        ),
        wait_local=lambda _connection, table: skipped.append(table),
        wait_cluster=lambda _connection, table, **_kwargs: skipped.append(table),
    )
    assert skipped == []

    waited: list[str] = []
    reconfiguration = SimpleNamespace(
        temporary_table_roles=None,
        temporary_table_scopes=[("db.shard", None), ("db.distr", "core")],
        plan=SimpleNamespace(
            options={"ch_ddl_wait_policy": "wait_all"},
            statements=[
                SimpleNamespace(phase="create_replacement", sql="CREATE TABLE db.shard"),
                SimpleNamespace(
                    phase="create_replacement",
                    sql="CREATE TABLE db.distr ENGINE = Distributed('core', 'db', 'shard')",
                ),
            ],
        ),
    )
    reconfigure_wait_module.wait_for_created_replacements(
        object(),
        reconfiguration,
        wait_local=lambda _connection, table: waited.append(table),
        wait_cluster=lambda _connection, table, **_kwargs: waited.append(table),
    )
    assert waited == ["db.shard", "db.distr"]
    assert reconfiguration.temporary_table_roles[-1][-1] == "distributed"


@pytest.mark.parametrize("policy", ["wait_distr", "wait_none"])
def test_single_physical_table_skips_wait_for_non_shard_policies(
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_physical_table",
        lambda *_args, **_kwargs: pytest.fail("physical readiness must be skipped"),
    )
    creation_policy = ClickHouseCreationPolicy(
        False,
        "MergeTree",
        "core",
        None,
        None,
        None,
        None,
        1,
        ddl_wait_policy=policy,
    )

    wait_module.after_create_table(
        object(),
        object(),
        "db.target",
        ch_creation_policy=creation_policy,
    )


def test_transfer_extension_count_accepts_integer() -> None:
    transfer_options_module.validate_optional_non_negative_int(0, "extension_cnt")
