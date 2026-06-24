from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from ...backends.base import (
    StageFinalizationRequest,
    StageTargetTableRequest,
    TargetWriteModeRequest,
)
from ...backend_adapters import get_backend_adapter
from ...connection.config import resolve_connection_backend
from ...execution.plans import SqlOperationMetadata, SqlPlan
from analytics_toolkit.general import time_print
from ._basic_ops import (
    build_clear_table_sqls,
)


def clear_target_table(
    connection_type: str,
    connection: Any,
    table_name: str,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
) -> SqlPlan | None:
    backend = resolve_connection_backend(connection_type)
    time_print(
        f"Clearing target table {table_name}",
        connection=connection_type,
        backend=backend,
    )
    if dry_run or return_sql:
        sqls = build_clear_table_sqls(
            backend,
            table_name,
            query_label=query_label,
        )
        plan = SqlPlan(
            operation="clear_target_table",
            target_alias=connection_type,
            target_backend=backend,
            target_table=table_name,
            metadata=SqlOperationMetadata(
                statement_count=len(sqls),
                query_label=query_label,
            ),
        )
        plan.extend(
            sqls,
            alias=connection_type,
            backend=backend,
            phase="clear_target",
            target_table=table_name,
        )
        return plan
    get_backend_adapter(backend).clear_table(
        connection,
        table_name,
        query_label=query_label,
    )
    return None

def apply_target_write_mode(
    connection_type: str,
    connection: Any,
    table_name: str,
    *,
    write_mode: str,
    target_exists: bool,
    replace_existing_non_ch: str,
    ch_cluster: str = "{cluster}",
    connection_label: str | None = None,
    drop_missing_ch_truncate_target: bool = True,
    query_label: str | None = None,
    connection_key: str | None = None,
    ch_retry_per_host_drops: bool = True,
    ch_only_shard: bool = False,
) -> bool:
    backend = resolve_connection_backend(connection_type)
    log_connection = connection_label or connection_type
    return get_backend_adapter(backend).apply_target_write_mode(
        TargetWriteModeRequest(
            connection=connection,
            table_name=table_name,
            write_mode=write_mode,
            target_exists=target_exists,
            replace_existing_non_ch=replace_existing_non_ch,
            ch_cluster=ch_cluster,
            connection_label=log_connection,
            drop_missing_ch_truncate_target=drop_missing_ch_truncate_target,
            query_label=query_label,
            connection_key=connection_key,
            ch_retry_per_host_drops=ch_retry_per_host_drops,
            ch_only_shard=ch_only_shard,
        )
    )

def build_upsert_stage_sqls(
    connection_type: str,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
    column_types: Mapping[str, str] | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    upsert_partition_column: str | None = None,
    final_stage_table: str | None = None,
    incoming_stage_tables: Sequence[str] | None = None,
    partition_values: Sequence[Any] | None = None,
    trino_partition_drop_sql_template: str | None = None,
) -> list[str]:
    backend = resolve_connection_backend(connection_type)
    if not key_columns:
        raise ValueError("key_columns are required for write_mode='upsert'.")
    if not columns:
        raise ValueError("columns are required for write_mode='upsert'.")

    return get_backend_adapter(backend).build_upsert_stage_sqls(
        target_table,
        stage_table,
        columns=columns,
        key_columns=key_columns,
        column_types=column_types,
        ch_cluster=ch_cluster,
        ch_only_shard=ch_only_shard,
        query_label=query_label,
        upsert_partition_column=upsert_partition_column,
        final_stage_table=final_stage_table,
        incoming_stage_tables=incoming_stage_tables,
        partition_values=partition_values,
        trino_partition_drop_sql_template=trino_partition_drop_sql_template,
    )


