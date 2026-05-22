from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any

import pandas as pd

from ...backend_adapters import (
    ch_cluster_clause,
    extract_row_count,
    format_ch_cluster_name,
    format_gp_information_schema_type,
    get_backend_adapter,
    is_simple_identifier,
    split_gp_table_name,
    split_trino_table_name as _adapter_split_trino_table_name,
)
from ...ch_lifecycle import (
    build_drop_ch_distributed_table_pair_sqls as _build_ch_pair_drop_sqls,
    drop_ch_distributed_table_pair as _drop_ch_pair,
    truncate_ch_distributed_table_pair as _truncate_ch_pair,
)
from ...connection.config import (
    get_connection_config,
    resolve_connection_backend,
)
from ...connection.get_sql_connection import (
    get_ch_connection_for_host,
    get_sql_connection,
)
from ...ddl.create_sql_table import create_sql_table
from ...ddl.create_sql_table import build_ch_shard_table_name, quote_identifier
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    UnsupportedConnectionTypeError,
    sql_preview,
)
from ...labels import apply_query_label
from ...operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
)
from ...plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from analytics_toolkit.general import time_print
from .models import DropManyPartitionsOptions


@dataclass(frozen=True)
class _GpPartitionDefinition:
    name: str
    start: str | None = None
    end: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class _GpCreateManyPartitionsOptions:
    connection_key: str
    backend: str
    target_table: str
    intervals: Sequence[Mapping[str, Any]] | None = None
    values: Sequence[str] | None = None
    days: Sequence[str] | None = None
    weeks: Sequence[str] | None = None
    months: Sequence[str] | None = None
    years: Sequence[str] | None = None
    name_template: str = "p_{}"
    retry_cnt: int = 5
    timeout_increment: int | float = 5
    dry_run: bool = False
    return_sql: bool = False
    return_metadata: bool = False
    query_label: str | None = None


def table_exists(
    connection_type: str,
    connection: Any,
    table_name: str,
    connection_key: str | None = None,
) -> bool:
    backend = resolve_connection_backend(connection_type)
    return get_backend_adapter(backend).table_exists(
        connection,
        table_name,
        connection_key=connection_key or connection_type,
    )


def build_clear_table_sqls(
    connection_type: str,
    table_name: str,
    query_label: str | None = None,
) -> list[str]:
    return get_backend_adapter(connection_type).clear_table_sqls(
        table_name,
        query_label=query_label,
    )


def build_drop_table_sql(
    connection_type: str,
    table_name: str,
    ch_cluster: str | None = None,
    query_label: str | None = None,
) -> str:
    return get_backend_adapter(connection_type).drop_table_sql(
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )


def build_drop_ch_distributed_table_pair_sqls(
    table_name: str,
    ch_cluster: str = "{cluster}",
    query_label: str | None = None,
) -> list[str]:
    return _build_ch_pair_drop_sqls(
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )


def build_drop_many_partitions_sqls(
    connection_type: str,
    table: str,
    partition_keys_list: Sequence[str],
    partition_column: str | None = None,
    gp_truncate: bool = False,
    ch_cluster: str = "{cluster}",
    query_label: str | None = None,
) -> list[str]:
    backend = resolve_connection_backend(connection_type)
    target_table = _validate_non_empty_table_name(table)
    partition_keys = _validate_partition_keys(partition_keys_list)
    normalized_partition_column = _normalize_partition_column(partition_column)
    _validate_drop_many_partitions_options(
        backend,
        partition_column=normalized_partition_column,
        gp_truncate=gp_truncate,
    )
    return [
        apply_query_label(sql, query_label)
        for sql in _build_drop_many_partitions_sqls_for_backend(
            backend,
            target_table,
            partition_keys,
            partition_column=normalized_partition_column,
            gp_truncate=gp_truncate,
            ch_cluster=ch_cluster,
        )
    ]


@timed_public_sql_function
def build_gp_create_many_partitions_sqls(
    table: str,
    *,
    intervals: Sequence[Mapping[str, Any]] | None = None,
    values: Sequence[str] | None = None,
    days: Sequence[str] | None = None,
    weeks: Sequence[str] | None = None,
    months: Sequence[str] | None = None,
    years: Sequence[str] | None = None,
    name_template: str = "p_{}",
    query_label: str | None = None,
) -> list[str]:
    target_table = _validate_non_empty_table_name(table)
    partitions = _normalize_gp_create_partitions(
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
    )
    return [
        apply_query_label(
            _build_gp_create_partition_sql(target_table, partition),
            query_label,
        )
        for partition in partitions
    ]


