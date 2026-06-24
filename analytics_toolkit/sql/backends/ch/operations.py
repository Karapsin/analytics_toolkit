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
    from ...ddl.clickhouse import build_ch_shard_table_name
    from ..utils import sql_string_literal
    from .adapter import ch_cluster_clause

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


def _first_result_value(result: Any, table_name: str) -> str:
    import pandas as pd

    if result.empty or len(result.columns) == 0:
        raise ValueError(f"No DDL returned for table {table_name}.")

    value = result.iat[0, 0]
    if pd.isna(value):
        raise ValueError(f"No DDL returned for table {table_name}.")
    return str(value)
