from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from ..registry import get_backend_adapter
from ...ddl.identifiers import (
    _add_table_identifier_suffix,
    _identifier_name,
    _parse_table_name,
    quote_identifier,
)


def _build_ch_create_table_sqls(
    *,
    table_name: str,
    joined_columns: str,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_distributed_table: bool,
    ch_only_shard: bool,
    ch_replace_table: bool,
    **_: object,
) -> list[str]:
    if ch_only_shard:
        return [
            build_ch_local_create_table_sql(
                table_name=table_name,
                joined_columns=joined_columns,
                partition_by=partition_by,
                order_by=order_by,
                ch_engine=ch_engine,
                ch_replace_table=ch_replace_table,
            )
        ]
    if ch_distributed_table:
        return build_ch_distributed_create_table_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_replace_table=ch_replace_table,
        )
    return [
        f"CREATE TABLE {table_name} ({joined_columns}) "
        "ENGINE = MergeTree ORDER BY tuple()"
    ]

def build_ch_distributed_create_table_sqls(
    table_name: str,
    joined_columns: str,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = False,
) -> list[str]:
    shard_table = build_ch_shard_table_name(table_name)
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    engine = _normalize_non_empty_string(ch_engine, "ch_engine")
    ch_sharding_key = _normalize_non_empty_string(ch_sharding_key, "ch_sharding_key")
    partition_sql = _build_partition_by_sql(partition_by)
    order_by_sql = _build_order_by_sql(order_by)
    database_name, shard_relation_name = split_ch_table_name_for_distributed_engine(
        shard_table
    )

    cluster_create_statement = (
        "CREATE OR REPLACE TABLE" if ch_replace_table else "CREATE TABLE IF NOT EXISTS"
    )

    shard_sql = (
        f"{cluster_create_statement} {shard_table}\n"
        f"ON CLUSTER {_format_ch_cluster_name(cluster_name)}\n"
        f"({joined_columns})\n"
        f"ENGINE = {engine}\n"
        f"{partition_sql}"
        f"{order_by_sql}"
    )
    local_shard_sql = (
        build_ch_local_create_table_sql(
            table_name=shard_table,
            joined_columns=joined_columns,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=engine,
        )
    )
    distributed_sql = (
        f"{cluster_create_statement} {table_name}\n"
        f"ON CLUSTER {_format_ch_cluster_name(cluster_name)}\n"
        f"({joined_columns})\n"
        "ENGINE = Distributed(\n"
        f"    {_sql_string_literal(cluster_name)},\n"
        f"    {database_name},\n"
        f"    {_sql_string_literal(shard_relation_name)},\n"
        f"    {ch_sharding_key}\n"
        ")"
    )
    local_distributed_sql = (
        f"CREATE TABLE IF NOT EXISTS {table_name}\n"
        f"({joined_columns})\n"
        "ENGINE = Distributed(\n"
        f"    {_sql_string_literal(cluster_name)},\n"
        f"    {database_name},\n"
        f"    {_sql_string_literal(shard_relation_name)},\n"
        f"    {ch_sharding_key}\n"
        ")"
    )
    return [shard_sql, local_shard_sql, distributed_sql, local_distributed_sql]

def build_ch_local_create_table_sql(
    table_name: str,
    joined_columns: str,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_replace_table: bool = False,
) -> str:
    engine = _normalize_non_empty_string(ch_engine, "ch_engine")
    partition_sql = _build_partition_by_sql(partition_by)
    order_by_sql = _build_order_by_sql(order_by)
    create_statement = (
        "CREATE OR REPLACE TABLE" if ch_replace_table else "CREATE TABLE IF NOT EXISTS"
    )
    sql = (
        f"{create_statement} {table_name}\n"
        f"({joined_columns})\n"
        f"ENGINE = {engine}\n"
        f"{partition_sql}"
        f"{order_by_sql}"
    )
    return add_explicit_ch_uuid_to_local_replicated_create(sql)

def build_ch_shard_table_name(table_name: str) -> str:
    return _add_table_identifier_suffix(table_name, "_shard", "clickhouse")

def split_ch_table_name_for_distributed_engine(table_name: str) -> tuple[str, str]:
    table = _parse_table_name(table_name, "clickhouse")
    relation_name = _identifier_name(table.this)
    database = table.args.get("db")
    if database is None:
        return "currentDatabase()", relation_name
    return _sql_string_literal(_identifier_name(database)), relation_name

def _build_partition_by_sql(
    partition_by: Sequence[str] | str | None,
) -> str:
    if partition_by is None:
        return ""
    expression = _normalize_ch_expression(partition_by, "partition_by")
    return f"PARTITION BY {expression}\n"

def _build_order_by_sql(order_by: Sequence[str] | str | None) -> str:
    expression = (
        "tuple()"
        if order_by is None
        else _normalize_ch_expression(order_by, "order_by")
    )
    return f"ORDER BY {expression}"

def _normalize_ch_expression(value: Sequence[str] | str, option_name: str) -> str:
    if isinstance(value, str):
        return _normalize_non_empty_string(value, option_name)

    columns = [_normalize_non_empty_string(column, option_name) for column in value]
    if not columns:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if len(set(columns)) != len(columns):
        raise ValueError(f"{option_name} must not contain duplicate column names.")
    quoted_columns = [quote_identifier(column, "ch") for column in columns]
    if len(quoted_columns) == 1:
        return quoted_columns[0]
    return f"({', '.join(quoted_columns)})"

def _normalize_non_empty_string(value: str, option_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{option_name} must not be empty.")
    return normalized

def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

def _format_ch_cluster_name(cluster_name: str) -> str:
    normalized = cluster_name.strip()
    if not normalized:
        return normalized
    if normalized[0] in {"'", '"', "`"}:
        return normalized
    if _is_simple_identifier(normalized):
        return normalized
    return _sql_string_literal(normalized)

def _is_simple_identifier(identifier: str) -> bool:
    if not identifier:
        return False
    if not (identifier[0].isalpha() or identifier[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in identifier)

def _execute_ch_command(connection: Any, sql: str) -> None:
    get_backend_adapter("ch").execute_command(connection, sql)

def add_explicit_ch_uuid_to_local_replicated_create(sql: str) -> str:
    if re.search(r"\bON\s+CLUSTER\b", sql, flags=re.IGNORECASE):
        return sql
    if not re.search(r"\bENGINE\s*=\s*Replicated", sql, flags=re.IGNORECASE):
        return sql
    if re.search(r"\bUUID\s+'[^']+'", sql, flags=re.IGNORECASE):
        return sql

    table_header, separator, rest = sql.partition("\n(")
    if not separator:
        return sql
    return f"{table_header}\nUUID '{uuid.uuid4()}'{separator}{rest}"
