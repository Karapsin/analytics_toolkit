from __future__ import annotations

from collections.abc import Sequence

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.execution.backend_options import preflight_sql_statements
from analytics_toolkit.sql.execution.operation_runner import (
    timed_public_sql_function,
    tracked_sql_operation,
)
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from analytics_toolkit.sql.execution.retry_cleanup import connection_scope
from analytics_toolkit.sql.execution.validation import (
    validate_positive_int,
    validate_positive_number,
)

from .models import ChDropTableOptions


@timed_public_sql_function
def drop_tables(
    db_key: str,
    table: str | list[str],
    *,
    if_exists: bool = False,
    ch_cluster: str | None = "{cluster}",
    ch_drop_shard: bool = True,
    ch_drop_distributed: bool = True,
    ch_wait_for_absence: bool = False,
    ch_wait_timeout_seconds: int = 300,
    ch_wait_poll_interval_seconds: float = 1,
    ch_retry_per_host_drops: bool = True,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    query_label: str | None = None,
) -> SqlPlan | SqlOperationResult | None:
    validate_positive_int(ch_wait_timeout_seconds, "ch_wait_timeout_seconds")
    validate_positive_number(
        ch_wait_poll_interval_seconds,
        "ch_wait_poll_interval_seconds",
    )
    config = get_connection_config(db_key)
    target_tables = _normalize_target_tables(table)
    options = ChDropTableOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        target_table=target_tables[0] if len(target_tables) == 1 else None,
        ch_shard_table=None,
        ch_only_shard=False,
        ch_cluster=(
            None
            if ch_cluster is None
            else _normalize_non_empty_string(str(ch_cluster), "ch_cluster")
        ),
        if_exists=bool(if_exists),
        ch_wait_for_absence=bool(ch_wait_for_absence),
        ch_wait_timeout_seconds=ch_wait_timeout_seconds,
        ch_wait_poll_interval_seconds=ch_wait_poll_interval_seconds,
        ch_retry_per_host_drops=bool(ch_retry_per_host_drops),
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
    )
    plan = build_drop_table_plan(
        options,
        target_tables=target_tables,
        if_exists=bool(if_exists),
        ch_drop_shard=bool(ch_drop_shard),
        ch_drop_distributed=bool(ch_drop_distributed),
    )
    if options.dry_run or options.return_sql:
        return plan

    metadata = plan.metadata
    connection = get_sql_connection(config.connection_key)
    with connection_scope(connection):
        try:
            with tracked_sql_operation(
                metadata=metadata,
                operation_name="drop_tables",
                alias=options.connection_key,
                backend=options.backend,
                phase="drop_tables",
                query_label=options.query_label,
                preview_sql=plan.sqls[0] if plan.sqls else None,
            ):
                preflight_sql_statements(connection, plan.sqls)
                adapter = get_backend_adapter(options.backend)
                drop_options = {
                    "connection_key": options.connection_key,
                    "ch_cluster": options.ch_cluster,
                    "ch_drop_shard": bool(ch_drop_shard),
                    "ch_drop_distributed": bool(ch_drop_distributed),
                    "ch_wait_for_absence": options.ch_wait_for_absence,
                    "ch_wait_timeout_seconds": options.ch_wait_timeout_seconds,
                    "ch_wait_poll_interval_seconds": options.ch_wait_poll_interval_seconds,
                    "ch_retry_per_host_drops": options.ch_retry_per_host_drops,
                    "if_exists": bool(if_exists),
                    "query_label": options.query_label,
                }
                batch_drop = getattr(adapter, "drop_tables_with_options", None)
                if callable(batch_drop):
                    batch_drop(connection, target_tables, **drop_options)
                else:
                    for target_table in target_tables:
                        adapter.drop_table_with_options(connection, target_table, **drop_options)
                metadata.affected_rows = None
        finally:
            time_print(
                "Closing connection",
                connection=config.connection_key,
                backend=config.backend,
                phase="close",
            )

    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


def build_drop_table_plan(
    options: ChDropTableOptions,
    *,
    target_tables: Sequence[str],
    if_exists: bool,
    ch_drop_shard: bool,
    ch_drop_distributed: bool,
) -> SqlPlan:
    sql_plans = [
        _build_drop_table_plan_for_one(
            options,
            table_name,
            if_exists=if_exists,
            ch_drop_shard=ch_drop_shard,
            ch_drop_distributed=ch_drop_distributed,
        )
        for table_name in target_tables
    ]
    statement_count = sum(len(plan.statements) for plan in sql_plans)
    pending_options = {
        key: value for subplan in sql_plans for key, value in subplan.options.items()
    }
    plan = SqlPlan(
        operation="drop_tables",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.target_table,
        options={
            **pending_options,
            "tables": list(target_tables),
            "if_exists": if_exists,
            "ch_cluster": options.ch_cluster,
            "ch_drop_shard": ch_drop_shard,
            "ch_drop_distributed": ch_drop_distributed,
            "ch_wait_for_absence": options.ch_wait_for_absence,
            "ch_wait_timeout_seconds": options.ch_wait_timeout_seconds,
            "ch_wait_poll_interval_seconds": (options.ch_wait_poll_interval_seconds),
            "ch_retry_per_host_drops": options.ch_retry_per_host_drops,
        },
        metadata=SqlOperationMetadata(
            statement_count=None if pending_options else statement_count,
            query_label=options.query_label,
        ),
    )
    for subplan in sql_plans:
        plan.statements.extend(subplan.statements)
    return plan


def _build_drop_table_plan_for_one(
    options: ChDropTableOptions,
    table_name: str,
    *,
    if_exists: bool,
    ch_drop_shard: bool,
    ch_drop_distributed: bool,
) -> SqlPlan:
    sqls = get_backend_adapter(options.backend).build_drop_tables_sqls(
        table_name,
        ch_cluster=options.ch_cluster,
        ch_drop_shard=ch_drop_shard,
        ch_drop_distributed=ch_drop_distributed,
        if_exists=if_exists,
        query_label=options.query_label,
    )
    pending_hook = getattr(get_backend_adapter(options.backend), "pending_drop_plan_options", None)
    pending_options = pending_hook() if callable(pending_hook) else {}
    if pending_options:
        sqls = []
    plan = SqlPlan(
        operation="drop_tables",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=table_name,
        options=pending_options,
        metadata=SqlOperationMetadata(
            statement_count=None if pending_options else len(sqls),
            query_label=options.query_label,
        ),
    )
    plan.extend(
        sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="drop_tables",
        target_table=table_name,
        query_label=options.query_label,
    )
    return plan


def _normalize_target_tables(table: str | list[str]) -> list[str]:
    if isinstance(table, str):
        return [_normalize_non_empty_string(table, "table")]
    if not isinstance(table, list):
        raise TypeError("table must be a string or a list of strings.")
    normalized_tables = [_normalize_non_empty_string(table_name, "table") for table_name in table]
    if not normalized_tables:
        raise ValueError("table must not be empty.")
    return normalized_tables


def _normalize_non_empty_string(value: str, option_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{option_name} must not be empty.")
    return normalized
