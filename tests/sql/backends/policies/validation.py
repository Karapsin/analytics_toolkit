from __future__ import annotations

from tests.sql._support.policies import (
    ClickHouseCreationPolicy,
    SimpleNamespace,
    SqlConfigError,
    _ch_config,
    _trino_config,
    config_module,
    creation_policy_module,
    ddl_options_module,
    json,
    pytest,
    sql,
    transfer_finalize_module,
    transfer_options_module,
    wait_module,
)


def test_airflow_source_file_rejects_direct_trino_storage_credentials(
    tmp_path: Path,
) -> None:
    (tmp_path / ".connections").write_text(
        json.dumps(
            {
                "source": "airflow",
                "connections": {
                    "trino": {
                        "type": "trino",
                        "access_key_id": "access-value",
                        "secret_access_key": "secret-value",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SqlConfigError, match="not allowed in Airflow-source"):
        config_module.load_sql_connections()


def test_clickhouse_connection_rejects_invalid_wait_policy(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"ch": _ch_config(ch_ddl_wait_policy="eventually")})

    with pytest.raises(SqlConfigError, match="ch_ddl_wait_policy"):
        config_module.get_connection_config("ch")


def test_clickhouse_missing_routing_hosts_returns_empty_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wait_module, "_query_ch_rows", lambda *_args, **_kwargs: [("a",)])
    monkeypatch.setattr(
        wait_module,
        "_query_ch_cluster_table_rows",
        lambda *_args, **_kwargs: [("a",)],
    )

    assert (
        wait_module._describe_ch_missing_routing_hosts(
            object(),
            "db.target_shard",
            ch_cluster="core",
        )
        == ""
    )


def test_clickhouse_preclear_requires_both_external_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transfer_finalize_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(needs_bounded_replace_preclear=lambda _only_shard: True),
    )
    options = SimpleNamespace(
        to_db_backend="ch",
        write_mode="replace",
        replace_target_table=True,
        ch_only_shard=False,
    )

    with pytest.raises(RuntimeError, match="must be supplied together"):
        transfer_finalize_module._preclear_clickhouse_replace_target(
            options,
            SimpleNamespace(target_exists=True),
            target_connection_runner=lambda *_args: None,
            target_host_connection_runner=None,
        )


def test_clickhouse_readiness_rejects_impossible_empty_attempt_range() -> None:
    policy = ClickHouseCreationPolicy(
        True,
        "ReplicatedMergeTree",
        "core",
        "Distributed({cluster}, {database}, {shard_table})",
        "core",
        "core",
        "rand()",
        3,
        ddl_ready_timeout_extension_cnt=-1,
    )

    with pytest.raises(RuntimeError, match="without capturing"):
        wait_module.after_create_table(
            object(),
            object(),
            "db.target",
            ch_distributed_table=True,
            ch_creation_policy=policy,
        )


@pytest.mark.parametrize("expected_schema", [None, {}])
def test_clickhouse_routing_validation_accepts_ready_table_without_schema_check(
    monkeypatch: pytest.MonkeyPatch,
    expected_schema: dict[str, str] | None,
) -> None:
    monkeypatch.setattr(
        wait_module,
        "_resolve_ch_cluster_name_for_wait",
        lambda _connection, cluster: cluster,
    )
    monkeypatch.setattr(wait_module, "_query_ch_expected_cluster_hosts", lambda *_a, **_k: 2)
    monkeypatch.setattr(wait_module, "_query_ch_count", lambda *_a, **_k: 2)
    if expected_schema == {}:
        monkeypatch.setattr(wait_module, "normalize_table_schema", lambda *_a, **_k: {})

    wait_module._validate_ch_shard_routing_cluster(
        object(),
        "db.target_shard",
        ch_cluster="core",
        shard_on_cluster="core",
        expected_column_types=expected_schema,
    )


def test_create_from_sql_rejects_clickhouse_wait_policy_for_trino(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"trino": _trino_config()})

    with pytest.raises(ValueError, match="requires a ClickHouse target"):
        sql.create_sql_table(
            "trino",
            "stage.target",
            sql="SELECT 1 AS id",
            ch_ddl_wait_policy="wait_none",
            dry_run=True,
        )


@pytest.mark.parametrize(
    "removed_field",
    ["transfer_staging_location", "transfer_parquet_staging_schema"],
)
def test_direct_trino_s3_staging_rejects_removed_field_names(
    removed_field: str,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"trino": _trino_config(**{removed_field: "legacy-value"})})

    with pytest.raises(SqlConfigError, match="removed Trino staging field"):
        config_module.get_connection_config("trino")


@pytest.mark.parametrize(
    "fields",
    [
        {"s3_transfer_staging_schema": "hive.stage"},
        {"s3_transfer_staging_location": "s3://bucket/prefix"},
    ],
)
def test_direct_trino_s3_staging_requires_schema_location_pair(
    fields: dict[str, str],
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"trino": _trino_config(**fields)})

    with pytest.raises(SqlConfigError, match="must be supplied together"):
        config_module.get_connection_config("trino")


@pytest.mark.parametrize(
    "field",
    [
        "access_key_id",
        "secret_access_key",
        "aws_access_key_id",
        "aws_secret_access_key",
    ],
)
def test_direct_trino_storage_credentials_reject_incomplete_pair(
    field: str,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"trino_secret": _trino_config(**{field: "value"})})

    with pytest.raises(SqlConfigError, match="must be supplied together"):
        config_module.get_connection_config("trino_secret")


def test_direct_trino_storage_credentials_reject_mixed_families(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_secret": _trino_config(
                aws_access_key_id="access-value",
                secret_access_key="secret-value",
            )
        }
    )

    with pytest.raises(SqlConfigError, match="cannot be mixed"):
        config_module.get_connection_config("trino_secret")


@pytest.mark.parametrize(
    "field",
    [
        "session_token",
        "aws_session_token",
    ],
)
def test_direct_trino_storage_credentials_reject_session_tokens(
    field: str,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"trino_secret": _trino_config(**{field: "value"})})

    with pytest.raises(SqlConfigError, match="unsupported Trino Parquet credential"):
        config_module.get_connection_config("trino_secret")


def test_direct_trino_storage_endpoint_rejects_dual_names(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino": _trino_config(
                endpoint_url="https://one.example",
                aws_endpoint_url="https://two.example",
            )
        }
    )

    with pytest.raises(SqlConfigError, match="only one"):
        config_module.get_connection_config("trino")


def test_invalid_distributed_engine_template_is_rejected() -> None:
    policy = ClickHouseCreationPolicy(
        True,
        "ReplicatedMergeTree",
        "core",
        "Distributed({cluster})",
        "core",
        "core",
        "rand()",
        1,
    )

    with pytest.raises(SqlConfigError, match="at least three arguments"):
        creation_policy_module._render_distributed_engine(policy, "db.target_shard")


def test_non_clickhouse_operation_rejects_wait_policy() -> None:
    config = SimpleNamespace(backend="trino", ddl_defaults=None)

    with pytest.raises(ValueError, match="requires a ClickHouse target"):
        ddl_options_module.resolve_operation_ddl(
            config,
            ch_ddl_wait_policy="wait_none",
        )


@pytest.mark.parametrize("value", [True, 1.5, -1])
def test_transfer_extension_count_requires_non_negative_integer(value: object) -> None:
    with pytest.raises(ValueError, match="integer of at least 0"):
        transfer_options_module.validate_optional_non_negative_int(value, "extension_cnt")