def build_upsert_stage_placeholder_sqls(
    connection_type: str,
    target_table: str,
    stage_table: str,
    *,
    key_columns: Sequence[str],
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    upsert_partition_column: str | None = None,
    final_stage_table: str | None = None,
    incoming_stage_tables: Sequence[str] | None = None,
    partition_values: Sequence[Any] | None = None,
    trino_partition_drop_sql_template: str | None = None,
) -> list[str]:
    backend = resolve_connection_backend(connection_type)
    if not key_columns:
        raise ValueError("key_columns are required for write_mode='upsert'.")

    return get_backend_adapter(backend).build_upsert_stage_placeholder_sqls(
        target_table,
        stage_table,
        key_columns=key_columns,
        ch_cluster=ch_cluster,
        ch_only_shard=ch_only_shard,
        query_label=query_label,
        upsert_partition_column=upsert_partition_column,
        final_stage_table=final_stage_table,
        incoming_stage_tables=incoming_stage_tables,
        partition_values=partition_values,
        trino_partition_drop_sql_template=trino_partition_drop_sql_template,
    )


def upsert_stage_table(
    connection_type: str,
    connection: Any,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
    column_types: Mapping[str, str] | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    upsert_partition_column: str | None = None,
    final_stage_table: str | None = None,
    incoming_stage_tables: Sequence[str] | None = None,
    trino_partition_drop_sql_template: str | None = None,
) -> None:
    backend = resolve_connection_backend(connection_type)
    time_print(
        f"Upserting staged rows from {stage_table} into {target_table}",
        backend=backend,
    )
    partition_values = None
    if upsert_partition_column is not None:
        partition_values = get_backend_adapter(backend).fetch_upsert_partition_values(
            connection,
            stage_table,
            partition_column=upsert_partition_column,
            incoming_stage_tables=incoming_stage_tables,
        )
    for sql in build_upsert_stage_sqls(
        backend,
        target_table,
        stage_table,
        columns=columns,
        key_columns=key_columns,
        column_types=column_types,
        ch_cluster=ch_cluster,
        ch_only_shard=ch_only_shard,
        query_label=query_label,
        upsert_partition_column=upsert_partition_column,
        final_stage_table=final_stage_table,
        incoming_stage_tables=incoming_stage_tables,
        partition_values=partition_values,
        trino_partition_drop_sql_template=trino_partition_drop_sql_template,
    ):
        get_backend_adapter(backend).execute_command(connection, sql)


def _build_trino_merge_sql(
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
) -> str:
    return get_backend_adapter("trino")._build_merge_sql(
        target_table,
        stage_table,
        columns=columns,
        key_columns=key_columns,
    )


def _build_trino_merge_placeholder_sql(
    target_table: str,
    stage_table: str,
    *,
    key_columns: Sequence[str],
) -> str:
    return get_backend_adapter("trino")._build_merge_placeholder_sql(
        target_table,
        stage_table,
        key_columns=key_columns,
    )


def _build_gp_delete_matching_stage_sql(
    target_table: str,
    stage_table: str,
    key_columns: Sequence[str],
) -> str:
    return get_backend_adapter("gp")._build_delete_matching_stage_sql(
        target_table,
        stage_table,
        key_columns,
    )


def _build_ch_delete_matching_stage_sql(
    target_table: str,
    stage_table: str,
    key_columns: Sequence[str],
    *,
    ch_cluster: str | None,
) -> str:
    return get_backend_adapter("ch")._build_delete_matching_stage_sql(
        target_table,
        stage_table,
        key_columns,
        ch_cluster=ch_cluster,
    )


def _build_ch_normalized_key_tuple(key_columns: Sequence[str]) -> str:
    return get_backend_adapter("ch")._build_normalized_key_tuple(key_columns)


def _build_insert_from_stage_sql(
    backend: str,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    column_types: Mapping[str, str] | None,
    query_label: str | None,
) -> str:
    return get_backend_adapter(backend).build_insert_from_stage_sql(
        target_table,
        stage_table,
        columns=columns,
        column_types=column_types,
        query_label=query_label,
    )


def _build_explicit_insert_from_stage_sql(
    backend: str,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    column_types: Mapping[str, str] | None,
) -> str:
    return get_backend_adapter(backend).build_explicit_insert_from_stage_sql(
        target_table,
        stage_table,
        columns=columns,
        column_types=column_types,
    )


