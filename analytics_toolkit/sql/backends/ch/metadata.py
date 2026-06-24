from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, cast

import pandas as pd

from ..utils import sql_string_literal


_ROW_COUNT_COLUMN = "row_count"
_TABLE_SIZE_BYTES_COLUMN = "table_size_bytes"


@dataclass(frozen=True)
class ClickHouseShardTable:
    cluster: str
    database: str
    table: str


def apply_clickhouse_shard_stats(
    connection_key: str,
    tables: pd.DataFrame,
    *,
    read_sql: Callable[[str, str], Any],
) -> pd.DataFrame:
    if "engine" not in tables.columns or "engine_full" not in tables.columns:
        return tables

    shard_refs_by_index: dict[object, ClickHouseShardTable] = {}
    for index, row in tables.iterrows():
        engine = row.get("engine")
        if not isinstance(engine, str) or engine.lower() != "distributed":
            continue

        shard_ref = extract_clickhouse_distributed_shard_table(
            row.get("engine_full"),
            row.get("schema"),
        )
        if shard_ref is not None:
            shard_refs_by_index[index] = shard_ref

    if not shard_refs_by_index:
        return tables

    stats = read_clickhouse_shard_stats(
        connection_key,
        set(shard_refs_by_index.values()),
        read_sql=read_sql,
    )
    if not stats:
        return tables

    resolved = tables.copy()
    for index, shard_ref in shard_refs_by_index.items():
        stat = stats.get(shard_ref)
        if stat is None:
            continue
        row_count, table_size_bytes = stat
        resolved.at[index, _ROW_COUNT_COLUMN] = row_count
        resolved.at[index, _TABLE_SIZE_BYTES_COLUMN] = table_size_bytes
    return resolved


def read_clickhouse_shard_stats(
    connection_key: str,
    shard_refs: set[ClickHouseShardTable],
    *,
    read_sql: Callable[[str, str], Any],
) -> dict[ClickHouseShardTable, tuple[object, object]]:
    stats: dict[ClickHouseShardTable, tuple[object, object]] = {}
    refs_by_cluster: dict[str, set[ClickHouseShardTable]] = {}
    original_refs_by_resolved_ref: dict[
        ClickHouseShardTable,
        set[ClickHouseShardTable],
    ] = {}
    for shard_ref in shard_refs:
        cluster = resolve_clickhouse_cluster_macro(
            connection_key,
            shard_ref.cluster,
            read_sql=read_sql,
        )
        resolved_ref = ClickHouseShardTable(
            cluster=cluster,
            database=shard_ref.database,
            table=shard_ref.table,
        )
        refs_by_cluster.setdefault(cluster, set()).add(resolved_ref)
        original_refs_by_resolved_ref.setdefault(resolved_ref, set()).add(shard_ref)

    for cluster, cluster_refs in refs_by_cluster.items():
        query = build_clickhouse_shard_stats_query(cluster, cluster_refs)
        try:
            result = cast(pd.DataFrame, read_sql(connection_key, query))
        except Exception:
            continue
        for _, row in result.iterrows():
            database = row.get("shard_database")
            table = row.get("shard_table")
            if not isinstance(database, str) or not isinstance(table, str):
                continue
            resolved_ref = ClickHouseShardTable(
                cluster=cluster,
                database=database,
                table=table,
            )
            stat = (
                row.get(_ROW_COUNT_COLUMN),
                row.get(_TABLE_SIZE_BYTES_COLUMN),
            )
            stats[resolved_ref] = stat
            for original_ref in original_refs_by_resolved_ref.get(resolved_ref, ()):
                stats[original_ref] = stat
    return stats


def build_clickhouse_shard_stats_query(
    cluster: str,
    shard_refs: set[ClickHouseShardTable],
) -> str:
    table_filters = ", ".join(
        f"({sql_string_literal(shard_ref.database)}, "
        f"{sql_string_literal(shard_ref.table)})"
        for shard_ref in sorted(
            shard_refs,
            key=lambda value: (value.database, value.table),
        )
    )
    return f"""
SELECT
    database AS shard_database,
    name AS shard_table,
    sum(ifNull(total_rows, 0)) AS row_count,
    sum(ifNull(total_bytes, 0)) AS table_size_bytes
FROM cluster({sql_string_literal(cluster)}, system, tables)
WHERE (database, name) IN ({table_filters})
GROUP BY database, name
ORDER BY database, name
""".strip()


