from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd

from .models import (
    TargetConnectionDefaults,
    TransferAttemptPolicy,
    TransferInsertPageSizing,
)


def prepare_sql(adapter: Any, config: Any, sql: str) -> str:
    del adapter, config
    return sql

_DEFAULT_CH_ENGINE = "ReplicatedMergeTree"
_DEFAULT_CH_CLUSTER = "{cluster}"
_DEFAULT_CH_SHARDING_KEY = "rand()"


def validate_airflow_file_overrides(
    adapter: Any,
    raw_config: dict[str, Any],
    connection_key: str,
) -> None:
    forbidden = adapter.forbidden_airflow_file_override_fields.intersection(raw_config)
    if forbidden:
        # Import lazily because connection.config imports the backend registry.
        from analytics_toolkit.sql.connection.errors import SqlConfigError  # noqa: PLC0415

        fields = ", ".join(sorted(forbidden))
        message = (
            f"Direct {adapter.display_name} object-storage credentials are not allowed "
            f"in Airflow-source .connections entry '{connection_key}': {fields}."
        )
        raise SqlConfigError(message)


def map_same_backend_source_type_to_target(adapter: Any, column: Any) -> str:
    return str(adapter.map_source_type_to_target(column))


def build_show_tables_query(
    adapter: Any,
    config: Any,
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
    *,
    trino_catalog: str | None = None,
    ch_distributed_table_stats: bool = False,
) -> str:
    del (
        adapter,
        config,
        schema,
        table_names,
        conditions,
        trino_catalog,
        ch_distributed_table_stats,
    )
    raise NotImplementedError


def postprocess_show_tables(
    adapter: Any,
    connection_key: str,
    tables: Any,
    *,
    ch_distributed_table_stats: bool = False,
    read_sql: Callable[[str, str], Any] | None = None,
) -> Any:
    del adapter, connection_key, ch_distributed_table_stats, read_sql
    return tables


def allows_show_tables_catalog_filter(adapter: Any) -> bool:
    return bool(adapter.supports_show_tables_catalog_filter)


def should_analyze_table(adapter: Any) -> bool:
    return bool(adapter.supports_analyze)


def validate_write_mode(
    adapter: Any,
    write_mode: str,
    *,
    option_name: str = "write_mode",
) -> str:
    normalized = write_mode.strip().lower()
    if normalized not in {"append", "replace", "truncate_insert", "upsert"}:
        raise ValueError(f"{option_name} must be one of: append, replace, truncate_insert, upsert.")

    if normalized not in adapter.supported_write_modes:
        raise ValueError(f"{adapter.display_name} does not support {option_name}={normalized!r}.")
    return normalized


def normalize_ch_columns_or_expression(
    adapter: Any,
    value: Sequence[str] | str | None,
    option_name: str,
) -> list[str] | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return normalize_ch_string(adapter, value, option_name)

    normalized = [normalize_ch_string(adapter, column, option_name) for column in value]
    if not normalized:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{option_name} must not contain duplicate column names.")
    return normalized


def normalize_ch_string(adapter: Any, value: str, option_name: str) -> str:
    del adapter
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{option_name} must not be empty.")
    return normalized


def validate_ch_columns_in_columns(
    adapter: Any,
    value: list[str] | str | None,
    columns: Sequence[str],
    option_name: str,
    *,
    data_name: str,
) -> None:
    del adapter
    if value is None or isinstance(value, str):
        return

    available_columns = {str(column) for column in columns}
    missing_columns = [column for column in value if column not in available_columns]
    if missing_columns:
        raise ValueError(
            f"{option_name} columns were not found in the {data_name}: "
            + ", ".join(missing_columns)
        )


