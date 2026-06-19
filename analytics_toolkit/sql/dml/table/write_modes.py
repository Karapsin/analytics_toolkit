from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from ...backend_adapters import ch_cluster_clause, get_backend_adapter
from ...connection.config import resolve_connection_backend
from ...ddl.api import _create_sql_table_with_connection
from ...execution.labels import apply_query_label
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
) -> None:
    backend = resolve_connection_backend(connection_type)
    time_print(
        f"Upserting staged rows from {stage_table} into {target_table}",
        backend=backend,
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
    ):
        get_backend_adapter(backend).execute_command(connection, sql)


def _build_trino_merge_sql(
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    key_columns: Sequence[str],
) -> str:
    adapter = get_backend_adapter("trino")
    on_predicates = " AND ".join(
        adapter.null_safe_key_equality("target_dst", "stage_src", column_name)
        for column_name in key_columns
    )
    assignments = ",\n  ".join(
        f"{adapter.quote_identifier(column_name)} = "
        f"stage_src.{adapter.quote_identifier(column_name)}"
        for column_name in columns
    )
    insert_columns = adapter.column_list_sql(columns)
    insert_values = ", ".join(
        f"stage_src.{adapter.quote_identifier(column_name)}" for column_name in columns
    )
    return (
        f"MERGE INTO {target_table} AS target_dst\n"
        f"USING {stage_table} AS stage_src\n"
        f"ON {on_predicates}\n"
        "WHEN MATCHED THEN UPDATE SET\n"
        f"  {assignments}\n"
        f"WHEN NOT MATCHED THEN INSERT ({insert_columns})\n"
        f"  VALUES ({insert_values})"
    )


def _build_trino_merge_placeholder_sql(
    target_table: str,
    stage_table: str,
    *,
    key_columns: Sequence[str],
) -> str:
    adapter = get_backend_adapter("trino")
    on_predicates = " AND ".join(
        adapter.null_safe_key_equality("target_dst", "stage_src", column_name)
        for column_name in key_columns
    )
    return (
        f"MERGE INTO {target_table} AS target_dst\n"
        f"USING {stage_table} AS stage_src\n"
        f"ON {on_predicates}\n"
        "WHEN MATCHED THEN UPDATE SET\n"
        "  <source query columns>\n"
        "WHEN NOT MATCHED THEN INSERT (<source query columns>)\n"
        "  VALUES (<source query columns>)"
    )


def _build_gp_delete_matching_stage_sql(
    target_table: str,
    stage_table: str,
    key_columns: Sequence[str],
) -> str:
    adapter = get_backend_adapter("gp")
    predicates = " AND ".join(
        adapter.null_safe_key_equality("target_dst", "stage_src", column_name)
        for column_name in key_columns
    )
    return (
        f"DELETE FROM {target_table} AS target_dst\n"
        f"USING {stage_table} AS stage_src\n"
        f"WHERE {predicates}"
    )


def _build_ch_delete_matching_stage_sql(
    target_table: str,
    stage_table: str,
    key_columns: Sequence[str],
    *,
    ch_cluster: str | None,
) -> str:
    target_tuple = _build_ch_normalized_key_tuple(key_columns)
    stage_tuple = _build_ch_normalized_key_tuple(key_columns)
    return (
        f"DELETE FROM {target_table}{ch_cluster_clause(ch_cluster)}\n"
        f"WHERE {target_tuple} IN (\n"
        f"  SELECT {stage_tuple} FROM {stage_table}\n"
        ")"
    )


def _build_ch_normalized_key_tuple(key_columns: Sequence[str]) -> str:
    adapter = get_backend_adapter("ch")
    expressions: list[str] = []
    for column_name in key_columns:
        quoted_column = adapter.quote_identifier(column_name)
        expressions.extend(
            [
                f"isNull({quoted_column})",
                f"ifNull(toString({quoted_column}), '')",
            ]
        )
    return "tuple(" + ", ".join(expressions) + ")"


def _build_insert_from_stage_sql(
    backend: str,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    column_types: Mapping[str, str] | None,
    query_label: str | None,
) -> str:
    typed_columns = _column_types_for_columns(column_types, columns)
    return apply_query_label(
        _build_explicit_insert_from_stage_sql(
            backend,
            target_table,
            stage_table,
            columns=columns,
            column_types=typed_columns,
        ),
        query_label,
    )


