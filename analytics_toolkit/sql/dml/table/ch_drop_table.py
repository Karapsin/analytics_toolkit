from __future__ import annotations

from sqlglot import exp, parse_one

from ...clickhouse.lifecycle import (
    build_drop_ch_distributed_table_pair_sqls,
    build_drop_ch_table_sqls,
    drop_ch_distributed_table_pair,
    drop_ch_table,
)
from ...clickhouse.options import resolve_ch_retry_per_host_drops_concurrency
from ...connection.config import get_connection_config
from ...connection.errors import UnsupportedConnectionTypeError
from ...connection.get_sql_connection import (
    get_ch_connection_for_host,
    get_sql_connection,
)
from ...ddl.clickhouse import build_ch_shard_table_name, _normalize_non_empty_string
from ...execution.operation_runner import timed_public_sql_function, tracked_sql_operation
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from analytics_toolkit.general import time_print
from .models import ChDropTableOptions


@timed_public_sql_function
def ch_drop_table(
    db_key: str,
    table: str,
    *,
    ch_cluster: str | None = "{cluster}",
    ch_shard_table: str | None = None,
    ch_wait_for_absence: bool = False,
    ch_wait_timeout_seconds: int = 300,
    ch_wait_poll_interval_seconds: float = 1,
    ch_retry_per_host_drops: bool = True,
    ch_retry_per_host_drops_concurrency: int | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    query_label: str | None = None,
) -> SqlPlan | SqlOperationResult | None:
    config = get_connection_config(db_key)
    if config.backend != "ch":
        raise UnsupportedConnectionTypeError(
            f"ch_drop_table requires a ch connection, got '{config.backend}'."
    )

    target_table = _normalize_non_empty_string(table, "table")
    ch_only_shard = (
        ch_shard_table is None and _is_default_ch_shard_table_name(target_table)
    )
    target_shard_table = (
        target_table
        if ch_only_shard
        else build_ch_shard_table_name(target_table)
        if ch_shard_table is None
        else _normalize_non_empty_string(ch_shard_table, "ch_shard_table")
    )
    cluster_name = (
        None
        if ch_only_shard or ch_cluster is None
        else _normalize_non_empty_string(ch_cluster, "ch_cluster")
    )
    options = ChDropTableOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        target_table=target_table,
        ch_shard_table=target_shard_table,
        ch_only_shard=ch_only_shard,
        ch_cluster=cluster_name,
        ch_wait_for_absence=bool(ch_wait_for_absence),
        ch_wait_timeout_seconds=ch_wait_timeout_seconds,
        ch_wait_poll_interval_seconds=ch_wait_poll_interval_seconds,
        ch_retry_per_host_drops=bool(ch_retry_per_host_drops),
        ch_retry_per_host_drops_concurrency=(
            resolve_ch_retry_per_host_drops_concurrency(
                ch_retry_per_host_drops=bool(ch_retry_per_host_drops),
                ch_retry_per_host_drops_concurrency=(
                    ch_retry_per_host_drops_concurrency
                ),
            )
        ),
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
    )
    plan = build_ch_drop_table_plan(options)
    if options.dry_run or options.return_sql:
        return plan

    metadata = plan.metadata
    connection = get_sql_connection(config.connection_key)
    try:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="ch_drop_table",
            alias=options.connection_key,
            backend=options.backend,
            phase="drop_table",
            query_label=options.query_label,
            preview_sql=plan.sqls[0] if plan.sqls else None,
        ):
            time_print(
                f"Dropping ClickHouse table {options.target_table}"
            )
            if options.ch_only_shard:
                drop_ch_table(
                    connection,
                    options.target_table,
                    ch_cluster=options.ch_cluster,
                    query_label=options.query_label,
                    wait_for_absence=options.ch_wait_for_absence,
                    wait_timeout_seconds=options.ch_wait_timeout_seconds,
                    wait_poll_interval_seconds=options.ch_wait_poll_interval_seconds,
                )
                metadata.affected_rows = None
                return (
                    SqlOperationResult(rows=None, metadata=metadata, plan=plan)
                    if options.return_metadata
                    else None
                )
            time_print(
                f"Dropping paired ClickHouse shard table {options.ch_shard_table}"
            )
            drop_ch_distributed_table_pair(
                connection,
                options.target_table,
                ch_cluster=options.ch_cluster,
                shard_table=options.ch_shard_table,
                query_label=options.query_label,
                wait_for_absence=options.ch_wait_for_absence,
                wait_timeout_seconds=options.ch_wait_timeout_seconds,
                wait_poll_interval_seconds=options.ch_wait_poll_interval_seconds,
                ch_retry_per_host_drops=options.ch_retry_per_host_drops,
                ch_retry_per_host_drops_concurrency=(
                    options.ch_retry_per_host_drops_concurrency
                ),
                per_host_connection_factory=(
                    lambda host: get_ch_connection_for_host(
                        options.connection_key,
                        host,
                    )
                ),
            )
            metadata.affected_rows = None
    finally:
        time_print(
            "Closing connection",
            connection=config.connection_key,
            backend=config.backend,
            phase="close",
        )
        connection.close()

    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


def build_ch_drop_table_plan(options: ChDropTableOptions) -> SqlPlan:
    sqls = (
        build_drop_ch_table_sqls(
            options.target_table,
            ch_cluster=options.ch_cluster,
        )
        if options.ch_only_shard
        else build_drop_ch_distributed_table_pair_sqls(
            options.target_table,
            ch_cluster=options.ch_cluster,
            shard_table=options.ch_shard_table,
        )
    )
    plan = SqlPlan(
        operation="ch_drop_table",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.target_table,
        options={
            "ch_shard_table": options.ch_shard_table,
            "ch_only_shard": options.ch_only_shard,
            "ch_cluster": options.ch_cluster,
            "ch_wait_for_absence": options.ch_wait_for_absence,
            "ch_wait_timeout_seconds": options.ch_wait_timeout_seconds,
            "ch_wait_poll_interval_seconds": options.ch_wait_poll_interval_seconds,
            "ch_retry_per_host_drops": options.ch_retry_per_host_drops,
            "ch_retry_per_host_drops_concurrency": (
                options.ch_retry_per_host_drops_concurrency
            ),
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
        phase="drop_table",
        target_table=options.target_table,
        query_label=options.query_label,
    )
    return plan


def _is_default_ch_shard_table_name(table_name: str) -> bool:
    try:
        table = parse_one(table_name, read="clickhouse", into=exp.Table)
    except Exception:
        return False
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        return False
    return str(table.this.this).endswith("_shard")