def extract_table_ddl(
    adapter: Any,
    connection_key: str,
    table_name: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    del adapter, connection_key, table_name, read_sql
    raise NotImplementedError


def validate_drop_partitions_options(
    adapter: Any,
    *,
    partition_column: str | None,
    gp_truncate: bool,
) -> None:
    from ..connection.errors import InvalidSqlInputError

    del adapter
    if gp_truncate:
        raise InvalidSqlInputError("gp_truncate=True is only supported for Greenplum connections.")
    if partition_column is not None:
        raise InvalidSqlInputError(
            "trino_partition_column is only supported for Trino partition deletes."
        )


def build_drop_partitions_sqls(
    adapter: Any,
    table: str,
    partition_keys: Sequence[str],
    *,
    partition_column: str | None = None,
    gp_truncate: bool = False,
    ch_cluster: str = "{cluster}",
) -> list[str]:
    del adapter, table, partition_keys, partition_column, gp_truncate, ch_cluster
    raise NotImplementedError


def build_create_partition_sql(
    adapter: Any,
    table: str,
    *,
    name: str,
    start: str | None = None,
    end: str | None = None,
    value: str | None = None,
) -> str:
    del adapter, table, name, start, end, value
    raise NotImplementedError


def query_transfer_stage_table_names(
    adapter: Any,
    connection: Any,
    *,
    connection_key: str,
    transfer_staging_schema: str,
    table_pattern: str,
) -> list[str]:
    del connection_key
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name LIKE %s
            """.strip(),
            (transfer_staging_schema, table_pattern),
        )
        return [str(row[0]) for row in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def qualify_transfer_stage_table_name(
    adapter: Any,
    connection_key: str,
    transfer_staging_schema: str,
    table_name: str,
) -> str:
    del connection_key
    parts = (transfer_staging_schema, table_name)
    return ".".join(_quote_identifier_part_when_needed(adapter, part) for part in parts)


def stage_base_identifier(
    adapter: Any,
    base_identifier: str,
    transfer_staging_username: str | None,
    stage_suffix: str,
) -> str:
    del adapter, transfer_staging_username, stage_suffix
    return base_identifier


def build_drop_tables_sqls(
    adapter: Any,
    table_name: str,
    *,
    ch_cluster: str | None = "{cluster}",
    ch_drop_shard: bool = True,
    ch_drop_distributed: bool = True,
    if_exists: bool = False,
    query_label: str | None = None,
) -> list[str]:
    del ch_drop_shard, ch_drop_distributed
    return [
        adapter.drop_table_sql(
            table_name,
            if_exists=if_exists,
            ch_cluster=ch_cluster,
            query_label=query_label,
        )
    ]


def build_drop_target_sqls(
    adapter: Any,
    table_name: str,
    *,
    ch_cluster: str | None = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
) -> list[str]:
    del ch_only_shard
    return [
        adapter.drop_table_sql(
            table_name,
            if_exists=True,
            ch_cluster=ch_cluster,
            query_label=query_label,
        )
    ]


def drop_table_with_options(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    connection_key: str,
    ch_cluster: str | None = "{cluster}",
    ch_drop_shard: bool = True,
    ch_drop_distributed: bool = True,
    ch_wait_for_absence: bool = False,
    ch_wait_timeout_seconds: int = 300,
    ch_wait_poll_interval_seconds: float = 1,
    ch_retry_per_host_drops: bool = True,
    if_exists: bool = False,
    query_label: str | None = None,
) -> None:
    del (
        ch_drop_shard,
        ch_drop_distributed,
        ch_wait_for_absence,
        ch_wait_timeout_seconds,
        ch_wait_poll_interval_seconds,
        ch_retry_per_host_drops,
    )
    from analytics_toolkit.general import time_print

    time_print(
        f"Dropping table {table_name}",
        connection=connection_key,
        backend=adapter.backend,
    )
    adapter.drop_table(
        connection,
        table_name,
        if_exists=if_exists,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )


def build_clear_target_sqls(
    adapter: Any,
    table_name: str,
    *,
    query_label: str | None = None,
    include_ch_shard: bool = False,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
) -> list[str]:
    del include_ch_shard, ch_cluster, ch_only_shard
    return adapter.clear_table_sqls(table_name, query_label=query_label)


def build_transfer_replace_target_sqls(
    adapter: Any,
    table_name: str,
    *,
    query_label: str | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
) -> list[str]:
    if adapter.transfer_replace_existing_non_ch() == "drop":
        return adapter.build_drop_target_sqls(
            table_name,
            query_label=query_label,
            ch_cluster=ch_cluster,
            ch_only_shard=ch_only_shard,
        )
    return adapter.build_clear_target_sqls(
        table_name,
        query_label=query_label,
        ch_cluster=ch_cluster,
        ch_only_shard=ch_only_shard,
    )


def transfer_replace_target_phase(adapter: Any) -> str:
    if adapter.transfer_replace_existing_non_ch() == "drop":
        return "drop_target"
    return "clear_target"


def transfer_replace_existing_non_ch(adapter: Any) -> str:
    del adapter
    return "clear"


def companion_table_name(adapter: Any, table_name: str) -> str | None:
    del adapter, table_name
    return None


def resolve_table_info_table_name(
    adapter: Any,
    table_name: str,
    *,
    connection_key: str,
) -> str | None:
    del adapter, table_name, connection_key
    return None


def rollback_quietly(adapter: Any, connection: Any) -> None:
    del adapter, connection


def refine_stage_column_types_from_rows(
    adapter: Any,
    column_types: dict[str, str] | None,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> dict[str, str] | None:
    del adapter, columns, rows
    return column_types


def should_ensure_load_target_table(adapter: Any, target_exists: bool) -> bool:
    del adapter
    return not target_exists


def build_load_target_create_kwargs(
    adapter: Any,
    *,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any = None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    write_mode: str,
    original_target_exists: bool,
) -> dict[str, Any]:
    del (
        adapter,
        ch_engine,
        ch_cluster,
        ch_sharding_key,
        ch_only_shard,
        write_mode,
        original_target_exists,
    )
    create_kwargs: dict[str, Any] = {
        "gp_distributed_by_key": gp_distributed_by_key,
    }
    if gp_partitions is not None:
        create_kwargs["gp_partitions"] = gp_partitions
    if partition_by is not None:
        create_kwargs["partition_by"] = partition_by
    if order_by is not None:
        create_kwargs["order_by"] = order_by
    return create_kwargs


def transfer_attempt_policy(adapter: Any, retry_cnt: int) -> TransferAttemptPolicy:
    del adapter, retry_cnt
    return TransferAttemptPolicy(insert_retry_cnt=1, retry_ambiguous_stage_load=True)


def target_connection_defaults(adapter: Any, config: Any) -> TargetConnectionDefaults:
    del adapter, config
    return TargetConnectionDefaults()


def uses_partition_replacement_upsert(adapter: Any) -> bool:
    return adapter.upsert_strategy == "partition_replace"


def needs_upsert_partition_drop_template(adapter: Any) -> bool:
    return bool(adapter.requires_upsert_partition_drop_template)


def supports_distributed_table_targets(adapter: Any) -> bool:
    return bool(adapter.supports_distributed_tables)


def can_create_transfer_target_before_batches(adapter: Any) -> bool:
    return bool(adapter.supports_early_transfer_target_creation)


def cancel_error_result(adapter: Any, error: Exception) -> dict[str, Any] | None:
    del adapter, error
    return None


def validate_ch_create_table_options(
    adapter: Any,
    *,
    option_owner: str,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool = False,
) -> None:
    del partition_by
    if not isinstance(ch_only_shard, bool):
        raise ValueError("ch_only_shard must be a boolean.")
    if not getattr(adapter, "supports_ch_create_table_options", False):
        if not getattr(adapter, "supports_create_table_order_by", True) and order_by is not None:
            raise ValueError(
                f"order_by is not supported when {option_owner} has type '{adapter.backend}'."
            )
    else:
        return
    if ch_only_shard:
        raise ValueError(f"ch_only_shard can only be used when {option_owner} has type 'ch'.")
    if ch_engine != _DEFAULT_CH_ENGINE:
        raise ValueError(f"ch_engine can only be used when {option_owner} has type 'ch'.")
    if ch_cluster != _DEFAULT_CH_CLUSTER:
        raise ValueError(f"ch_cluster can only be used when {option_owner} has type 'ch'.")
    if ch_sharding_key != _DEFAULT_CH_SHARDING_KEY:
        raise ValueError(f"ch_sharding_key can only be used when {option_owner} has type 'ch'.")


def transfer_insert_page_sizing(
    adapter: Any,
    *,
    gp_insert_chunk_size: int | None,
) -> TransferInsertPageSizing | None:
    del adapter, gp_insert_chunk_size
    return None


def requires_load_target_column_metadata(
    adapter: Any,
    *,
    write_mode: str,
    original_target_exists: bool,
) -> bool:
    del adapter
    return write_mode == "upsert" and original_target_exists


def resolve_ch_retry_per_host_drops(adapter: Any, requested: bool) -> bool:
    del adapter, requested
    return False


def expected_create_table_column_types(
    adapter: Any,
    batch: pd.DataFrame,
    column_types: dict[str, str] | None,
    *,
    ch_distributed_table: bool,
    ch_only_shard: bool,
) -> dict[str, str] | None:
    del adapter, batch, column_types, ch_distributed_table, ch_only_shard
    return None


def validate_gp_distributed_by_key_option(
    adapter: Any,
    value: Sequence[str] | None,
    *,
    option_owner: str,
) -> None:
    del adapter
    if value is not None:
        raise ValueError(
            f"gp_distributed_by_key can only be used when {option_owner} has type 'gp'."
        )


def normalize_gp_partitions_option(
    adapter: Any,
    gp_partitions: Any,
    *,
    partition_by: Sequence[str] | str | None,
    option_owner: str,
) -> Any:
    del partition_by
    backend = adapter.backend
    if gp_partitions is not None:
        from analytics_toolkit.sql.connection.errors import (  # noqa: PLC0415
            InvalidSqlInputError,
        )

        message = (
            f"gp_partitions can only be used when {option_owner} has type 'gp', not {backend!r}."
        )
        raise InvalidSqlInputError(message)
    return None


def validate_gp_insert_chunk_size_option(
    adapter: Any,
    value: int | None,
    *,
    option_owner: str,
) -> None:
    del adapter
    if value is not None:
        raise ValueError(
            f"gp_insert_chunk_size can only be used when {option_owner} has type 'gp'."
        )


def validate_trino_insert_chunk_size_option(
    adapter: Any,
    value: int | None,
    *,
    option_owner: str,
) -> None:
    del adapter, option_owner
    if value is not None and value <= 0:
        raise ValueError("trino_insert_chunk_size must be a positive integer.")


def resolve_transfer_staging_mode(
    adapter: Any,
    requested_mode: Any,
    *,
    s3_transfer_staging_schema: str | None,
    s3_transfer_staging_location: str | None,
) -> Any:
    del adapter, s3_transfer_staging_schema, s3_transfer_staging_location
    if requested_mode is None:
        return None
    if requested_mode not in {"parquet", "values"}:
        raise ValueError("trino_mode must be one of: 'parquet', 'values'.")
    raise ValueError("trino_mode can only be used when to_db has type 'trino'.")


def create_table_from_sql_fast_path(adapter: Any, **kwargs: Any) -> tuple[bool, Any]:
    del adapter, kwargs
    return False, None


def uses_create_table_from_sql_fast_path(
    adapter: Any,
    *,
    source_backend: str,
    source_key: str,
    target_key: str,
) -> bool:
    del adapter, source_backend, source_key, target_key
    return False


def should_insert_create_table_from_sql_directly(
    adapter: Any,
    *,
    source_backend: str,
    source_key: str,
    target_key: str,
) -> bool:
    del source_key, target_key
    return source_backend == adapter.backend


def resolve_transfer_stage_column_types(
    adapter: Any,
    connection: Any,
    stage_table: str,
    *,
    connection_key: str,
    current_column_types: dict[str, str] | None,
) -> dict[str, str] | None:
    del adapter, connection, stage_table, connection_key
    return current_column_types


def normalize_insert_batch(adapter: Any, batch: Any) -> Any:
    del adapter
    return batch


def normalize_transfer_source_batch(
    adapter: Any,
    batch: Any,
    source_column_types: dict[str, str | None],
) -> Any:
    del adapter, source_column_types
    return batch


def mark_upsert_finalization_error(adapter: Any, exc: Exception) -> None:
    del adapter, exc


def normalize_insert_rows(
    adapter: Any,
    rows: Sequence[Sequence[Any]],
) -> list[tuple[Any, ...]]:
    del adapter
    return [tuple(row) for row in rows]


def should_wrap_insert_error_as_ambiguous(
    adapter: Any,
    connection: Any,
    exc: Exception,
) -> bool:
    del adapter, connection, exc
    return True


def should_refresh_connection_before_insert_retry(adapter: Any) -> bool:
    del adapter
    return False


def build_create_from_sql_target_create_kwargs(
    adapter: Any,
    *,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any = None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    drop_target_if_exists: bool,
    target_exists_before_drop: bool,
) -> dict[str, Any]:
    del (
        adapter,
        ch_engine,
        ch_cluster,
        ch_sharding_key,
        ch_only_shard,
        drop_target_if_exists,
        target_exists_before_drop,
    )
    create_kwargs: dict[str, Any] = {
        "gp_distributed_by_key": gp_distributed_by_key,
    }
    if gp_partitions is not None:
        create_kwargs["gp_partitions"] = gp_partitions
    if partition_by is not None:
        create_kwargs["partition_by"] = partition_by
    if order_by is not None:
        create_kwargs["order_by"] = order_by
    return create_kwargs


def wait_for_table_absence(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    ch_cluster: str | None = None,
) -> None:
    del adapter, connection, table_name, ch_cluster


def build_vacuum_table_sql(
    adapter: Any,
    table_name: str,
    *,
    analyze: bool = False,
    full: bool = False,
    verbose: bool = True,
) -> str:
    del adapter, table_name, analyze, full, verbose
    raise NotImplementedError


def vacuum_table(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    analyze: bool = False,
    full: bool = False,
    verbose: bool = True,
) -> None:
    sql = adapter.build_vacuum_table_sql(
        table_name,
        analyze=analyze,
        full=full,
        verbose=verbose,
    )
    previous_autocommit = getattr(connection, "autocommit", None)
    cursor = connection.cursor()
    try:
        if previous_autocommit is not None:
            connection.autocommit = True
        cursor.execute(sql)
    finally:
        cursor.close()
        if previous_autocommit is not None:
            connection.autocommit = previous_autocommit


def prepare_existing_target_for_create_from_sql(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    drop_target_if_exists: bool,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    connection_key: str | None = None,
    ch_retry_per_host_drops: bool = True,
) -> bool:
    del ch_only_shard, ch_retry_per_host_drops
    if not drop_target_if_exists:
        return False
    from analytics_toolkit.general import time_print

    time_print(
        f"Dropping existing table {table_name}",
        connection=connection_key or adapter.backend,
        backend=adapter.backend,
    )
    adapter.drop_table(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )
    return False


def estimate_source_rows(
    adapter: Any,
    connection: Any,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> int | None:
    del adapter, connection, source_sql, query_label
    return None


def build_creation_policy_cleanup_sqls(
    adapter: Any,
    table_name: str,
    creation_policy: Any,
    *,
    query_label: str | None = None,
    if_exists: bool = True,
) -> list[str]:
    del adapter, table_name, creation_policy, query_label, if_exists
    return []


def drop_table(
    adapter: Any,
    connection: Any,
    table_name: str,
    **options: Any,
) -> None:
    adapter.execute_command(
        connection,
        adapter.drop_table_sql(table_name, **options),
    )


def preclear_distributed_replace_target(
    adapter: Any,
    *_args: Any,
    **_options: Any,
) -> bool:
    del adapter
    return False


def open_transfer_host_connection(adapter: Any, connection_key: str, host: str) -> Any:
    del adapter, connection_key, host
    message = "This backend does not provide per-host transfer connections."
    raise RuntimeError(message)


def needs_bounded_replace_preclear(adapter: Any, only_shard: object) -> bool:
    del adapter, only_shard
    return False


def after_create_table(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    ch_cluster: str = "{cluster}",
    ch_distributed_table: bool = False,
    ch_only_shard: bool = False,
    expected_column_types: dict[str, str] | None = None,
    ch_creation_policy: Any = None,
) -> None:
    del (
        adapter,
        connection,
        table_name,
        ch_cluster,
        ch_distributed_table,
        ch_only_shard,
        expected_column_types,
        ch_creation_policy,
    )


def _quote_identifier_part_when_needed(adapter: Any, identifier: str) -> str:
    if _is_simple_identifier(identifier):
        return identifier
    return adapter.quote_identifier(identifier)


def _is_simple_identifier(identifier: str) -> bool:
    if not identifier:
        return False
    if not (identifier[0].isalpha() or identifier[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in identifier)