def _build_explicit_insert_from_stage_sql(
    backend: str,
    target_table: str,
    stage_table: str,
    *,
    columns: Sequence[str],
    column_types: Mapping[str, str] | None,
) -> str:
    adapter = get_backend_adapter(backend)
    if column_types:
        return adapter.build_insert_from_table_sql(
            target_table,
            stage_table,
            column_types,
        )

    target_columns = adapter.column_list_sql(columns)
    selected_columns = ", ".join(adapter.quote_identifier(column) for column in columns)
    return (
        f"INSERT INTO {target_table} ({target_columns}) "
        f"SELECT {selected_columns} FROM {stage_table}"
    )


def _build_insert_from_stage_placeholder_sql(
    backend: str,
    target_table: str,
    stage_table: str,
    *,
    query_label: str | None,
) -> str:
    del backend
    return apply_query_label(
        f"INSERT INTO {target_table} (<source query columns>) "
        f"SELECT <source query columns> FROM {stage_table}",
        query_label,
    )


def _column_types_for_columns(
    column_types: Mapping[str, str] | None,
    columns: Sequence[str],
) -> dict[str, str] | None:
    if column_types is None:
        return None

    missing_columns = [column for column in columns if column not in column_types]
    if missing_columns:
        raise ValueError(
            "Target table is missing staged column(s): "
            + ", ".join(missing_columns)
        )
    return {column: column_types[column] for column in columns}


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
) -> None:
    backend = resolve_connection_backend(connection_type)
    time_print(
        f"Finalizing staged transfer from {stage_table} into {target_table}",
        connection=connection_key or connection_type,
        backend=backend,
    )
    original_target_exists = target_exists

    if write_mode == "upsert":
        if not target_exists:
            target_exists = _ensure_stage_target_table(
                backend=backend,
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
            insert_from_table(
                backend,
                connection,
                target_table,
                stage_table,
                column_types=insert_column_types,
                query_label=query_label,
            )
            return

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
                connection_key=connection_key,
                ch_replace_table=False,
                ch_only_shard=ch_only_shard,
            )

        upsert_stage_table(
            backend,
            connection,
            target_table,
            stage_table,
            columns=list(
                insert_column_types
                or target_column_types
                or sample_batch.columns
            ),
            key_columns=key_columns or [],
            column_types=insert_column_types,
            ch_cluster=ch_cluster,
            ch_only_shard=ch_only_shard,
            query_label=query_label,
        )
        return

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
            connection_key=connection_key,
            ch_replace_table=(
                original_target_exists
                and replace_target_table
                and write_mode == "replace"
                and not ch_only_shard
            ),
            ch_only_shard=ch_only_shard,
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
        _create_sql_table_with_connection(
            backend,
            connection,
            target_table,
            None if target_column_types is not None else sample_batch,
            connection_key=connection_key or backend,
            table_schema=target_column_types,
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
    if backend == "ch":
        _ensure_ch_distributed_target_pair(
            backend,
            connection,
            target_table,
            sample_batch,
            target_exists=False,
            target_column_types=target_column_types,
            insert_column_types=target_column_types,
            gp_distributed_by_key=gp_distributed_by_key,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            query_label=query_label,
            connection_key=connection_key,
            ch_replace_table=False,
            ch_only_shard=ch_only_shard,
        )
        return True

    create_kwargs: dict[str, Any] = {}
    if partition_by is not None:
        create_kwargs["partition_by"] = partition_by
    if order_by is not None:
        create_kwargs["order_by"] = order_by
    _create_sql_table_with_connection(
        backend,
        connection,
        target_table,
        None if target_column_types is not None else sample_batch,
        connection_key=connection_key or backend,
        table_schema=target_column_types,
        gp_distributed_by_key=gp_distributed_by_key,
        query_label=query_label,
        **create_kwargs,
    )
    return True


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

    _create_sql_table_with_connection(
        connection_type,
        connection,
        target_table,
        None if create_column_types is not None else create_batch,
        connection_key=connection_key or connection_type,
        table_schema=create_column_types,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=not ch_only_shard,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        query_label=query_label,
    )
