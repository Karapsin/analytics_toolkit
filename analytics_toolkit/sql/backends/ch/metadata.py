from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple, cast

import pandas as pd
import sqlglot.tokens as sqlglot_tokens
from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.execution.backend_options import backend_operation

from .cluster_functions import static_name
from .dialect import ClickHouseDialect, sql_string_literal
from .outage_context import logical_statistics

_ROW_COUNT_COLUMN = "row_count"
_TABLE_SIZE_BYTES_COLUMN = "table_size_bytes"
_DISTRIBUTED_IDENTITY_ARGS = 3
_ZERO_ARGUMENT_CALL_TOKENS = 3
_TOKEN_TYPE: Any = vars(sqlglot_tokens)["TokenType"]


@dataclass(frozen=True)
class ClickHouseShardTable:
    cluster: str
    database: str
    table: str


class _LogicalStats(Dict[ClickHouseShardTable, Tuple[object, object]]):
    """Values retain the coverage evidence of their complete collection scope."""

    data_complete = True

    def __init__(self) -> None:
        super().__init__()
        self.cluster_status: list[dict[str, Any]] = []


def apply_clickhouse_logical_stats(
    connection_key: str,
    tables: pd.DataFrame,
    *,
    cluster: str,
    read_sql: Callable[[str, str], Any],
) -> pd.DataFrame:
    refs = {
        index: ClickHouseShardTable(cluster, row["schema"], row["table_name"])
        for index, row in tables.iterrows()
        if isinstance(row.get("engine"), str) and row["engine"].lower() != "distributed"
    }
    if not refs:
        return tables
    stats = read_clickhouse_shard_stats(connection_key, set(refs.values()), read_sql=read_sql)
    result = tables.copy()
    result[_ROW_COUNT_COLUMN] = result[_ROW_COUNT_COLUMN].astype(object)
    for index, ref in refs.items():
        if ref in stats:
            result.loc[index, _ROW_COUNT_COLUMN] = stats[ref][0]
            result.loc[index, _TABLE_SIZE_BYTES_COLUMN] = stats[ref][1]
    _attach_stats_coverage(result, stats)
    return result


def _attach_stats_coverage(tables: pd.DataFrame, stats: Any) -> None:
    if not getattr(stats, "data_complete", True):
        tables.attrs["data_complete"] = False
    tables.attrs["ch_cluster_status"] = [
        *tables.attrs.get("ch_cluster_status", []),
        *getattr(stats, "cluster_status", []),
    ]


def apply_clickhouse_shard_stats(
    connection_key: str,
    tables: pd.DataFrame,
    *,
    read_sql: Callable[[str, str], Any],
) -> pd.DataFrame:
    if "engine" not in tables.columns or "engine_full" not in tables.columns:
        return tables

    shard_refs_by_index: dict[object, ClickHouseShardTable] = {}
    unresolved = False
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
        else:
            unresolved = True

    if unresolved:
        tables.attrs["data_complete"] = False

    if not shard_refs_by_index:
        return tables

    stats = read_clickhouse_shard_stats(
        connection_key,
        set(shard_refs_by_index.values()),
        read_sql=read_sql,
    )
    if not stats:
        _attach_stats_coverage(tables, stats)
        return tables

    resolved = tables.copy()
    resolved[_ROW_COUNT_COLUMN] = resolved[_ROW_COUNT_COLUMN].astype(object)
    for index, shard_ref in shard_refs_by_index.items():
        stat = stats.get(shard_ref)
        if stat is None:
            continue
        row_count, table_size_bytes = stat
        resolved.at[index, _ROW_COUNT_COLUMN] = row_count
        resolved.at[index, _TABLE_SIZE_BYTES_COLUMN] = table_size_bytes
    _attach_stats_coverage(resolved, stats)
    return resolved


