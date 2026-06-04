from __future__ import annotations

from collections.abc import Sequence

from ..connection.errors import UnsupportedConnectionTypeError
from ..execution.labels import apply_query_label
from .clickhouse import (
    _build_ch_create_table_sqls,
    _normalize_non_empty_string,
    _sql_string_literal,
)
from .identifiers import column_list_sql, quote_identifier


def _validate_only_shard(backend: str, only_shard: bool, option_owner: str) -> None:
    if not isinstance(only_shard, bool):
        raise ValueError("only_shard must be a boolean.")
    if only_shard and backend != "ch":
        raise ValueError(
            f"only_shard can only be used when {option_owner} has type 'ch'."
        )

def _build_backend_create_table_sqls(
    *,
    backend: str,
    table_name: str,
    joined_columns: str,
    gp_distributed_by_key: list[str] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_distributed_table: bool,
    only_shard: bool,
    ch_replace_table: bool,
) -> list[str]:
    try:
        build_sqls = _CREATE_TABLE_SQL_BUILDERS[backend]
    except KeyError as exc:
        raise UnsupportedConnectionTypeError(
            "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
        ) from exc
    return build_sqls(
        table_name=table_name,
        joined_columns=joined_columns,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        only_shard=only_shard,
        ch_replace_table=ch_replace_table,
    )

def _build_gp_create_table_sqls(
    *,
    table_name: str,
    joined_columns: str,
    gp_distributed_by_key: list[str] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    **_: object,
) -> list[str]:
    if order_by is not None:
        raise ValueError("order_by is not supported for Greenplum create table.")
    storage_sql = (
        "WITH (appendonly=true,\n"
        "        blocksize=32768,\n"
        "        compresstype=zstd,\n"
        "        compresslevel=4,\n"
        "        orientation=column)"
    )
    if gp_distributed_by_key:
        distribution_sql = (
            f"DISTRIBUTED BY ({column_list_sql(gp_distributed_by_key, 'gp')})"
        )
    else:
        distribution_sql = "DISTRIBUTED RANDOMLY"
    partition_sql = _build_gp_partition_by_sql(partition_by)
    return [
        f"CREATE TABLE {table_name} ({joined_columns}) "
        f"{storage_sql} {distribution_sql}{partition_sql}"
    ]

def _build_trino_create_table_sqls(
    *,
    table_name: str,
    joined_columns: str,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    **_: object,
) -> list[str]:
    properties = _build_trino_table_properties(
        partition_by=partition_by,
        order_by=order_by,
    )
    return [
        f"CREATE TABLE {table_name} ({joined_columns}) "
        f"WITH ({properties})"
    ]

def _apply_query_label_to_sqls(sqls: list[str], query_label: str | None) -> list[str]:
    return [apply_query_label(sql, query_label) for sql in sqls]

def _build_gp_partition_by_sql(partition_by: Sequence[str] | str | None) -> str:
    if partition_by is None:
        return ""
    partition_column = _normalize_gp_partition_column(partition_by)
    return f" PARTITION BY RANGE ({quote_identifier(partition_column, 'gp')})"

def _normalize_gp_partition_column(partition_by: Sequence[str] | str) -> str:
    if isinstance(partition_by, str):
        return _normalize_non_empty_string(partition_by, "partition_by")

    columns = [_normalize_non_empty_string(column, "partition_by") for column in partition_by]
    if len(columns) != 1:
        raise ValueError("partition_by for Greenplum must contain exactly one column.")
    return columns[0]

def _build_trino_table_properties(
    *,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
) -> str:
    properties = [
        "format = 'PARQUET'",
        "object_store_layout_enabled = true",
    ]
    partition_entries = _normalize_trino_property_entries(partition_by, "partition_by")
    if partition_entries:
        properties.append(
            f"partitioning = {_trino_string_array_sql(partition_entries)}"
        )
    order_entries = _normalize_trino_property_entries(order_by, "order_by")
    if order_entries:
        properties.append(f"sorted_by = {_trino_string_array_sql(order_entries)}")
    return ", ".join(properties)

def _normalize_trino_property_entries(
    value: Sequence[str] | str | None,
    option_name: str,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_normalize_non_empty_string(value, option_name)]

    entries = [_normalize_non_empty_string(entry, option_name) for entry in value]
    if not entries:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if len(set(entries)) != len(entries):
        raise ValueError(f"{option_name} must not contain duplicate entries.")
    return entries

def _trino_string_array_sql(entries: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(_sql_string_literal(entry) for entry in entries) + "]"

_CREATE_TABLE_SQL_BUILDERS = {
    "gp": _build_gp_create_table_sqls,
    "trino": _build_trino_create_table_sqls,
    "ch": _build_ch_create_table_sqls,
}
