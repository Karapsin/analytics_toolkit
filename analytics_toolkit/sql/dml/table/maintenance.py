from __future__ import annotations

from typing import Any

from ...backend_adapters import get_backend_adapter
from ...backends.registry import get_backend_capability
from ...connection.config import get_connection_config, resolve_connection_backend
from ...connection.get_sql_connection import get_sql_connection
from ...connection.errors import UnsupportedConnectionTypeError
from ...execution.operation_runner import timed_public_sql_function
from ...execution.plans import SqlOperationMetadata, SqlPlan
from analytics_toolkit.general import time_print
from ._basic_ops import (
    build_drop_table_sql,
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
    if not get_backend_capability(backend).supports_analyze:
        if dry_run or return_sql:
            return SqlPlan(
                operation="analyze_table",
                target_alias=connection_type,
                target_backend=backend,
                target_table=table_name,
                options={
                    "skipped": True,
                    "reason": f"{backend} analyze is a no-op",
                },
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
        sql = get_backend_adapter(backend).analyze_table_sql(
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
    adapter = get_backend_adapter(config.backend)
    time_print(
        f"Vacuuming table {table_name}",
        connection=config.connection_key,
        backend=config.backend,
    )
    try:
        adapter.vacuum_table(
            conn,
            table_name,
            analyze=analyze,
            full=full,
            verbose=verbose,
        )
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
    if_exists: bool = True,
    operation_label: str = "stage table",
) -> None:
    backend = resolve_connection_backend(connection_backend)
    adapter = get_backend_adapter(backend)

    def operation(attempt: int) -> None:
        connection = connection_ref["connection"]
        try:
            drop_table(
                backend,
                connection,
                table_name,
                query_label=query_label,
                if_exists=if_exists,
            )
            return None
        except Exception:
            adapter.rollback_quietly(connection)
            replace_connection_fn(connection_key, connection_ref)
            raise

    retry_fn(
        operation_name=f"dropping {operation_label} {table_name} on {connection_key}",
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
    if_exists: bool = True,
    dry_run: bool = False,
    return_sql: bool = False,
    wait_for_absence: bool = False,
) -> SqlPlan | None:
    backend = resolve_connection_backend(connection_type)
    if dry_run or return_sql:
        sql = build_drop_table_sql(
            backend,
            table_name,
            if_exists=if_exists,
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
        if_exists=if_exists,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )
    if wait_for_absence:
        get_backend_adapter(backend).wait_for_table_absence(
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
    adapter = get_backend_adapter("ch")
    adapter.drop_table_with_options(
        connection,
        table_name,
        connection_key=connection_key or "",
        ch_cluster=ch_cluster,
        ch_drop_shard=True,
        ch_drop_distributed=True,
        query_label=query_label,
        if_exists=True,
        ch_wait_for_absence=wait_for_absence,
        ch_wait_timeout_seconds=wait_timeout_seconds,
        ch_wait_poll_interval_seconds=wait_poll_interval_seconds,
        ch_retry_per_host_drops=(
            ch_retry_per_host_drops and connection_key is not None
        ),
    )

def clear_ch_distributed_table_data(
    connection: Any,
    table_name: str,
    ch_cluster: str = "{cluster}",
    query_label: str | None = None,
) -> None:
    adapter = get_backend_adapter("ch")
    adapter.execute_commands(
        connection,
        adapter.build_clear_target_sqls(
            table_name,
            include_ch_shard=True,
            ch_cluster=ch_cluster,
            query_label=query_label,
        ),
    )