def build_analyze_table_sql(
    connection_type: str,
    table_name: str,
    query_label: str | None = None,
) -> str:
    backend = resolve_connection_backend(connection_type)
    return get_backend_adapter(backend).analyze_table_sql(
        table_name,
        query_label=query_label,
    )


def clear_target_table(
    connection_type: str,
    connection: Any,
    table_name: str,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
) -> SqlPlan | None:
    time_print(f"Clearing target table {table_name} on {connection_type}")
    backend = resolve_connection_backend(connection_type)
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
) -> bool:
    backend = resolve_connection_backend(connection_type)
    log_connection = connection_label or connection_type
    if write_mode == "append":
        return target_exists

    if backend == "ch":
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
        time_print(f"Dropping existing table {table_name} on {log_connection}")
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
    ch_partition_by: list[str] | str | None = None,
    ch_order_by: list[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    query_label: str | None = None,
    connection_key: str | None = None,
    ch_retry_per_host_drops: bool = True,
) -> None:
    time_print(
        f"Finalizing staged transfer from {stage_table} into {target_table} on {connection_type}"
    )
    backend = resolve_connection_backend(connection_type)
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
            ch_partition_by=ch_partition_by,
            ch_order_by=ch_order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            query_label=query_label,
            ch_replace_table=(
                original_target_exists
                and replace_target_table
                and write_mode == "replace"
            ),
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
        create_sql_table(
            backend,
            connection,
            target_table,
            sample_batch,
            column_types=target_column_types,
            gp_distributed_by_key=gp_distributed_by_key,
            query_label=query_label,
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
    ch_partition_by: list[str] | str | None,
    ch_order_by: list[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    query_label: str | None,
    ch_replace_table: bool = False,
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
        ch_partition_by=ch_partition_by,
        ch_order_by=ch_order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=True,
        ch_replace_table=ch_replace_table,
        query_label=query_label,
    )


def analyze_table(
    connection_type: str,
    connection: Any,
    table_name: str,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
) -> SqlPlan | None:
    backend = resolve_connection_backend(connection_type)
    if backend == "ch":
        if dry_run or return_sql:
            return SqlPlan(
                operation="analyze_table",
                target_alias=connection_type,
                target_backend=backend,
                target_table=table_name,
                options={"skipped": True, "reason": "ClickHouse analyze is a no-op"},
                metadata=SqlOperationMetadata(
                    statement_count=0,
                    query_label=query_label,
                ),
            )
        return None

    time_print(f"Analyzing target table {table_name} on {connection_type}")
    if dry_run or return_sql:
        sql = build_analyze_table_sql(
            backend,
            table_name,
            query_label=query_label,
        )
        plan = SqlPlan(
            operation="analyze_table",
            target_alias=connection_type,
            target_backend=backend,
            target_table=table_name,
            metadata=SqlOperationMetadata(
                statement_count=1,
                query_label=query_label,
            ),
        )
        plan.add(
            sql,
            alias=connection_type,
            backend=backend,
            phase="analyze",
            target_table=table_name,
        )
        return plan
    get_backend_adapter(backend).analyze_table(
        connection,
        table_name,
        query_label=query_label,
    )
    return None


@timed_public_sql_function
def gp_create_many_partitions(
    db_key: str,
    table: str,
    *,
    intervals: Sequence[Mapping[str, Any]] | None = None,
    values: Sequence[str] | None = None,
    days: Sequence[str] | None = None,
    weeks: Sequence[str] | None = None,
    months: Sequence[str] | None = None,
    years: Sequence[str] | None = None,
    name_template: str = "p_{}",
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
) -> SqlPlan | SqlOperationResult | None:
    options = _build_gp_create_many_partitions_options(
        db_key=db_key,
        table=table,
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
    )
    plan = build_gp_create_many_partitions_plan(options)
    if options.dry_run or options.return_sql:
        return plan

    metadata = plan.metadata
    time_print(
        f"Creating {metadata.statement_count} partition(s) "
        f"on {options.target_table} via {options.connection_key}"
    )

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="gp_create_many_partitions",
            alias=options.connection_key,
            backend=options.backend,
            phase="create_partitions",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=plan.sqls[0] if plan.sqls else None,
        ):
            get_backend_adapter(options.backend).execute_commands(
                connection_ref["connection"],
                plan.sqls,
            )
            metadata.affected_rows = None

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="gp_create_many_partitions",
            alias=options.connection_key,
            backend=options.backend,
            phase="create_partitions",
            target_table=options.target_table,
            retry_attempt=attempt,
            sql_preview=sql_preview(plan.sqls[0] if plan.sqls else None),
        )

    run_connection_operation(
        operation_name=(
            f"creating partitions on {options.connection_key}.{options.target_table}"
        ),
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


@timed_public_sql_function
def drop_many_partitions(
    db_key: str,
    table: str,
    partition_keys_list: list[str],
    partition_column: str | None = None,
    gp_truncate: bool = False,
    *,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
) -> SqlPlan | SqlOperationResult | None:
    options = _build_drop_many_partitions_options(
        db_key=db_key,
        table=table,
        partition_keys_list=partition_keys_list,
        partition_column=partition_column,
        gp_truncate=gp_truncate,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
    )
    plan = build_drop_many_partitions_plan(options)
    if options.dry_run or options.return_sql:
        return plan

    metadata = plan.metadata
    time_print(
        f"Dropping {len(options.partition_keys)} partition(s) "
        f"from {options.target_table} on {options.connection_key}"
    )

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="drop_many_partitions",
            alias=options.connection_key,
            backend=options.backend,
            phase="drop_partitions",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=plan.sqls[0] if plan.sqls else None,
        ):
            get_backend_adapter(options.backend).execute_commands(
                connection_ref["connection"],
                plan.sqls,
            )
            metadata.affected_rows = None

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="drop_many_partitions",
            alias=options.connection_key,
            backend=options.backend,
            phase="drop_partitions",
            target_table=options.target_table,
            retry_attempt=attempt,
            sql_preview=sql_preview(plan.sqls[0] if plan.sqls else None),
        )

    run_connection_operation(
        operation_name=(
            f"dropping partitions from {options.connection_key}.{options.target_table}"
        ),
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


def build_gp_create_many_partitions_plan(
    options: _GpCreateManyPartitionsOptions,
) -> SqlPlan:
    sqls = build_gp_create_many_partitions_sqls(
        options.target_table,
        intervals=options.intervals,
        values=options.values,
        days=options.days,
        weeks=options.weeks,
        months=options.months,
        years=options.years,
        name_template=options.name_template,
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="gp_create_many_partitions",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.target_table,
        options={
            "partition_input": _selected_gp_create_partition_input(
                intervals=options.intervals,
                values=options.values,
                days=options.days,
                weeks=options.weeks,
                months=options.months,
                years=options.years,
            ),
            "name_template": options.name_template,
        },
        metadata=SqlOperationMetadata(
            statement_count=len(sqls),
            query_label=options.query_label,
        ),
    )
    plan.extend(
        sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="create_partitions",
        target_table=options.target_table,
    )
    return plan


def _build_gp_create_many_partitions_options(
    *,
    db_key: str,
    table: str,
    intervals: Sequence[Mapping[str, Any]] | None,
    values: Sequence[str] | None,
    days: Sequence[str] | None,
    weeks: Sequence[str] | None,
    months: Sequence[str] | None,
    years: Sequence[str] | None,
    name_template: str,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    dry_run: bool,
    return_sql: bool,
    return_metadata: bool,
) -> _GpCreateManyPartitionsOptions:
    config = get_connection_config(db_key)
    if config.backend != "gp":
        raise UnsupportedConnectionTypeError(
            "gp_create_many_partitions requires a gp connection, "
            f"got '{config.backend}'."
        )
    if retry_cnt < 1:
        raise ValueError("retry_cnt must be at least 1.")
    if timeout_increment < 0:
        raise ValueError("timeout_increment must be non-negative.")

    target_table = _validate_non_empty_table_name(table)
    _normalize_gp_create_partitions(
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
    )
    return _GpCreateManyPartitionsOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        target_table=target_table,
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
        name_template=name_template,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
    )


