from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlglot import exp, parse_one


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
    del adapter, config
    if trino_catalog is not None:
        from ...connection.errors import InvalidSqlInputError

        raise InvalidSqlInputError(
            "trino_catalog is only supported for Trino connections."
        )
    from ..metadata import build_clickhouse_show_tables_query

    return build_clickhouse_show_tables_query(
        schema,
        table_names,
        conditions,
        include_distributed_metadata=ch_distributed_table_stats,
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
    if not ch_distributed_table_stats:
        return tables
    from .metadata import apply_clickhouse_shard_stats

    if read_sql is None:
        from ...dml.io.read_sql import read_sql as read_sql_impl
    else:
        read_sql_impl = read_sql

    return apply_clickhouse_shard_stats(
        connection_key,
        tables,
        read_sql=read_sql_impl,
    )


def extract_table_ddl(
    adapter: Any,
    connection_key: str,
    table_name: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    del adapter
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
        f"ALTER TABLE {shard_table}{cluster_clause} "
        f"DROP PARTITION {sql_string_literal(key)}"
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
    del adapter, connection_key, table_pattern
    from ..utils import sql_string_literal

    result = connection.query(
        "SELECT name FROM system.tables WHERE database = "
        f"{sql_string_literal(transfer_staging_schema)}"
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
            raise ValueError(
                "ch_drop_shard must be True when dropping a ClickHouse shard table."
            )
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
    raise ValueError(
        "At least one of ch_drop_shard or ch_drop_distributed must be True."
    )


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
    connection_key: str,
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
    del adapter
    from analytics_toolkit.general import time_print
    from ...connection.get_sql_connection import get_ch_connection_for_host
    from .ddl import build_ch_shard_table_name
    from .lifecycle import drop_ch_distributed_table_pair, drop_ch_table

    if _is_default_ch_shard_table_name(table_name):
        if not ch_drop_shard:
            raise ValueError(
                "ch_drop_shard must be True when dropping a ClickHouse shard table."
            )
        time_print(
            f"Dropping ClickHouse table {table_name}",
            connection=connection_key,
            backend="ch",
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
            backend="ch",
        )
        time_print(
            f"Dropping paired ClickHouse shard table {shard_table}",
            connection=connection_key,
            backend="ch",
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
                lambda host: get_ch_connection_for_host(connection_key, host)
            ),
        )
        return

    if ch_drop_distributed:
        time_print(
            f"Dropping ClickHouse table {table_name}",
            connection=connection_key,
            backend="ch",
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
            backend="ch",
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
        return adapter.clear_table_sqls(table_name, query_label=query_label)

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
    if not drop_target_if_exists:
        return False

    from analytics_toolkit.general import time_print
    from ...connection.get_sql_connection import get_ch_connection_for_host
    from .lifecycle import drop_ch_distributed_table_pair

    target_existed_before_drop = False
    if not ch_only_shard:
        target_existed_before_drop = bool(
            adapter.table_exists(
                connection,
                table_name,
                connection_key=connection_key or "ch",
            )
        )

    if ch_only_shard:
        time_print(f"Dropping existing ClickHouse table {table_name}")
        adapter.drop_table(
            connection,
            table_name,
            ch_cluster=None,
            query_label=query_label,
        )
        return False

    time_print(f"Dropping existing ClickHouse distributed table pair {table_name}")
    per_host_connection_factory = (
        (lambda host: get_ch_connection_for_host(connection_key, host))
        if connection_key is not None
        else None
    )
    drop_ch_distributed_table_pair(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        query_label=query_label,
        wait_for_absence=True,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
        per_host_connection_factory=per_host_connection_factory,
    )
    return target_existed_before_drop


def wait_for_table_absence(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    ch_cluster: str | None = None,
) -> None:
    del adapter
    from ...clickhouse.wait import (
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

    return _estimate_clickhouse_source_rows(
        connection,
        source_sql,
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
