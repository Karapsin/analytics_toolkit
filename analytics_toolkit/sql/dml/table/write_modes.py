from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from ...backend_adapters import get_backend_adapter
from ...connection.config import resolve_connection_backend
from ...ddl.api import create_sql_table
from ...execution.plans import SqlOperationMetadata, SqlPlan
from analytics_toolkit.general import time_print
from ._basic_ops import (
    build_clear_table_sqls,
    get_table_column_types,
    insert_from_table,
)
from .maintenance import (
    clear_ch_distributed_table_data,
    drop_ch_distributed_table_pair,
    drop_table,
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
    ch_retry_per_host_drops_concurrency: int | None = None,
    ch_only_shard: bool = False,
) -> bool:
    backend = resolve_connection_backend(connection_type)
    log_connection = connection_label or connection_type
    if write_mode == "append":
        return target_exists

    if backend == "ch":
        if ch_only_shard:
            if write_mode == "truncate_insert" and target_exists:
                clear_target_table(
                    backend,
                    connection,
                    table_name,
                    query_label=query_label,
                )
                return True
            if write_mode == "truncate_insert" and not drop_missing_ch_truncate_target:
                return False

            time_print(f"Dropping existing ClickHouse table {table_name}")
            drop_table(
                backend,
                connection,
                table_name,
                ch_cluster=None,
                query_label=query_label,
            )
            return False

        if write_mode == "truncate_insert" and target_exists:
            clear_ch_distributed_table_data(
                connection,
                table_name,
                ch_cluster=ch_cluster,
                query_label=query_label,
            )
            return True
        if write_mode == "truncate_insert" and not drop_missing_ch_truncate_target:
            return False

        time_print(
            "Dropping existing ClickHouse distributed table pair "
            f"{table_name}"
        )
        drop_ch_distributed_table_pair(
            connection,
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
            wait_for_absence=True,
            connection_key=connection_key,
            ch_retry_per_host_drops=ch_retry_per_host_drops,
            ch_retry_per_host_drops_concurrency=ch_retry_per_host_drops_concurrency,
        )
        return False

    if not target_exists:
        return False

    if write_mode == "truncate_insert" or replace_existing_non_ch == "clear":
        clear_target_table(
            backend,
            connection,
            table_name,
            query_label=query_label,
        )
        return True

    if replace_existing_non_ch == "drop":
        time_print(
            f"Dropping existing table {table_name}",
            connection=log_connection,
            backend=backend,
        )
        drop_table(
            backend,
            connection,
            table_name,
            query_label=query_label,
        )
        return False

    raise ValueError("replace_existing_non_ch must be one of: clear, drop.")

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
    gp_distributed_by_key: list[str] | None = None,
    partition_by: list[str] | str | None = None,
    order_by: list[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    query_label: str | None = None,
    connection_key: str | None = None,
    ch_retry_per_host_drops: bool = True,
    ch_retry_per_host_drops_concurrency: int | None = None,
    ch_only_shard: bool = False,
) -> None:
    backend = resolve_connection_backend(connection_type)
    time_print(
        f"Finalizing staged transfer from {stage_table} into {target_table}",
        connection=connection_key or connection_type,
        backend=backend,
    )
    original_target_exists = target_exists

    if replace_target_table:
        target_exists = apply_target_write_mode(
            backend,
            connection,
            target_table,
            write_mode=write_mode,
            target_exists=target_exists,
            replace_existing_non_ch="clear",
            ch_cluster=ch_cluster,
            query_label=query_label,
            connection_key=connection_key,
            ch_retry_per_host_drops=ch_retry_per_host_drops,
            ch_retry_per_host_drops_concurrency=ch_retry_per_host_drops_concurrency,
            ch_only_shard=ch_only_shard,
        )

    if backend == "ch":
        _ensure_ch_distributed_target_pair(
            connection_type,
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
            ch_replace_table=(
                original_target_exists
                and replace_target_table
                and write_mode == "replace"
                and not ch_only_shard
            ),
            only_shard=ch_only_shard,
        )
        insert_from_table(
            backend,
            connection,
            target_table,
            stage_table,
            column_types=insert_column_types,
            query_label=query_label,
        )
        return

    if not target_exists:
        create_kwargs: dict[str, Any] = {}
        if partition_by is not None:
            create_kwargs["partition_by"] = partition_by
        if order_by is not None:
            create_kwargs["order_by"] = order_by
        create_sql_table(
            backend,
            connection,
            target_table,
            sample_batch,
            column_types=target_column_types,
            gp_distributed_by_key=gp_distributed_by_key,
            query_label=query_label,
            **create_kwargs,
        )

    insert_from_table(
        backend,
        connection,
        target_table,
        stage_table,
        column_types=insert_column_types,
        query_label=query_label,
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
    ch_replace_table: bool = False,
    only_shard: bool = False,
) -> None:
    create_batch = sample_batch
    create_column_types = target_column_types or insert_column_types
    if target_exists:
        existing_column_types = get_table_column_types(
            connection_type,
            connection,
            target_table,
        )
        if existing_column_types:
            create_batch = pd.DataFrame(columns=list(existing_column_types))
            create_column_types = existing_column_types

    create_sql_table(
        connection_type,
        connection,
        target_table,
        create_batch,
        column_types=create_column_types,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=not only_shard,
        only_shard=only_shard,
        ch_replace_table=ch_replace_table,
        query_label=query_label,
    )