def build_drop_many_partitions_plan(
    options: DropManyPartitionsOptions,
) -> SqlPlan:
    sqls = build_drop_many_partitions_sqls(
        options.backend,
        options.target_table,
        options.partition_keys,
        partition_column=options.partition_column,
        gp_truncate=options.gp_truncate,
        ch_cluster=options.ch_cluster,
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="drop_many_partitions",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.target_table,
        options={
            "partition_keys": options.partition_keys,
            "partition_column": options.partition_column,
            "gp_truncate": options.gp_truncate,
            "ch_cluster": options.ch_cluster,
        },
        metadata=SqlOperationMetadata(
            statement_count=len(sqls),
            query_label=options.query_label,
        ),
    )
    plan.extend(
        sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="drop_partitions",
        target_table=options.target_table,
    )
    return plan


def _build_drop_many_partitions_options(
    *,
    db_key: str,
    table: str,
    partition_keys_list: Sequence[str],
    partition_column: str | None,
    gp_truncate: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    dry_run: bool,
    return_sql: bool,
    return_metadata: bool,
) -> DropManyPartitionsOptions:
    config = get_connection_config(db_key)
    if retry_cnt < 1:
        raise ValueError("retry_cnt must be at least 1.")
    if timeout_increment < 0:
        raise ValueError("timeout_increment must be non-negative.")

    target_table = _validate_non_empty_table_name(table)
    partition_keys = _validate_partition_keys(partition_keys_list)
    normalized_partition_column = _normalize_partition_column(partition_column)
    _validate_drop_many_partitions_options(
        config.backend,
        partition_column=normalized_partition_column,
        gp_truncate=gp_truncate,
    )
    return DropManyPartitionsOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        target_table=target_table,
        partition_keys=partition_keys,
        partition_column=normalized_partition_column,
        gp_truncate=gp_truncate,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
    )


