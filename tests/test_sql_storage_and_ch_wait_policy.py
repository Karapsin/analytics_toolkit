from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.creation_policy import (
    ClickHouseCreationPolicy,
    resolve_clickhouse_creation_policy,
)
from analytics_toolkit.sql.connection.ddl_defaults import legacy_clickhouse_scope
from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Self

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
ddl_options_module = importlib.import_module("analytics_toolkit.sql.dml.ddl_options")
parquet_stage_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
)
storage_module = importlib.import_module("analytics_toolkit.sql.backends.trino.storage")
wait_module = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")
creation_policy_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.creation_policy"
)
reconfigure_wait_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.reconfigure_wait"
)
transfer_options_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.options")
transfer_finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)
trino_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.trino.adapter")


def _trino_config(**overrides: object) -> dict[str, object]:
    return {
        "type": "trino",
        "host": "trino.example",
        "user": "user",
        **overrides,
    }


def _ch_config(**overrides: object) -> dict[str, object]:
    return {
        "type": "ch",
        "host": "ch.example",
        "user": "user",
        "password": "password",
        **overrides,
    }


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


def test_omitted_trino_storage_credentials_preserve_provider_chain(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"trino": _trino_config()})

    config = config_module.get_connection_config("trino")

    assert storage_module.parquet_storage_options(config) is None


def test_parquet_storage_credentials_reach_upload_and_cleanup() -> None:
    open_calls: list[tuple[str, str, dict[str, str]]] = []
    cleanup_calls: list[tuple[str, dict[str, str]]] = []

    class RemoteFile:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, value: bytes) -> int:
            return len(value)

    class FileSystem:
        def rm(self, path: str, *, recursive: bool) -> None:
            assert recursive is True
            cleanup_calls.append((path, {}))

    fs = FileSystem()
    fsspec = SimpleNamespace(
        open=lambda uri, mode, **kwargs: open_calls.append((uri, mode, kwargs)) or RemoteFile(),
        core=SimpleNamespace(
            url_to_fs=lambda uri, **kwargs: (
                cleanup_calls.append((uri, kwargs)) or (fs, "bucket/stage")
            )
        ),
    )
    options = {"key": "access-value", "secret": "secret-value"}

    parquet_stage_module.upload_spooled_file(
        fsspec,
        SimpleNamespace(read=lambda _size=-1: b""),
        "s3://bucket/stage/file.parquet",
        storage_options=options,
    )
    parquet_stage_module.cleanup_parquet_stage_location(
        "s3://bucket/stage/",
        fsspec_module=fsspec,
        storage_options=options,
    )

    assert open_calls == [
        ("s3://bucket/stage/file.parquet", "wb", options),
    ]
    assert cleanup_calls[0] == ("s3://bucket/stage/", options)


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


def test_clickhouse_connection_rejects_invalid_wait_policy(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections({"ch": _ch_config(ch_ddl_wait_policy="eventually")})

    with pytest.raises(SqlConfigError, match="ch_ddl_wait_policy"):
        config_module.get_connection_config("ch")


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


def test_non_clickhouse_operation_rejects_wait_policy() -> None:
    config = SimpleNamespace(backend="trino", ddl_defaults=None)

    with pytest.raises(ValueError, match="requires a ClickHouse target"):
        ddl_options_module.resolve_operation_ddl(
            config,
            ch_ddl_wait_policy="wait_none",
        )


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


def test_clickhouse_routing_error_allows_empty_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wait_module,
        "_resolve_ch_cluster_name_for_wait",
        lambda _connection, cluster: cluster,
    )
    monkeypatch.setattr(wait_module, "_query_ch_expected_cluster_hosts", lambda *_a, **_k: 2)
    monkeypatch.setattr(wait_module, "_query_ch_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        wait_module,
        "_describe_ch_missing_routing_hosts",
        lambda *_args, **_kwargs: "",
    )

    with pytest.raises(SqlConfigError, match="visible on 1/2"):
        wait_module._validate_ch_shard_routing_cluster(
            object(),
            "db.target_shard",
            ch_cluster="core",
            shard_on_cluster="core",
            expected_column_types=None,
        )


def test_clickhouse_routing_schema_error_allows_empty_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = iter([2, 0])
    monkeypatch.setattr(
        wait_module,
        "_resolve_ch_cluster_name_for_wait",
        lambda _connection, cluster: cluster,
    )
    monkeypatch.setattr(wait_module, "_query_ch_expected_cluster_hosts", lambda *_a, **_k: 2)
    monkeypatch.setattr(wait_module, "_query_ch_count", lambda *_a, **_k: next(counts))
    monkeypatch.setattr(
        wait_module,
        "_describe_ch_cluster_schema_mismatch",
        lambda *_args, **_kwargs: "",
    )

    with pytest.raises(SqlConfigError, match="observed 0/2"):
        wait_module._validate_ch_shard_routing_cluster(
            object(),
            "db.target_shard",
            ch_cluster="core",
            shard_on_cluster="core",
            expected_column_types={"id": "Int64"},
        )


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


@pytest.mark.parametrize("value", [True, 1.5, -1])
def test_transfer_extension_count_requires_non_negative_integer(value: object) -> None:
    with pytest.raises(ValueError, match="integer of at least 0"):
        transfer_options_module.validate_optional_non_negative_int(value, "extension_cnt")


def test_transfer_extension_count_accepts_integer() -> None:
    transfer_options_module.validate_optional_non_negative_int(0, "extension_cnt")


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


def test_trino_upsert_placeholder_builds_complete_stage_sqls() -> None:
    statements = trino_adapter_module.TrinoAdapter().build_upsert_stage_placeholder_sqls(
        "iceberg.stage.target",
        "iceberg.stage.incoming",
        key_columns=["id"],
        upsert_partition_column="dt",
        final_stage_table="iceberg.stage.final",
        partition_values=["2026-01-01"],
        trino_partition_drop_sql_template=(
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    )

    assert len(statements) >= 2


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