def resolve_clickhouse_cluster_macro(
    connection_key: str,
    cluster: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    macro_name = extract_clickhouse_macro_name(cluster)
    if macro_name is None:
        return cluster

    query = (
        "SELECT "
        f"getMacro({sql_string_literal(macro_name)}) AS cluster_name"
    )
    try:
        result = cast(pd.DataFrame, read_sql(connection_key, query))
    except Exception:
        return cluster

    if result.empty or "cluster_name" not in result.columns:
        return cluster
    value = result.iloc[0]["cluster_name"]
    if pd.isna(value):
        return cluster
    resolved = str(value).strip()
    return resolved or cluster


def extract_clickhouse_macro_name(value: str) -> str | None:
    match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
    if match is None:
        return None
    return match.group(1)


def extract_clickhouse_distributed_shard_table(
    engine_full: object,
    default_database: object,
) -> ClickHouseShardTable | None:
    if not isinstance(engine_full, str):
        return None

    args = extract_clickhouse_function_args(engine_full, "Distributed")
    if args is None or len(args) < 3:
        return None

    cluster = normalize_clickhouse_engine_arg(args[0])
    database = normalize_clickhouse_distributed_database_arg(
        args[1],
        default_database,
    )
    table = normalize_clickhouse_engine_arg(args[2])
    if not cluster or not database or not table:
        return None
    return ClickHouseShardTable(cluster=cluster, database=database, table=table)


def normalize_clickhouse_distributed_database_arg(
    value: str,
    default_database: object,
) -> str | None:
    database = normalize_clickhouse_engine_arg(value)
    if database is None:
        return None

    compact = "".join(database.split()).lower()
    if compact in {"currentdatabase()", "database()"}:
        if not isinstance(default_database, str):
            return None
        database = default_database.strip()
        if not database:
            return None
    return database


def normalize_clickhouse_engine_arg(value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None

    if len(normalized) >= 2 and normalized[0] == normalized[-1]:
        if normalized[0] == "'":
            return normalized[1:-1].replace("''", "'")
        if normalized[0] in {'"', "`"}:
            return normalized[1:-1].replace(normalized[0] * 2, normalized[0])

    return normalized


def extract_clickhouse_function_args(
    sql: str,
    function_name: str,
) -> list[str] | None:
    position = find_clickhouse_function_call(sql, function_name)
    if position is None:
        return None

    open_paren = skip_whitespace(sql, position + len(function_name))
    close_paren = find_matching_paren(sql, open_paren)
    if close_paren is None:
        return None
    return split_top_level_args(sql[open_paren + 1 : close_paren])


def find_clickhouse_function_call(sql: str, function_name: str) -> int | None:
    name_length = len(function_name)
    index = 0
    quote: str | None = None
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'" and index + 1 < len(sql):
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue

        candidate_end = index + name_length
        if (
            sql[index:candidate_end].lower() == function_name.lower()
            and is_clickhouse_identifier_boundary(sql, index - 1)
            and is_clickhouse_identifier_boundary(sql, candidate_end)
            and skip_whitespace(sql, candidate_end) < len(sql)
            and sql[skip_whitespace(sql, candidate_end)] == "("
        ):
            return index
        index += 1
    return None


def find_matching_paren(sql: str, open_paren: int) -> int | None:
    if open_paren >= len(sql) or sql[open_paren] != "(":
        return None

    depth = 1
    quote: str | None = None
    index = open_paren + 1
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'" and index + 1 < len(sql):
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def split_top_level_args(args_sql: str) -> list[str]:
    args: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    while index < len(args_sql):
        char = args_sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(args_sql) and args_sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'" and index + 1 < len(args_sql):
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            arg = args_sql[start:index].strip()
            if arg:
                args.append(arg)
            start = index + 1
        index += 1

    arg = args_sql[start:].strip()
    if arg:
        args.append(arg)
    return args


def skip_whitespace(sql: str, position: int) -> int:
    while position < len(sql) and sql[position].isspace():
        position += 1
    return position


def is_clickhouse_identifier_boundary(sql: str, position: int) -> bool:
    return position < 0 or position >= len(sql) or not is_clickhouse_identifier_char(
        sql[position],
    )


def is_clickhouse_identifier_char(char: str) -> bool:
    return char.isalnum() or char == "_"
