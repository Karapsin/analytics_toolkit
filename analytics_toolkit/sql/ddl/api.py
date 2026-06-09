from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from ..backend_adapters import get_backend_adapter
from ..connection.config import get_connection_config, resolve_connection_backend
from ..connection.errors import SqlOperationContext, sql_preview
from ..connection.get_sql_connection import get_sql_connection
from ..execution.operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
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
    normalize_table_schema,
)


@timed_public_sql_function
def create_sql_table(
    db_key: str,
    table_name: str,
    df: pd.DataFrame | None = None,
    *,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_only_shard: bool = False,
    ch_replace_table: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    dry_run: bool = False,
    return_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: Mapping[str, str] | None = None,
) -> SqlPlan | SqlOperationResult | None:
    config = get_connection_config(db_key)
    validate_retry_options(retry_cnt, timeout_increment)
    options = _build_create_table_options(
        connection_key=config.connection_key,
        backend=config.backend,
        table_name=table_name,
        df=df,
        column_types=column_types,
        table_schema=table_schema,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        option_owner="db_key",
    )
    create_sqls = _build_create_sql_table_sqls(options, option_owner="db_key")
    metadata, plan = _build_create_table_plan(options, create_sqls)

    if options.dry_run or options.return_sql:
        return plan

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        _execute_create_sql_table(
            options=options,
            connection=connection_ref["connection"],
            create_sqls=create_sqls,
            metadata=metadata,
            retry_attempt=attempt,
        )

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="create_sql_table",
            alias=options.connection_key,
            backend=options.backend,
            phase="create_target",
            target_table=options.table_name,
            retry_attempt=attempt,
            sql_preview=sql_preview(create_sqls[0] if create_sqls else options.table_name),
        )

    run_connection_operation(
        operation_name=(
            f"creating table {options.connection_key}.{options.table_name} "
            f"({options.backend})"
        ),
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


@timed_public_sql_function
def build_create_table_sql(
    db_key: str,
    table_name: str,
    df: pd.DataFrame | None = None,
    *,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_only_shard: bool = False,
    ch_replace_table: bool = False,
    query_label: str | None = None,
    table_schema: Mapping[str, str] | None = None,
) -> str:
    config = get_connection_config(db_key)
    options = _build_create_table_options(
        connection_key=config.connection_key,
        backend=config.backend,
        table_name=table_name,
        df=df,
        column_types=column_types,
        table_schema=table_schema,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        dry_run=False,
        return_sql=False,
        query_label=query_label,
        return_metadata=False,
        option_owner="db_key",
    )
    return ";\n".join(_build_create_sql_table_sqls(options, option_owner="db_key"))


def _create_sql_table_with_connection(
    backend: str,
    connection: Any,
    table_name: str,
    df: pd.DataFrame | None = None,
    *,
    connection_key: str | None = None,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_only_shard: bool = False,
    ch_replace_table: bool = False,
    dry_run: bool = False,
    return_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: Mapping[str, str] | None = None,
) -> SqlPlan | SqlOperationResult | None:
    resolved_backend = resolve_connection_backend(backend)
    resolved_key = connection_key or resolved_backend
    options = _build_create_table_options(
        connection_key=resolved_key,
        backend=resolved_backend,
        table_name=table_name,
        df=df,
        column_types=column_types,
        table_schema=table_schema,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        option_owner="connection",
    )
    create_sqls = _build_create_sql_table_sqls(options, option_owner="connection")
    metadata, plan = _build_create_table_plan(options, create_sqls)

    if options.dry_run or options.return_sql:
        return plan

    _execute_create_sql_table(
        options=options,
        connection=connection,
        create_sqls=create_sqls,
        metadata=metadata,
        retry_attempt=None,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


def _build_create_table_options(
    *,
    connection_key: str,
    backend: str,
    table_name: str,
    df: pd.DataFrame | None,
    column_types: Mapping[str, str] | None,
    table_schema: Mapping[str, str] | None,
    gp_distributed_by_key: list[str] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_distributed_table: bool,
    ch_only_shard: bool,
    ch_replace_table: bool,
    dry_run: bool,
    return_sql: bool,
    query_label: str | None,
    return_metadata: bool,
    option_owner: str,
) -> CreateSqlTableOptions:
    _validate_only_shard(backend, ch_only_shard, option_owner)
    create_df, normalized_schema = _resolve_create_dataframe_and_schema(
        df=df,
        table_schema=table_schema,
    )
    if table_name.strip() == "":
        raise ValueError("table_name must not be empty.")
    return CreateSqlTableOptions(
        connection_key=connection_key,
        backend=backend,
        table_name=table_name,
        df=create_df,
        column_types=column_types,
        table_schema=(
            _resolve_create_column_types(
                table_schema=normalized_schema,
                column_types=column_types,
                columns=create_df.columns,
            )
            if normalized_schema is not None
            else None
        ),
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
    )


def _resolve_create_dataframe_and_schema(
    *,
    df: pd.DataFrame | None,
    table_schema: Mapping[str, str] | None,
) -> tuple[pd.DataFrame, dict[str, str] | None]:
    if df is None:
        normalized_schema = normalize_table_schema(table_schema)
        if normalized_schema is None:
            raise ValueError("Either df or table_schema must be provided.")
        return pd.DataFrame(columns=list(normalized_schema)), normalized_schema

    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame.")
    normalized_schema = normalize_table_schema(table_schema, columns=df.columns)
    return df, normalized_schema


def _build_create_sql_table_sqls(
    options: CreateSqlTableOptions,
    *,
    option_owner: str,
) -> list[str]:
    return _build_create_table_sqls(
        options.backend,
        options.table_name,
        options.df,
        column_types=options.column_types,
        table_schema=options.table_schema,
        gp_distributed_by_key=options.gp_distributed_by_key,
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_distributed_table=options.ch_distributed_table,
        ch_only_shard=options.ch_only_shard,
        ch_replace_table=options.ch_replace_table,
        query_label=options.query_label,
        option_owner=option_owner,
    )


def _build_create_table_plan(
    options: CreateSqlTableOptions,
    create_sqls: list[str],
) -> tuple[SqlOperationMetadata, SqlPlan]:
    metadata = SqlOperationMetadata(
        statement_count=len(create_sqls),
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="create_table",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.table_name,
        metadata=metadata,
    )
    plan.extend(
        create_sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="create_table",
        target_table=options.table_name,
    )
    return metadata, plan


def _execute_create_sql_table(
    *,
    options: CreateSqlTableOptions,
    connection: Any,
    create_sqls: list[str],
    metadata: SqlOperationMetadata,
    retry_attempt: int | None,
) -> None:
    expected_ch_column_types = (
        _build_expected_ch_column_types(
            options.df,
            _resolve_create_column_types(
                table_schema=options.table_schema,
                column_types=options.column_types,
                columns=options.df.columns,
            ),
        )
        if (
            options.backend == "ch"
            and options.ch_distributed_table
            and not options.ch_only_shard
        )
        else None
    )

    time_print(
        f"Creating target table {options.table_name}",
        connection=options.connection_key,
        backend=options.backend,
    )
    with tracked_sql_operation(
        metadata=metadata,
        operation_name="create_sql_table",
        alias=options.connection_key,
        backend=options.backend,
        phase="create_target",
        retry_attempt=retry_attempt,
        query_label=options.query_label,
        preview_sql=create_sqls[0] if create_sqls else None,
    ):
        get_backend_adapter(options.backend).execute_commands(connection, create_sqls)
        if options.backend == "ch":
            if options.ch_distributed_table and not options.ch_only_shard:
                _wait_for_ch_distributed_table_pair(
                    connection,
                    options.table_name,
                    ch_cluster=options.ch_cluster,
                    expected_column_types=expected_ch_column_types,
                )


def _build_create_table_sqls(
    backend: str,
    table_name: str,
    df: pd.DataFrame,
    *,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: list[str] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_only_shard: bool = False,
    ch_replace_table: bool = False,
    query_label: str | None = None,
    table_schema: Mapping[str, str] | None = None,
    option_owner: str = "db_key",
) -> list[str]:
    _validate_only_shard(backend, ch_only_shard, option_owner)
    resolved_column_types = _resolve_create_column_types(
        table_schema=table_schema,
        column_types=column_types,
        columns=df.columns,
    )
    joined_columns = _build_column_definitions(
        backend,
        df,
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
            ch_only_shard=ch_only_shard,
            ch_replace_table=ch_replace_table,
        ),
        query_label,
    )
