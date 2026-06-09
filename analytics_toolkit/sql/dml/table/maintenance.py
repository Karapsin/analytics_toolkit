from __future__ import annotations

from typing import Any

from ...backend_adapters import get_backend_adapter
from ...clickhouse.lifecycle import (
    drop_ch_distributed_table_pair as _drop_ch_pair,
    truncate_ch_distributed_table_pair as _truncate_ch_pair,
)
from ...connection.config import get_connection_config, resolve_connection_backend
from ...connection.get_sql_connection import get_ch_connection_for_host, get_sql_connection
from ...connection.errors import UnsupportedConnectionTypeError
from ...execution.operation_runner import timed_public_sql_function
from ...execution.plans import SqlOperationMetadata, SqlPlan
from analytics_toolkit.general import time_print
from ._basic_ops import (
    build_analyze_table_sql,
    build_drop_table_sql,
    quote_qualified_table_name,
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

    time_print(
        f"Analyzing target table {table_name}",
        connection=connection_type,
        backend=backend,
    )
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
def gp_vacuum(
    table_name: str,
    analyze: bool = False,
    full: bool = False,
    verbose: bool = True,
    db_key: str = "gp",
) -> None:
    config = get_connection_config(db_key)
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

    time_print(
        f"Vacuuming table {qualified_table_name}",
        connection=config.connection_key,
        backend=config.backend,
    )
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
        time_print(
            "Closing connection",
            connection=config.connection_key,
            backend=config.backend,
            phase="close",
        )
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
        from ...clickhouse.wait import _wait_for_ch_table_absence
        from ...clickhouse.wait import _wait_for_ch_table_absence_on_cluster

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
    ch_retry_per_host_drops_concurrency: int | None = None,
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
        ch_retry_per_host_drops_concurrency=ch_retry_per_host_drops_concurrency,
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