def read_clickhouse_shard_stats(
    connection_key: str,
    shard_refs: set[ClickHouseShardTable],
    *,
    read_sql: Callable[[str, str], Any],
) -> dict[ClickHouseShardTable, tuple[object, object]]:
    from .metadata_values import decode_metadata_frame  # noqa: PLC0415

    stats = _LogicalStats()
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
        targets = tuple(sorted((ref.cluster, ref.database, ref.table) for ref in cluster_refs))
        try:
            with logical_statistics(targets=targets), backend_operation() as state:
                result = cast("pd.DataFrame", read_sql(connection_key, query))
                result = decode_metadata_frame(result, ("shard_database", "shard_table"))
                result.attrs["data_complete"] = (
                    result.attrs.get("data_complete", True) and not state.partial
                )
                result.attrs["ch_cluster_status"] = list(state.statuses.values())
        except ClickHouseClusterTopologyError:
            # An unverified replication group cannot produce a certified logical
            # estimate. Preserve deterministic topology failure semantics.
            raise
        except Exception:
            stats.data_complete = False
            continue
        stats.data_complete = stats.data_complete and result.attrs.get("data_complete", True)
        stats.cluster_status.extend(result.attrs.get("ch_cluster_status", []))
        for _, row in result.iterrows():
            complete = row.get("statistics_complete", False)
            if complete is None or pd.isna(complete) or not bool(complete):
                stats.data_complete = False
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
    if not shard_refs.issubset(stats):
        stats.data_complete = False
    return stats


def build_clickhouse_shard_stats_query(
    cluster: str,
    shard_refs: set[ClickHouseShardTable],
) -> str:
    from .statistics_query import build_shard_statistics_query  # noqa: PLC0415

    return build_shard_statistics_query(cluster, shard_refs)


