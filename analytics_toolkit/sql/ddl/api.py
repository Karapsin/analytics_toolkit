from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from ..backend_adapters import get_backend_adapter
from ..connection.config import resolve_connection_backend
from ..execution.operation_runner import timed_public_sql_function, tracked_sql_operation
from ..execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ..clickhouse.wait import _wait_for_ch_distributed_table_pair
from analytics_toolkit.general import time_print
from .builders import (
    _apply_query_label_to_sqls,
    _build_backend_create_table_sqls,
    _validate_only_shard,
)
from .models import CreateSqlTableOptions
from .schema import (
    _build_column_definitions,
    _build_expected_ch_column_types,
    _resolve_create_column_types,
)


@timed_public_sql_function
def create_sql_table(
    connection_type: str,
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    only_shard: bool = False,
    ch_replace_table: bool = False,
    dry_run: bool = False,
    return_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: Mapping[str, str] | None = None,
) -> SqlPlan | SqlOperationResult | None:
    backend = resolve_connection_backend(connection_type)
    options = CreateSqlTableOptions(
        connection_type=connection_type,
        backend=backend,
        connection=connection,
        table_name=table_name,
        batch=batch,
        column_types=column_types,
        table_schema=(
            _resolve_create_column_types(
                table_schema=table_schema,
                column_types=column_types,
                columns=batch.columns,
            )
            if table_schema is not None
            else None
        ),
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        only_shard=only_shard,
        ch_replace_table=ch_replace_table,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
    )
    time_print(
        f"Creating target table {table_name}",
        connection=connection_type,
        backend=backend,
    )
    create_sqls = build_create_table_sqls(
        options.backend,
        options.table_name,
        options.batch,
        column_types=options.column_types,
        table_schema=options.table_schema,
        gp_distributed_by_key=options.gp_distributed_by_key,
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_distributed_table=options.ch_distributed_table,
        only_shard=options.only_shard,
        ch_replace_table=options.ch_replace_table,
        query_label=options.query_label,
    )
    expected_ch_column_types = (
        _build_expected_ch_column_types(
            options.batch,
            _resolve_create_column_types(
                table_schema=options.table_schema,
                column_types=options.column_types,
                columns=options.batch.columns,
            ),
        )
        if (
            options.backend == "ch"
            and options.ch_distributed_table
            and not options.only_shard
        )
        else None
    )
    metadata = SqlOperationMetadata(
        statement_count=len(create_sqls),
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="create_table",
        target_alias=options.connection_type,
        target_backend=options.backend,
        target_table=options.table_name,
        metadata=metadata,
    )
    plan.extend(
        create_sqls,
        alias=options.connection_type,
        backend=options.backend,
        phase="create_table",
        target_table=options.table_name,
    )

    if options.dry_run or options.return_sql:
        return plan

    with tracked_sql_operation(
        metadata=metadata,
        operation_name="create_sql_table",
        alias=options.connection_type,
        backend=options.backend,
        phase="create_target",
        query_label=options.query_label,
        preview_sql=create_sqls[0] if create_sqls else None,
    ):
        get_backend_adapter(options.backend).execute_commands(options.connection, create_sqls)
        if options.backend == "ch":
            if options.ch_distributed_table and not options.only_shard:
                _wait_for_ch_distributed_table_pair(
                    options.connection,
                    options.table_name,
                    ch_cluster=options.ch_cluster,
                    expected_column_types=expected_ch_column_types,
                )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None

@timed_public_sql_function
def build_create_table_sql(
    connection_type: str,
    table_name: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    only_shard: bool = False,
    ch_replace_table: bool = False,
    query_label: str | None = None,
    table_schema: Mapping[str, str] | None = None,
) -> str:
    return ";\n".join(
        build_create_table_sqls(
            connection_type,
            table_name,
            batch,
            column_types=column_types,
            table_schema=table_schema,
            gp_distributed_by_key=gp_distributed_by_key,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=ch_distributed_table,
            only_shard=only_shard,
            ch_replace_table=ch_replace_table,
            query_label=query_label,
        )
    )

@timed_public_sql_function
def build_create_table_sqls(
    connection_type: str,
    table_name: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    only_shard: bool = False,
    ch_replace_table: bool = False,
    query_label: str | None = None,
    table_schema: Mapping[str, str] | None = None,
) -> list[str]:
    backend = resolve_connection_backend(connection_type)
    _validate_only_shard(backend, only_shard, "connection_type")
    resolved_column_types = _resolve_create_column_types(
        table_schema=table_schema,
        column_types=column_types,
        columns=batch.columns,
    )
    joined_columns = _build_column_definitions(
        backend,
        batch,
        resolved_column_types,
    )
    return _apply_query_label_to_sqls(
        _build_backend_create_table_sqls(
            backend=backend,
            table_name=table_name,
            joined_columns=joined_columns,
            gp_distributed_by_key=gp_distributed_by_key,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=ch_distributed_table,
            only_shard=only_shard,
            ch_replace_table=ch_replace_table,
        ),
        query_label,
    )