@timed_public_sql_function
def gp_vacuum(
    table_name: str,
    analyze: bool = False,
    full: bool = False,
    verbose: bool = True,
    connection_key: str = "gp",
) -> None:
    config = get_connection_config(connection_key)
    if config.backend != "gp":
        raise UnsupportedConnectionTypeError(
            f"gp_vacuum requires a gp connection, got '{config.backend}'."
        )

    conn = get_sql_connection(config.connection_key)
    qualified_table_name = quote_qualified_table_name(table_name, "gp")
    options: list[str] = []
    if full:
        options.append("FULL")
    if verbose:
        options.append("VERBOSE")
    if analyze:
        options.append("ANALYZE")

    options_sql = f" ({', '.join(options)})" if options else ""
    sql = f"VACUUM{options_sql} {qualified_table_name}"

    time_print(f"Vacuuming table {qualified_table_name} on gp")
    try:
        previous_autocommit = conn.autocommit
        cursor = conn.cursor()
        try:
            conn.autocommit = True
            cursor.execute(sql)
        finally:
            cursor.close()
            conn.autocommit = previous_autocommit
    finally:
        time_print(f"Closing {config.connection_key} connection")
        conn.close()


def drop_table_with_retry(
    connection_backend: str,
    connection_key: str,
    connection_ref: dict[str, Any],
    table_name: str,
    retry_fn: Any,
    retry_cnt: int,
    timeout_increment: int | float,
    rollback_fn: Any,
    replace_connection_fn: Any,
    query_label: str | None = None,
) -> None:
    backend = resolve_connection_backend(connection_backend)

    def operation(attempt: int) -> None:
        connection = connection_ref["connection"]
        try:
            drop_table(backend, connection, table_name, query_label=query_label)
            return None
        except Exception:
            if backend == "gp":
                rollback_fn(connection)
            replace_connection_fn(connection_key, connection_ref)
            raise

    retry_fn(
        operation_name=f"dropping stage table {table_name} on {connection_key}",
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=operation,
    )


def drop_table(
    connection_type: str,
    connection: Any,
    table_name: str,
    ch_cluster: str | None = None,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    wait_for_absence: bool = False,
) -> SqlPlan | None:
    backend = resolve_connection_backend(connection_type)
    if dry_run or return_sql:
        sql = build_drop_table_sql(
            backend,
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
        )
        plan = SqlPlan(
            operation="drop_table",
            target_alias=connection_type,
            target_backend=backend,
            target_table=table_name,
            metadata=SqlOperationMetadata(
                statement_count=1,
                query_label=query_label,
            ),
        )
        plan.add(
            sql,
            alias=connection_type,
            backend=backend,
            phase="drop_target",
            target_table=table_name,
        )
        return plan
    get_backend_adapter(backend).drop_table(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )
    if backend == "ch" and wait_for_absence:
        from ...ddl.create_sql_table import _wait_for_ch_table_absence
        from ...ddl.create_sql_table import _wait_for_ch_table_absence_on_cluster

        if ch_cluster is None:
            _wait_for_ch_table_absence(connection, table_name)
        else:
            _wait_for_ch_table_absence_on_cluster(
                connection,
                table_name,
                ch_cluster=ch_cluster,
            )
    return None


