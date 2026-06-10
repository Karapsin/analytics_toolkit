from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp, parse_one

from ...clickhouse.lifecycle import (
    build_drop_ch_distributed_table_pair_sqls,
    build_drop_ch_table_sqls,
    drop_ch_distributed_table_pair,
    drop_ch_table,
)
from ...connection.config import get_connection_config
from ...connection.get_sql_connection import (
    get_ch_connection_for_host,
    get_sql_connection,
)
from ...ddl.clickhouse import build_ch_shard_table_name, _normalize_non_empty_string
from ...execution.operation_runner import timed_public_sql_function, tracked_sql_operation
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from analytics_toolkit.general import time_print
from ._basic_ops import build_drop_table_sql
from .maintenance import drop_table as _drop_table_single
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
            else _normalize_non_empty_string(ch_cluster, "ch_cluster")
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
            for target_table in target_tables:
                _drop_single_table(
                    connection,
                    target_table,
                    backend=options.backend,
                    ch_cluster=options.ch_cluster,
                    query_label=options.query_label,
                    ch_wait_for_absence=options.ch_wait_for_absence,
                    ch_wait_timeout_seconds=options.ch_wait_timeout_seconds,
                    ch_wait_poll_interval_seconds=(
                        options.ch_wait_poll_interval_seconds
                    ),
                    ch_retry_per_host_drops=options.ch_retry_per_host_drops,
                    ch_drop_shard=bool(ch_drop_shard),
                    ch_drop_distributed=bool(ch_drop_distributed),
                    if_exists=bool(if_exists),
                    connection_key=options.connection_key,
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
    plan = SqlPlan(
        operation="drop_tables",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.target_table,
        options={
            "tables": list(target_tables),
            "if_exists": if_exists,
            "ch_cluster": options.ch_cluster,
            "ch_drop_shard": ch_drop_shard,
            "ch_drop_distributed": ch_drop_distributed,
            "ch_wait_for_absence": options.ch_wait_for_absence,
            "ch_wait_timeout_seconds": options.ch_wait_timeout_seconds,
            "ch_wait_poll_interval_seconds": (
                options.ch_wait_poll_interval_seconds
            ),
            "ch_retry_per_host_drops": options.ch_retry_per_host_drops,
        },
        metadata=SqlOperationMetadata(
            statement_count=statement_count,
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
    if options.backend != "ch":
        sql = build_drop_table_sql(
            options.backend,
            table_name,
            if_exists=if_exists,
            query_label=options.query_label,
        )
        plan = SqlPlan(
            operation="drop_tables",
            target_alias=options.connection_key,
            target_backend=options.backend,
            target_table=table_name,
            metadata=SqlOperationMetadata(
                statement_count=1,
                query_label=options.query_label,
            ),
        )
        plan.extend(
            [sql],
            alias=options.connection_key,
            backend=options.backend,
            phase="drop_tables",
            target_table=table_name,
            query_label=options.query_label,
        )
        return plan

    if _is_default_ch_shard_table_name(table_name):
        if not ch_drop_shard:
            raise ValueError(
                "ch_drop_shard must be True when dropping a ClickHouse shard table."
            )
        sqls = build_drop_ch_table_sqls(
            table_name,
            ch_cluster=None,
            query_label=options.query_label,
            if_exists=if_exists,
        )
    elif ch_drop_distributed and ch_drop_shard:
        sqls = build_drop_ch_distributed_table_pair_sqls(
            table_name,
            ch_cluster=options.ch_cluster,
            query_label=options.query_label,
            if_exists=if_exists,
        )
    elif ch_drop_distributed:
        sqls = build_drop_ch_table_sqls(
            table_name,
            ch_cluster=options.ch_cluster,
            query_label=options.query_label,
            if_exists=if_exists,
        )
    elif ch_drop_shard:
        shard_table = build_ch_shard_table_name(table_name)
        sqls = build_drop_ch_table_sqls(
            shard_table,
            ch_cluster=options.ch_cluster,
            query_label=options.query_label,
            if_exists=if_exists,
        )
    else:
        raise ValueError(
            "At least one of ch_drop_shard or ch_drop_distributed must be True."
        )

    plan = SqlPlan(
        operation="drop_tables",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=table_name,
        metadata=SqlOperationMetadata(
            statement_count=len(sqls),
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


def _drop_single_table(
    connection: Any,
    table_name: str,
    *,
    backend: str,
    ch_cluster: str | None,
    query_label: str | None,
    ch_wait_for_absence: bool,
    ch_wait_timeout_seconds: int,
    ch_wait_poll_interval_seconds: float,
    ch_retry_per_host_drops: bool,
    ch_drop_shard: bool,
    ch_drop_distributed: bool,
    if_exists: bool,
    connection_key: str,
) -> None:
    if backend != "ch":
        time_print(
            f"Dropping table {table_name}",
            connection=connection_key,
            backend=backend,
        )
        _drop_table_single(
            backend,
            connection,
            table_name,
            query_label=query_label,
            if_exists=if_exists,
        )
        return

    if _is_default_ch_shard_table_name(table_name):
        if not ch_drop_shard:
            raise ValueError(
                "ch_drop_shard must be True when dropping a ClickHouse shard table."
            )
        time_print(
            f"Dropping ClickHouse table {table_name}",
            connection=connection_key,
            backend=backend,
        )
        drop_ch_table(
            connection,
            table_name,
            ch_cluster=None,
            query_label=query_label,
            if_exists=if_exists,
            wait_for_absence=ch_wait_for_absence,
            wait_timeout_seconds=ch_wait_timeout_seconds,
            wait_poll_interval_seconds=ch_wait_poll_interval_seconds,
        )
        return

    if ch_drop_distributed and ch_drop_shard:
        shard_table = build_ch_shard_table_name(table_name)
        time_print(
            f"Dropping ClickHouse table {table_name}",
            connection=connection_key,
            backend=backend,
        )
        time_print(
            f"Dropping paired ClickHouse shard table {shard_table}",
            connection=connection_key,
            backend=backend,
        )
        drop_ch_distributed_table_pair(
            connection,
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
            if_exists=if_exists,
            wait_for_absence=ch_wait_for_absence,
            wait_timeout_seconds=ch_wait_timeout_seconds,
            wait_poll_interval_seconds=ch_wait_poll_interval_seconds,
            ch_retry_per_host_drops=ch_retry_per_host_drops,
            per_host_connection_factory=(
                lambda host: get_ch_connection_for_host(
                    connection_key,
                    host,
                )
            ),
        )
        return

    if ch_drop_distributed:
        time_print(
            f"Dropping ClickHouse table {table_name}",
            connection=connection_key,
            backend=backend,
        )
        drop_ch_table(
            connection,
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
            if_exists=if_exists,
            wait_for_absence=ch_wait_for_absence,
            wait_timeout_seconds=ch_wait_timeout_seconds,
            wait_poll_interval_seconds=ch_wait_poll_interval_seconds,
        )
        return

    if ch_drop_shard:
        shard_table = build_ch_shard_table_name(table_name)
        time_print(
            f"Dropping ClickHouse shard table {shard_table} for {table_name}",
            connection=connection_key,
            backend=backend,
        )
        drop_ch_table(
            connection,
            shard_table,
            ch_cluster=ch_cluster,
            query_label=query_label,
            if_exists=if_exists,
            wait_for_absence=ch_wait_for_absence,
            wait_timeout_seconds=ch_wait_timeout_seconds,
            wait_poll_interval_seconds=ch_wait_poll_interval_seconds,
        )
        return

    raise ValueError(
        "At least one of ch_drop_shard or ch_drop_distributed must be True."
    )


def _is_default_ch_shard_table_name(table_name: str) -> bool:
    try:
        table = parse_one(table_name, read="clickhouse", into=exp.Table)
    except Exception:
        return False
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        return False
    return str(table.this.this).endswith("_shard")


def _normalize_target_tables(table: str | list[str]) -> list[str]:
    if isinstance(table, str):
        return [_normalize_non_empty_string(table, "table")]
    if not isinstance(table, list):
        raise TypeError("table must be a string or a list of strings.")
    normalized_tables = [
        _normalize_non_empty_string(table_name, "table")
        for table_name in table
    ]
    if not normalized_tables:
        raise ValueError("table must not be empty.")
    return normalized_tables
