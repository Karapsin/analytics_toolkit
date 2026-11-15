from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlglot import exp, parse_one

import analytics_toolkit.sql.backends.common_methods as _common_methods
from analytics_toolkit.sql.backends.base import _apply_query_label
from analytics_toolkit.sql.backends.models import ReadColumnResult

from .routing import (
    connection_cluster_routing,
    local_sql,
    query_local,
    routed_connection_sql,
)


def build_execute_create_as_sqls(  # noqa: PLR0913
    adapter: Any,
    *,
    table_name: str,
    source_sql: str,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any,
    partition_by: Any,
    order_by: Any,
    ddl_properties: Any,
    ch_creation_policy: Any,
    ch_only_shard: bool,
    if_not_exists: bool,
) -> tuple[list[str], bool]:
    del adapter, gp_distributed_by_key, gp_partitions, ddl_properties
    from .creation_policy import build_policy_create_as_sqls  # noqa: PLC0415

    return build_policy_create_as_sqls(
        table_name=table_name,
        source_sql=source_sql,
        partition_by=partition_by,
        order_by=order_by,
        policy=ch_creation_policy,
        ch_only_shard=ch_only_shard,
        if_not_exists=if_not_exists,
    )


read_columns = _common_methods.read_columns


def build_materialize_transfer_source_sql(
    adapter: Any,
    table_name: str,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> str:
    return _apply_query_label(
        f"CREATE TABLE {table_name} ENGINE = MergeTree ORDER BY tuple() AS "
        f"{adapter.strip_query_semicolon(source_sql)}",
        query_label,
    )


def read_columns_impl(
    adapter: Any,
    connection: Any,
    query: str,
    read_dbapi_columns: Callable[[Any, str], Any],
) -> ReadColumnResult:
    del adapter, read_dbapi_columns
    result = connection.query(query, column_oriented=True)
    column_names = tuple(str(name) for name in result.column_names)
    columns = tuple(list(column) for column in result.result_columns)
    return ReadColumnResult(
        column_names=column_names,
        columns=columns,
        row_count=int(result.row_count),
    )


def build_show_tables_query(
    adapter: Any,
    config: Any,
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
    *,
    trino_catalog: str | None = None,
    ch_distributed_table_stats: bool = False,
) -> str:
    del adapter, config, ch_distributed_table_stats
    if trino_catalog is not None:
        from ...connection.errors import InvalidSqlInputError

        raise InvalidSqlInputError("trino_catalog is only supported for Trino connections.")
    from ..metadata import build_clickhouse_show_tables_query

    return build_clickhouse_show_tables_query(
        schema,
        table_names,
        conditions,
        include_distributed_metadata=True,
    )


def postprocess_show_tables(
    adapter: Any,
    connection_key: str,
    tables: Any,
    *,
    ch_distributed_table_stats: bool = False,
    read_sql: Callable[[str, str], Any] | None = None,
) -> Any:
    del adapter
    from analytics_toolkit.sql.connection.config import get_connection_config

    from .metadata import apply_clickhouse_logical_stats, apply_clickhouse_shard_stats

    if read_sql is None:
        from ...dml.io.read_sql import read_sql as read_sql_impl
    else:
        read_sql_impl = read_sql
    tables = tables.drop_duplicates(subset=["schema", "table_name"]).copy()
    from analytics_toolkit.sql.execution.backend_options import current_backend_operation

    state = current_backend_operation()
    if state is not None:
        tables.attrs["data_complete"] = not state.partial
        tables.attrs["ch_cluster_status"] = list(state.statuses.values())
    routing = getattr(get_connection_config(connection_key), "cluster_routing", None)
    if routing is not None:
        tables = apply_clickhouse_logical_stats(
            connection_key,
            tables,
            cluster=routing.cluster,
            read_sql=read_sql_impl,
        )
    if ch_distributed_table_stats:
        tables = apply_clickhouse_shard_stats(connection_key, tables, read_sql=read_sql_impl)
    return tables


def extract_table_ddl(
    adapter: Any,
    connection_key: str,
    table_name: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    del adapter
    with local_sql():
        result = read_sql(connection_key, f"SHOW CREATE TABLE {table_name}")
    return _first_result_value(result, table_name)


def build_drop_partitions_sqls(
    adapter: Any,
    table: str,
    partition_keys: Sequence[str],
    *,
    partition_column: str | None = None,
    gp_truncate: bool = False,
    ch_cluster: str = "{cluster}",
) -> list[str]:
    del adapter, partition_column, gp_truncate
    from ..utils import sql_string_literal
    from .adapter import ch_cluster_clause
    from .ddl import build_ch_shard_table_name

    shard_table = build_ch_shard_table_name(table)
    cluster_clause = ch_cluster_clause(ch_cluster)
    return [
        f"ALTER TABLE {shard_table}{cluster_clause} DROP PARTITION {sql_string_literal(key)}"
        for key in partition_keys
    ]


def query_transfer_stage_table_names(
    adapter: Any,
    connection: Any,
    *,
    connection_key: str,
    transfer_staging_schema: str,
    table_pattern: str,
) -> list[str]:
    del adapter, connection_key
    from ..utils import sql_string_literal

    routing = connection_cluster_routing(connection)
    source = "system.tables"
    if routing is not None:
        source = f"clusterAllReplicas({sql_string_literal(routing.cluster)}, system, tables)"
    result = query_local(
        connection,
        f"SELECT DISTINCT name FROM {source} WHERE database = "
        f"{sql_string_literal(transfer_staging_schema)} AND name LIKE "
        f"{sql_string_literal(table_pattern)}",
    )
    return [str(row[0]) for row in (result.result_rows or [])]


def qualify_transfer_stage_table_name(
    adapter: Any,
    connection_key: str,
    transfer_staging_schema: str,
    table_name: str,
) -> str:
    del adapter, connection_key
    return f"{transfer_staging_schema}.{table_name}"


def build_drop_tables_sqls(
    adapter: Any,
    table_name: str,
    *,
    ch_cluster: str | None = "{cluster}",
    ch_drop_shard: bool = True,
    ch_drop_distributed: bool = True,
    if_exists: bool = False,
    query_label: str | None = None,
) -> list[str]:
    del adapter
    from .ddl import build_ch_shard_table_name
    from .lifecycle import (
        build_drop_ch_distributed_table_pair_sqls,
        build_drop_ch_table_sqls,
    )

    if _is_default_ch_shard_table_name(table_name):
        if not ch_drop_shard:
            raise ValueError("ch_drop_shard must be True when dropping a ClickHouse shard table.")
        return build_drop_ch_table_sqls(
            table_name,
            ch_cluster=None,
            query_label=query_label,
            if_exists=if_exists,
        )
    if ch_drop_distributed and ch_drop_shard:
        return build_drop_ch_distributed_table_pair_sqls(
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
            if_exists=if_exists,
        )
    if ch_drop_distributed:
        return build_drop_ch_table_sqls(
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
            if_exists=if_exists,
        )
    if ch_drop_shard:
        return build_drop_ch_table_sqls(
            build_ch_shard_table_name(table_name),
            ch_cluster=ch_cluster,
            query_label=query_label,
            if_exists=if_exists,
        )
    raise ValueError("At least one of ch_drop_shard or ch_drop_distributed must be True.")


def build_drop_target_sqls(
    adapter: Any,
    table_name: str,
    *,
    ch_cluster: str | None = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
) -> list[str]:
    if ch_only_shard:
        return [
            adapter.drop_table_sql(
                table_name,
                if_exists=True,
                ch_cluster=None,
                query_label=query_label,
            )
        ]
    return build_drop_tables_sqls(
        adapter,
        table_name,
        ch_cluster=ch_cluster,
        ch_drop_shard=True,
        ch_drop_distributed=True,
        if_exists=True,
        query_label=query_label,
    )


def drop_table_with_options(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    connection_key: str | None,
    ch_cluster: str | None = "{cluster}",
    ch_drop_shard: bool = True,
    ch_drop_distributed: bool = True,
    ch_wait_for_absence: bool = False,
    ch_wait_timeout_seconds: int = 300,
    ch_wait_poll_interval_seconds: float = 1,
    ch_retry_per_host_drops: bool = True,
    if_exists: bool = False,
    query_label: str | None = None,
) -> None:
    drop_tables_with_options(
        adapter,
        connection,
        [table_name],
        connection_key=connection_key,
        ch_cluster=ch_cluster,
        ch_drop_shard=ch_drop_shard,
        ch_drop_distributed=ch_drop_distributed,
        if_exists=if_exists,
        query_label=query_label,
        ch_wait_for_absence=ch_wait_for_absence,
        ch_wait_timeout_seconds=ch_wait_timeout_seconds,
        ch_wait_poll_interval_seconds=ch_wait_poll_interval_seconds,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
    )


def drop_tables_with_options(
    adapter: Any, connection: Any, tables: Sequence[str], **options: Any
) -> None:
    from .drop_execution import DropExecutionOptions, execute_verified_drop
    from .drop_layout import (
        DropIntent,
        VerifiedDropLayout,
        VerifiedDropResource,
        certify_existing_drop,
        verified_drop_resource_identity,
    )

    intent = DropIntent(
        options.get("connection_key") or "ch",
        options.get("ch_cluster", "{cluster}"),
        options.get("ch_drop_shard", True),
        options.get("ch_drop_distributed", True),
        options.get("if_exists", False),
    )
    plans = [certify_existing_drop(connection, table, intent) for table in tables]
    resources: dict[tuple[object, ...], VerifiedDropResource] = {}
    for plan in plans:
        for resource in plan.resources:
            resources.setdefault(verified_drop_resource_identity(resource), resource)
    execute_verified_drop(
        adapter,
        connection,
        VerifiedDropLayout(tuple(resources.values()), plans[0].config),
        DropExecutionOptions(
            if_exists=intent.if_exists,
            wait_for_absence=options.get("ch_wait_for_absence", False),
            timeout_seconds=options.get("ch_wait_timeout_seconds", 300),
            poll_interval_seconds=options.get("ch_wait_poll_interval_seconds", 1),
            retry_per_host_drops=options.get("ch_retry_per_host_drops", True),
            query_label=options.get("query_label"),
        ),
    )


def pending_drop_plan_options(adapter: Any) -> dict[str, Any]:
    del adapter
    return {
        "pending_existing_layout_verification": True,
        "pending_resource_scope_resolution": True,
        "execution_requires": "Complete existing table-role ownership and endpoint certification.",
    }


def build_clear_target_sqls(
    adapter: Any,
    table_name: str,
    *,
    query_label: str | None = None,
    include_ch_shard: bool = False,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
) -> list[str]:
    if not include_ch_shard or ch_only_shard:
        return list(adapter.clear_table_sqls(table_name, query_label=query_label))

    from .lifecycle import build_truncate_ch_distributed_table_pair_sqls

    return build_truncate_ch_distributed_table_pair_sqls(
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
    )


def build_transfer_replace_target_sqls(
    adapter: Any,
    table_name: str,
    *,
    query_label: str | None = None,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
) -> list[str]:
    return build_drop_target_sqls(
        adapter,
        table_name,
        ch_cluster=ch_cluster,
        ch_only_shard=ch_only_shard,
        query_label=query_label,
    )


def transfer_replace_target_phase(adapter: Any) -> str:
    del adapter
    return "drop_target"


def companion_table_name(adapter: Any, table_name: str) -> str | None:
    del adapter
    from .ddl import build_ch_shard_table_name

    return build_ch_shard_table_name(table_name)


def prepare_existing_target_for_create_from_sql(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    drop_target_if_exists: bool,
    ch_cluster: str = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
    connection_key: str | None = None,
    ch_retry_per_host_drops: bool = True,
) -> bool:
    del ch_only_shard
    if not drop_target_if_exists:
        return False
    exists = bool(
        adapter.table_exists(connection, table_name, connection_key=connection_key or "ch")
    )
    if not exists:
        return False
    drop_table_with_options(
        adapter,
        connection,
        table_name,
        connection_key=connection_key,
        ch_cluster=ch_cluster,
        ch_wait_for_absence=True,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
        if_exists=True,
        query_label=query_label,
    )
    return True


def wait_for_table_absence(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    ch_cluster: str | None = None,
) -> None:
    del adapter
    from .wait import (
        _wait_for_ch_table_absence,
        _wait_for_ch_table_absence_on_cluster,
    )

    if ch_cluster is None:
        _wait_for_ch_table_absence(connection, table_name)
        return
    _wait_for_ch_table_absence_on_cluster(
        connection,
        table_name,
        ch_cluster=ch_cluster,
    )


def estimate_source_rows(
    adapter: Any,
    connection: Any,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> int | None:
    del adapter
    from ..source_estimate import _estimate_clickhouse_source_rows

    routed_source_sql = routed_connection_sql(connection, source_sql)
    with local_sql(connection):
        return _estimate_clickhouse_source_rows(
            connection,
            routed_source_sql,
            query_label=query_label,
        )


def _first_result_value(result: Any, table_name: str) -> str:
    import pandas as pd

    if result.empty or len(result.columns) == 0:
        raise ValueError(f"No DDL returned for table {table_name}.")

    value = result.iat[0, 0]
    if pd.isna(value):
        raise ValueError(f"No DDL returned for table {table_name}.")
    return str(value)


def _is_default_ch_shard_table_name(table_name: str) -> bool:
    try:
        table = parse_one(table_name, read="clickhouse", into=exp.Table)
    except Exception:
        return False
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        return False
    return str(table.this.this).endswith("_shard")