def drop_ch_distributed_table_pair(
    connection: Any,
    table_name: str,
    ch_cluster: str = "{cluster}",
    query_label: str | None = None,
    wait_for_absence: bool = False,
    wait_timeout_seconds: int = 300,
    wait_poll_interval_seconds: float = 1,
    connection_key: str | None = None,
    ch_retry_per_host_drops: bool = True,
) -> None:
    per_host_connection_factory = (
        (lambda host: get_ch_connection_for_host(connection_key, host))
        if connection_key is not None
        else None
    )
    _drop_ch_pair(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
        wait_for_absence=wait_for_absence,
        wait_timeout_seconds=wait_timeout_seconds,
        wait_poll_interval_seconds=wait_poll_interval_seconds,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
        per_host_connection_factory=per_host_connection_factory,
    )


def clear_ch_distributed_table_data(
    connection: Any,
    table_name: str,
    ch_cluster: str = "{cluster}",
    query_label: str | None = None,
) -> None:
    _truncate_ch_pair(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )


def get_trino_table_column_types(
    connection: Any,
    table_name: str,
    connection_key: str = "trino",
) -> dict[str, str]:
    return get_backend_adapter("trino").get_table_column_types(
        connection,
        table_name,
        connection_key=connection_key,
    )


def get_table_column_types(
    connection_type: str,
    connection: Any,
    table_name: str,
    connection_key: str | None = None,
) -> dict[str, str]:
    backend = resolve_connection_backend(connection_type)
    return get_backend_adapter(backend).get_table_column_types(
        connection,
        table_name,
        connection_key=connection_key or connection_type,
    )


def _get_gp_table_column_types(connection: Any, table_name: str) -> dict[str, str]:
    return get_backend_adapter("gp").get_table_column_types(
        connection,
        table_name,
        connection_key="gp",
    )


def _split_gp_table_name(table_name: str) -> tuple[str, str]:
    return split_gp_table_name(table_name)


def _format_gp_information_schema_type(
    data_type: str,
    udt_name: Any,
    numeric_precision: Any,
    numeric_scale: Any,
) -> str:
    return format_gp_information_schema_type(
        data_type,
        udt_name,
        numeric_precision,
        numeric_scale,
    )


def _get_ch_table_column_types(connection: Any, table_name: str) -> dict[str, str]:
    return get_backend_adapter("ch").get_table_column_types(
        connection,
        table_name,
        connection_key="ch",
    )


def insert_from_table(
    connection_type: str,
    connection: Any,
    target_table: str,
    source_table: str,
    column_types: Mapping[str, str] | None = None,
    query_label: str | None = None,
) -> None:
    backend = resolve_connection_backend(connection_type)
    get_backend_adapter(backend).insert_from_table(
        connection,
        target_table,
        source_table,
        column_types=column_types,
        query_label=query_label,
    )


def insert_from_query(
    connection_type: str,
    connection: Any,
    target_table: str,
    source_sql: str,
    column_types: Mapping[str, str],
    query_label: str | None = None,
) -> int:
    backend = resolve_connection_backend(connection_type)
    return get_backend_adapter(backend).insert_from_query(
        connection,
        target_table,
        source_sql,
        column_types,
        query_label=query_label,
    )


def build_insert_from_query_sql(
    connection_type: str,
    target_table: str,
    source_sql: str,
    column_types: Mapping[str, str],
    query_label: str | None = None,
) -> str:
    backend = resolve_connection_backend(connection_type)
    return apply_query_label(
        get_backend_adapter(backend).build_insert_from_query_sql(
            target_table,
            source_sql,
            column_types,
        ),
        query_label,
    )


def build_insert_from_table_sql(
    connection_type: str,
    target_table: str,
    source_table: str,
    column_types: Mapping[str, str] | None = None,
    query_label: str | None = None,
) -> str:
    backend = resolve_connection_backend(connection_type)
    return apply_query_label(
        get_backend_adapter(backend).build_insert_from_table_sql(
            target_table,
            source_table,
            column_types,
        ),
        query_label,
    )


def count_table_rows(
    connection_type: str,
    connection: Any,
    table_name: str,
    query_label: str | None = None,
) -> int:
    backend = resolve_connection_backend(connection_type)
    return get_backend_adapter(backend).count_table_rows(
        connection,
        table_name,
        query_label=query_label,
    )


def build_count_table_rows_sql(
    connection_type: str,
    table_name: str,
    query_label: str | None = None,
) -> str:
    return get_backend_adapter(connection_type).count_table_rows_sql(
        table_name,
        query_label=query_label,
    )


