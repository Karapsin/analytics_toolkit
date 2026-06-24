from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any


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
    del (
        adapter,
        config,
        schema,
        table_names,
        conditions,
        trino_catalog,
        ch_distributed_table_stats,
    )
    raise NotImplementedError


def postprocess_show_tables(
    adapter: Any,
    connection_key: str,
    tables: Any,
    *,
    ch_distributed_table_stats: bool = False,
    read_sql: Callable[[str, str], Any] | None = None,
) -> Any:
    del adapter, connection_key, ch_distributed_table_stats, read_sql
    return tables


def extract_table_ddl(
    adapter: Any,
    connection_key: str,
    table_name: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    del adapter, connection_key, table_name, read_sql
    raise NotImplementedError


def validate_drop_partitions_options(
    adapter: Any,
    *,
    partition_column: str | None,
    gp_truncate: bool,
) -> None:
    from ..connection.errors import InvalidSqlInputError

    del adapter
    if gp_truncate:
        raise InvalidSqlInputError(
            "gp_truncate=True is only supported for Greenplum connections."
        )
    if partition_column is not None:
        raise InvalidSqlInputError(
            "trino_partition_column is only supported for Trino partition deletes."
        )


def build_drop_partitions_sqls(
    adapter: Any,
    table: str,
    partition_keys: Sequence[str],
    *,
    partition_column: str | None = None,
    gp_truncate: bool = False,
    ch_cluster: str = "{cluster}",
) -> list[str]:
    del adapter, table, partition_keys, partition_column, gp_truncate, ch_cluster
    raise NotImplementedError


def build_create_partition_sql(
    adapter: Any,
    table: str,
    *,
    name: str,
    start: str | None = None,
    end: str | None = None,
    value: str | None = None,
) -> str:
    del adapter, table, name, start, end, value
    raise NotImplementedError


def query_transfer_stage_table_names(
    adapter: Any,
    connection: Any,
    *,
    connection_key: str,
    transfer_staging_schema: str,
    table_pattern: str,
) -> list[str]:
    del connection_key
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name LIKE %s
            """.strip(),
            (transfer_staging_schema, table_pattern),
        )
        return [str(row[0]) for row in (cursor.fetchall() or [])]
    finally:
        cursor.close()


def qualify_transfer_stage_table_name(
    adapter: Any,
    connection_key: str,
    transfer_staging_schema: str,
    table_name: str,
) -> str:
    del connection_key
    parts = (transfer_staging_schema, table_name)
    return ".".join(_quote_identifier_part_when_needed(adapter, part) for part in parts)


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
    del ch_drop_shard, ch_drop_distributed
    return [
        adapter.drop_table_sql(
            table_name,
            if_exists=if_exists,
            ch_cluster=ch_cluster,
            query_label=query_label,
        )
    ]


def build_drop_target_sqls(
    adapter: Any,
    table_name: str,
    *,
    ch_cluster: str | None = "{cluster}",
    ch_only_shard: bool = False,
    query_label: str | None = None,
) -> list[str]:
    del ch_only_shard
    return [
        adapter.drop_table_sql(
            table_name,
            if_exists=True,
            ch_cluster=ch_cluster,
            query_label=query_label,
        )
    ]


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
    del (
        ch_drop_shard,
        ch_drop_distributed,
        ch_wait_for_absence,
        ch_wait_timeout_seconds,
        ch_wait_poll_interval_seconds,
        ch_retry_per_host_drops,
    )
    from analytics_toolkit.general import time_print

    time_print(
        f"Dropping table {table_name}",
        connection=connection_key,
        backend=adapter.backend,
    )
    adapter.drop_table(
        connection,
        table_name,
        if_exists=if_exists,
        ch_cluster=ch_cluster,
        query_label=query_label,
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
    del include_ch_shard, ch_cluster, ch_only_shard
    return adapter.clear_table_sqls(table_name, query_label=query_label)


def companion_table_name(adapter: Any, table_name: str) -> str | None:
    del adapter, table_name
    return None


def _quote_identifier_part_when_needed(adapter: Any, identifier: str) -> str:
    if _is_simple_identifier(identifier):
        return identifier
    return adapter.quote_identifier(identifier)


def _is_simple_identifier(identifier: str) -> bool:
    if not identifier:
        return False
    if not (identifier[0].isalpha() or identifier[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in identifier)