def _build_insert_from_stage_placeholder_sql(
    backend: str,
    target_table: str,
    stage_table: str,
    *,
    query_label: str | None,
) -> str:
    return get_backend_adapter(backend).build_insert_from_stage_placeholder_sql(
        target_table,
        stage_table,
        query_label=query_label,
    )


def _column_types_for_columns(
    column_types: Mapping[str, str] | None,
    columns: Sequence[str],
) -> dict[str, str] | None:
    return get_backend_adapter("gp").column_types_for_columns(column_types, columns)


def finalize_stage_table(
    connection_type: str,
    connection: Any,
    stage_table: str,
    target_table: str,
    replace_target_table: bool,
    target_exists: bool,
    sample_batch: pd.DataFrame,
    target_column_types: Mapping[str, str] | None = None,
    insert_column_types: Mapping[str, str] | None = None,
    write_mode: str = "replace",
    key_columns: list[str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: list[str] | str | None = None,
    order_by: list[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    query_label: str | None = None,
    connection_key: str | None = None,
    ch_retry_per_host_drops: bool = True,
    ch_only_shard: bool = False,
    upsert_partition_column: str | None = None,
    final_upsert_stage_table: str | None = None,
    incoming_stage_tables: list[str] | None = None,
    trino_upsert_partition_drop_sql_template: str | None = None,
) -> None:
    backend = resolve_connection_backend(connection_type)
    time_print(
        f"Finalizing staged transfer from {stage_table} into {target_table}",
        connection=connection_key or connection_type,
        backend=backend,
    )
    get_backend_adapter(backend).finalize_stage_table(
        StageFinalizationRequest(
            connection=connection,
            stage_table=stage_table,
            target_table=target_table,
            replace_target_table=replace_target_table,
            target_exists=target_exists,
            sample_batch=sample_batch,
            target_column_types=target_column_types,
            insert_column_types=insert_column_types,
            write_mode=write_mode,
            key_columns=key_columns,
            gp_distributed_by_key=gp_distributed_by_key,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            query_label=query_label,
            connection_key=connection_key,
            ch_retry_per_host_drops=ch_retry_per_host_drops,
            ch_only_shard=ch_only_shard,
            upsert_partition_column=upsert_partition_column,
            final_upsert_stage_table=final_upsert_stage_table,
            incoming_stage_tables=incoming_stage_tables,
            trino_upsert_partition_drop_sql_template=(
                trino_upsert_partition_drop_sql_template
            ),
        )
    )


def _ensure_stage_target_table(
    *,
    backend: str,
    connection: Any,
    target_table: str,
    sample_batch: pd.DataFrame,
    target_column_types: Mapping[str, str] | None,
    gp_distributed_by_key: list[str] | None,
    partition_by: list[str] | str | None,
    order_by: list[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    query_label: str | None,
    connection_key: str | None,
    ch_only_shard: bool = False,
) -> bool:
    return get_backend_adapter(backend).ensure_stage_target_table(
        StageTargetTableRequest(
            connection=connection,
            target_table=target_table,
            sample_batch=sample_batch,
            target_column_types=target_column_types,
            gp_distributed_by_key=gp_distributed_by_key,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            query_label=query_label,
            connection_key=connection_key,
            ch_only_shard=ch_only_shard,
        )
    )


def _ensure_ch_distributed_target_pair(
    connection_type: str,
    connection: Any,
    target_table: str,
    sample_batch: pd.DataFrame,
    *,
    target_exists: bool,
    target_column_types: Mapping[str, str] | None,
    insert_column_types: Mapping[str, str] | None,
    gp_distributed_by_key: list[str] | None,
    partition_by: list[str] | str | None,
    order_by: list[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    query_label: str | None,
    connection_key: str | None,
    ch_replace_table: bool = False,
    ch_only_shard: bool = False,
) -> None:
    get_backend_adapter(connection_type).ensure_distributed_target_pair(
        connection,
        target_table,
        sample_batch,
        target_exists=target_exists,
        target_column_types=target_column_types,
        insert_column_types=insert_column_types,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        query_label=query_label,
        connection_key=connection_key,
        ch_replace_table=ch_replace_table,
        ch_only_shard=ch_only_shard,
    )