def _build_insert_from_table_sql(
    connection_type: str,
    target_table: str,
    source_table: str,
    column_types: Mapping[str, str] | None,
) -> str:
    return get_backend_adapter(connection_type).build_insert_from_table_sql(
        target_table,
        source_table,
        column_types,
    )


def _build_typed_insert_select_sql(
    connection_type: str,
    target_table: str,
    from_sql: str,
    column_types: Mapping[str, str],
) -> str:
    return get_backend_adapter(connection_type)._build_typed_insert_select_sql(
        target_table,
        from_sql,
        column_types,
    )


def _cast_select_expression(
    connection_type: str,
    column_name: str,
    target_type: str,
) -> str:
    return get_backend_adapter(connection_type).cast_select_expression(
        column_name,
        target_type,
    )


def _extract_row_count(executed: Any) -> int:
    return extract_row_count(executed)


def _extract_row_count_from_mapping(value: Mapping[str, Any]) -> int | None:
    for key in (
        "rowcount",
        "row_count",
        "written_rows",
        "writtenRows",
        "processedRows",
        "rows",
    ):
        row_count = _coerce_row_count(value.get(key))
        if row_count is not None:
            return row_count
    return None


def _coerce_row_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        row_count = int(value)
    except (TypeError, ValueError):
        return None
    if row_count < 0:
        return None
    return row_count


def _build_drop_many_partitions_sqls_for_backend(
    backend: str,
    table: str,
    partition_keys: list[str],
    *,
    partition_column: str | None,
    gp_truncate: bool,
    ch_cluster: str,
) -> list[str]:
    if backend == "gp":
        action = "TRUNCATE" if gp_truncate else "DROP"
        return [
            f"ALTER TABLE {table} {action} PARTITION FOR ({_sql_string_literal(key)})"
            for key in partition_keys
        ]
    if backend == "trino":
        if partition_column is None:
            raise InvalidSqlInputError(
                "partition_column is required for Trino partition deletes."
            )
        partition_values = ", ".join(
            f"DATE {_sql_string_literal(key)}" for key in partition_keys
        )
        return [
            f"DELETE FROM {table}\nWHERE {partition_column} IN ({partition_values})"
        ]
    if backend == "ch":
        shard_table = build_ch_shard_table_name(table)
        cluster_clause = ch_cluster_clause(ch_cluster)
        return [
            f"ALTER TABLE {shard_table}{cluster_clause} "
            f"DROP PARTITION {_sql_string_literal(key)}"
            for key in partition_keys
        ]
    raise UnsupportedConnectionTypeError(
        "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
    )