def resolve_clickhouse_cluster_macro(
    connection_key: str,
    cluster: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> str:
    macro_name = extract_clickhouse_macro_name(cluster)
    if macro_name is None:
        return cluster

    query = f"SELECT getMacro({sql_string_literal(macro_name)}) AS cluster_name"
    try:
        result = cast("pd.DataFrame", read_sql(connection_key, query))
    except Exception:
        return cluster

    if result.empty or "cluster_name" not in result.columns:
        return cluster
    value = result.iloc[0]["cluster_name"]
    if pd.isna(value):
        return cluster
    from .metadata_values import metadata_text  # noqa: PLC0415

    resolved = metadata_text(value).strip()
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

    args = _distributed_arguments(engine_full)
    if args is None or len(args) < _DISTRIBUTED_IDENTITY_ARGS:
        return None

    cluster = static_name(args[0])
    database = (
        normalize_clickhouse_distributed_database_arg(
            args[1].sql(dialect=ClickHouseDialect), default_database
        )
        if _is_current_database(args[1])
        else static_name(args[1])
    )
    table = static_name(args[2])
    if not cluster or not database or not table:
        return None
    return ClickHouseShardTable(cluster=cluster, database=database, table=table)


def normalize_clickhouse_distributed_database_arg(
    value: str,
    default_database: object,
) -> str | None:
    expression = _engine_expression(value)
    if _is_zero_argument_database_call(value) or _is_current_database(expression):
        return default_database if isinstance(default_database, str) and default_database else None
    return normalize_clickhouse_engine_arg(value)


def normalize_clickhouse_engine_arg(value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None

    expression = _engine_expression(normalized)
    if isinstance(expression, exp.Literal) and expression.is_string:
        return str(expression.this)
    decoded = static_name(expression) if expression is not None else None
    if decoded is not None:
        return decoded

    # Keep the exported helper's historical best-effort fallback. Ownership
    # extraction above accepts only a fully parsed static Distributed identity.
    if len(normalized) >= 2 and normalized[0] == normalized[-1]:
        if normalized[0] == "'":
            return normalized[1:-1].replace("''", "'")
        if normalized[0] in {'"', "`"}:
            return normalized[1:-1].replace(normalized[0] * 2, normalized[0])

    return normalized


def _engine_expression(value: str) -> exp.Expression | None:
    try:
        statements = parse(f"SELECT {value}", read=ClickHouseDialect)
    except SqlglotError:
        return None
    if len(statements) != 1:
        return None
    statement = statements[0]
    if (
        not isinstance(statement, exp.Select)
        or len(statement.expressions) != 1
        or any(value for key, value in statement.args.items() if key != "expressions")
    ):
        return None
    return cast("exp.Expression", statement.expressions[0])


def _is_current_database(value: exp.Expression | None) -> bool:
    if value is not None and value.key in {"currentdatabase", "currentschema"}:
        return not any(value.args.values())
    return (
        isinstance(value, exp.Anonymous)
        and value.name.casefold() in {"currentdatabase", "database"}
        and not any(argument for key, argument in value.args.items() if key != "this")
    )


def _is_zero_argument_database_call(value: str) -> bool:
    try:
        tokens = ClickHouseDialect().tokenize(value)
    except SqlglotError:
        return False
    return (
        len(tokens) == _ZERO_ARGUMENT_CALL_TOKENS
        and tokens[0].token_type in {_TOKEN_TYPE.VAR, _TOKEN_TYPE.DATABASE}
        and tokens[0].text.casefold() in {"currentdatabase", "database"}
        and tokens[1].token_type is _TOKEN_TYPE.L_PAREN
        and tokens[2].token_type is _TOKEN_TYPE.R_PAREN
    )


def _distributed_arguments(value: str) -> list[exp.Expression] | None:
    try:
        tokens = ClickHouseDialect().tokenize(value)
    except SqlglotError:
        return None
    if (
        len(tokens) < _ZERO_ARGUMENT_CALL_TOKENS
        or tokens[0].token_type is not _TOKEN_TYPE.VAR
        or tokens[0].text.casefold() != "distributed"
        or tokens[1].token_type is not _TOKEN_TYPE.L_PAREN
        or tokens[-1].token_type is not _TOKEN_TYPE.R_PAREN
    ):
        return None
    arguments = _argument_slices(value[tokens[1].end + 1 : tokens[-1].start])
    if arguments is None:
        return None
    expressions = [
        exp.Anonymous(this="currentDatabase")
        if _is_zero_argument_database_call(argument)
        else _engine_expression(argument)
        for argument in arguments
    ]
    if any(expression is None for expression in expressions):
        return None
    return cast("list[exp.Expression]", expressions)


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


def _argument_slices(args_sql: str) -> list[str] | None:
    try:
        tokens = ClickHouseDialect().tokenize(args_sql)
    except SqlglotError:
        return None
    delimiters = {
        _TOKEN_TYPE.L_PAREN: _TOKEN_TYPE.R_PAREN,
        _TOKEN_TYPE.L_BRACKET: _TOKEN_TYPE.R_BRACKET,
        _TOKEN_TYPE.L_BRACE: _TOKEN_TYPE.R_BRACE,
    }
    closing = set(delimiters.values())
    stack = []
    arguments = []
    start = 0
    for token in tokens:
        kind = token.token_type
        if kind in delimiters:
            stack.append(delimiters[kind])
        elif kind in closing:
            if not stack or stack.pop() != kind:
                return None
        elif kind is _TOKEN_TYPE.COMMA and not stack:
            arguments.append(args_sql[start : token.start].strip())
            start = token.end + 1
    if stack:
        return None
    arguments.append(args_sql[start:].strip())
    return arguments


def split_top_level_args(args_sql: str) -> list[str]:
    return [argument for argument in (_argument_slices(args_sql) or []) if argument]


def skip_whitespace(sql: str, position: int) -> int:
    while position < len(sql) and sql[position].isspace():
        position += 1
    return position


def is_clickhouse_identifier_boundary(sql: str, position: int) -> bool:
    return (
        position < 0
        or position >= len(sql)
        or not is_clickhouse_identifier_char(
            sql[position],
        )
    )


def is_clickhouse_identifier_char(char: str) -> bool:
    return char.isalnum() or char == "_"