def _normalize_gp_create_partitions(
    *,
    intervals: Sequence[Mapping[str, Any]] | None,
    values: Sequence[str] | None,
    days: Sequence[str] | None,
    weeks: Sequence[str] | None,
    months: Sequence[str] | None,
    years: Sequence[str] | None,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    _validate_gp_partition_name_template(name_template)
    selected = _selected_gp_create_partition_input(
        intervals=intervals,
        values=values,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
    )
    if selected == "intervals":
        return _normalize_gp_interval_partitions(intervals, name_template)
    if selected == "values":
        return _normalize_gp_value_partitions(values, name_template)
    if selected == "days":
        return _normalize_gp_period_partitions(days, "days", name_template)
    if selected == "weeks":
        return _normalize_gp_period_partitions(weeks, "weeks", name_template)
    if selected == "months":
        return _normalize_gp_period_partitions(months, "months", name_template)
    if selected == "years":
        return _normalize_gp_period_partitions(years, "years", name_template)
    raise AssertionError(f"Unexpected Greenplum partition input: {selected}")


def _selected_gp_create_partition_input(
    *,
    intervals: Sequence[Mapping[str, Any]] | None,
    values: Sequence[str] | None,
    days: Sequence[str] | None,
    weeks: Sequence[str] | None,
    months: Sequence[str] | None,
    years: Sequence[str] | None,
) -> str:
    provided = {
        name: value
        for name, value in {
            "intervals": intervals,
            "values": values,
            "days": days,
            "weeks": weeks,
            "months": months,
            "years": years,
        }.items()
        if value is not None
    }
    if len(provided) != 1:
        raise InvalidSqlInputError(
            "Exactly one of intervals, values, days, weeks, months, or years "
            "must be provided."
        )
    return next(iter(provided))


def _normalize_gp_interval_partitions(
    intervals: Sequence[Mapping[str, Any]] | None,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    interval_items = _validate_gp_partition_sequence(intervals, "intervals")
    partitions: list[_GpPartitionDefinition] = []
    for index, item in enumerate(interval_items):
        if not isinstance(item, Mapping):
            raise InvalidSqlInputError(
                "intervals must contain mappings with start and end values."
            )
        start = _parse_gp_partition_date(item.get("start"), "interval start")
        end = _parse_gp_partition_date(item.get("end"), "interval end")
        if end <= start:
            raise InvalidSqlInputError("Interval end must be after interval start.")

        raw_name = item.get("name")
        if raw_name is None:
            name = _render_gp_partition_name(name_template, start.isoformat())
        else:
            name = _validate_gp_partition_identifier(
                raw_name,
                f"intervals[{index}].name",
            )
        partitions.append(
            _GpPartitionDefinition(
                name=name,
                start=start.isoformat(),
                end=end.isoformat(),
            )
        )
    return partitions


def _normalize_gp_value_partitions(
    values: Sequence[str] | None,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    value_items = _validate_gp_partition_sequence(values, "values")
    partitions: list[_GpPartitionDefinition] = []
    for value in value_items:
        if not isinstance(value, str):
            raise InvalidSqlInputError("values must contain strings.")
        normalized = value.strip()
        if not normalized:
            raise InvalidSqlInputError("values must not contain empty strings.")
        partitions.append(
            _GpPartitionDefinition(
                name=_render_gp_partition_name(name_template, normalized),
                value=normalized,
            )
        )
    return partitions


def _normalize_gp_period_partitions(
    raw_values: Sequence[str] | None,
    input_name: str,
    name_template: str,
) -> list[_GpPartitionDefinition]:
    date_values = _validate_gp_partition_sequence(raw_values, input_name)
    partitions: list[_GpPartitionDefinition] = []
    for raw_value in date_values:
        start = _parse_gp_partition_date(raw_value, input_name)
        end = _next_gp_partition_period_start(start, input_name)
        partitions.append(
            _GpPartitionDefinition(
                name=_render_gp_partition_name(name_template, start.isoformat()),
                start=start.isoformat(),
                end=end.isoformat(),
            )
        )
    return partitions


def _validate_gp_partition_sequence(
    value: Sequence[Any] | None,
    argument_name: str,
) -> list[Any]:
    if value is None:
        raise InvalidSqlInputError(f"{argument_name} must be provided.")
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise InvalidSqlInputError(
            f"{argument_name} must be a non-empty sequence."
        )
    try:
        items = list(value)
    except TypeError as exc:
        raise InvalidSqlInputError(
            f"{argument_name} must be a non-empty sequence."
        ) from exc
    if not items:
        raise InvalidSqlInputError(
            f"{argument_name} must be a non-empty sequence."
        )
    return items


def _parse_gp_partition_date(value: Any, argument_name: str) -> date:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise InvalidSqlInputError(
                f"{argument_name} must contain ISO date strings."
            )
        try:
            parsed = date.fromisoformat(normalized)
        except ValueError as exc:
            raise InvalidSqlInputError(
                f"{argument_name} must contain valid ISO date strings."
            ) from exc
    else:
        raise InvalidSqlInputError(
            f"{argument_name} must contain ISO date strings."
        )
    return parsed


def _next_gp_partition_period_start(start: date, input_name: str) -> date:
    if input_name == "days":
        return start + timedelta(days=1)
    if input_name == "weeks":
        if start.weekday() != 0:
            raise InvalidSqlInputError("weeks values must be Monday week starts.")
        return start + timedelta(days=7)
    if input_name == "months":
        if start.day != 1:
            raise InvalidSqlInputError("months values must be month starts.")
        year = start.year + (1 if start.month == 12 else 0)
        month = 1 if start.month == 12 else start.month + 1
        return date(year, month, 1)
    if input_name == "years":
        if start.month != 1 or start.day != 1:
            raise InvalidSqlInputError("years values must be year starts.")
        return date(start.year + 1, 1, 1)
    raise AssertionError(f"Unexpected Greenplum period input: {input_name}")


def _validate_gp_partition_name_template(name_template: str) -> None:
    if not isinstance(name_template, str):
        raise InvalidSqlInputError("name_template must be a string.")
    remainder = name_template.replace("{}", "", 1)
    if name_template.count("{}") != 1 or "{" in remainder or "}" in remainder:
        raise InvalidSqlInputError(
            "name_template must contain exactly one {} placeholder."
        )


def _render_gp_partition_name(name_template: str, value: str) -> str:
    sanitized = _sanitize_gp_partition_name_token(value)
    return _validate_gp_partition_identifier(
        name_template.format(sanitized),
        "partition name",
    )


def _sanitize_gp_partition_name_token(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z_]+", "_", str(value).strip())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "partition"


def _validate_gp_partition_identifier(value: Any, argument_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidSqlInputError(f"{argument_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError(f"{argument_name} must not be empty.")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized) is None:
        raise InvalidSqlInputError(
            f"{argument_name} must be an unquoted SQL identifier."
        )
    return normalized


def _build_gp_create_partition_sql(
    table: str,
    partition: _GpPartitionDefinition,
) -> str:
    if partition.value is not None:
        return (
            f"ALTER TABLE {table} ADD PARTITION {partition.name} "
            f"VALUES ({_sql_string_literal(partition.value)})"
        )
    if partition.start is None or partition.end is None:
        raise InvalidSqlInputError(
            "Range partitions require both start and end values."
        )
    return (
        f"ALTER TABLE {table} ADD PARTITION {partition.name} "
        f"START ({_sql_string_literal(partition.start)}) INCLUSIVE "
        f"END ({_sql_string_literal(partition.end)}) EXCLUSIVE"
    )


def _validate_non_empty_table_name(table: str) -> str:
    normalized = str(table).strip()
    if not normalized:
        raise InvalidSqlInputError("Table name must not be empty.")
    return normalized


def _validate_partition_keys(partition_keys_list: Sequence[str]) -> list[str]:
    if isinstance(partition_keys_list, (str, bytes)) or not partition_keys_list:
        raise InvalidSqlInputError(
            "partition_keys_list must be a non-empty sequence of strings."
        )

    partition_keys: list[str] = []
    for partition_key in partition_keys_list:
        if not isinstance(partition_key, str):
            raise InvalidSqlInputError("Partition values must be strings.")
        normalized = partition_key.strip()
        if not normalized:
            raise InvalidSqlInputError("Partition values must not be empty.")
        partition_keys.append(normalized)
    return partition_keys


def _normalize_partition_column(partition_column: str | None) -> str | None:
    if partition_column is None:
        return None
    normalized = str(partition_column).strip()
    return normalized or None


def _validate_drop_many_partitions_options(
    backend: str,
    *,
    partition_column: str | None,
    gp_truncate: bool,
) -> None:
    if gp_truncate and backend != "gp":
        raise UnsupportedConnectionTypeError(
            "gp_truncate=True is only supported for Greenplum connections."
        )
    if backend == "trino" and partition_column is None:
        raise InvalidSqlInputError(
            "partition_column is required for Trino partition deletes."
        )


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def split_trino_table_name(
    table_name: str,
    connection_key: str = "trino",
) -> tuple[str, str, str]:
    return _adapter_split_trino_table_name(table_name, connection_key=connection_key)


def quote_qualified_table_name(table_name: str, connection_type: str) -> str:
    parts = [part.strip() for part in table_name.split(".")]
    if not parts or any(not part for part in parts):
        raise InvalidSqlInputError("Table name must be a non-empty identifier.")
    if len(parts) > 3:
        raise InvalidSqlInputError(
            "Table name must be unqualified or dot-qualified up to three parts."
        )
    return ".".join(quote_identifier(part, connection_type) for part in parts)


def _truncate_ch_table(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = None,
    query_label: str | None = None,
) -> None:
    _execute_ch_command(
        connection,
        apply_query_label(
            f"TRUNCATE TABLE IF EXISTS {table_name}{_ch_cluster_clause(ch_cluster)}",
            query_label,
        ),
    )


def _ch_cluster_clause(ch_cluster: str | None) -> str:
    return ch_cluster_clause(ch_cluster)


def _format_ch_cluster_name(cluster_name: str) -> str:
    return format_ch_cluster_name(cluster_name)


def _is_simple_identifier(identifier: str) -> bool:
    return is_simple_identifier(identifier)


def _execute_ch_command(connection: Any, sql: str) -> None:
    get_backend_adapter("ch").execute_command(connection, sql)


def _gp_table_exists(connection: Any, table_name: str) -> bool:
    return get_backend_adapter("gp").table_exists(
        connection,
        table_name,
        connection_key="gp",
    )


def _trino_table_exists(
    connection: Any,
    table_name: str,
    connection_key: str,
) -> bool:
    return get_backend_adapter("trino").table_exists(
        connection,
        table_name,
        connection_key=connection_key,
    )


def _ch_table_exists(client: Any, table_name: str) -> bool:
    return get_backend_adapter("ch").table_exists(
        client,
        table_name,
        connection_key="ch",
    )
