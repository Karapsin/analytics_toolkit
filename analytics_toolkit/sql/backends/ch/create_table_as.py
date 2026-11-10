from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

import sqlparse
from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.ddl_defaults import legacy_clickhouse_scope

from ...connection.config import get_connection_config
from ...connection.errors import InvalidSqlInputError, UnsupportedConnectionTypeError
from ...connection.get_sql_connection import get_sql_connection
from ...ddl.identifiers import quote_identifier
from ...ddl.schema import build_table_schema_column_definitions, normalize_table_schema
from ...dml.table._basic_ops import _execute_ch_command
from ...dml.table.models import ChCreateTableAsOptions
from ...execution.labels import apply_query_label
from ...execution.operation_runner import timed_public_sql_function
from ...execution.plans import SqlOperationResult, SqlPlan
from .creation_policy import ClickHouseCreationPolicy, resolve_clickhouse_creation_policy
from .ddl import (
    _normalize_non_empty_string,
    build_ch_local_create_table_sql,
    build_ch_shard_table_name,
)
from .lifecycle import (
    build_create_ch_distributed_table_pair_sqls,
    build_drop_ch_distributed_table_pair_sqls,
    build_drop_ch_table_sqls,
)
from .wait import _wait_for_ch_distributed_table_pair

# These runtime names are imported by existing internal consumers and test clients.
__all__ = [
    "ChCreateTableAsOptions",
    "SqlOperationResult",
    "SqlPlan",
    "_execute_ch_command",
    "_wait_for_ch_distributed_table_pair",
    "ch_create_table_as",
    "get_sql_connection",
]


@timed_public_sql_function
def ch_create_table_as(
    db_key: str,
    table_name: str,
    query: str,
    *,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_only_shard: bool = False,
    ch_retry_per_host_drops: bool = True,
    insert_data: bool = True,
    drop_target_if_exists: bool = True,
    dry_run: bool = False,
    return_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: dict[str, str] | None = None,
) -> SqlPlan | SqlOperationResult | None:
    # The shared execution helper imports this module's compatibility codecs.
    from .policy_ctas import create_table_as_with_policy  # noqa: PLC0415

    config = get_connection_config(db_key)
    if config.backend != "ch":
        message = f"ch_create_table_as requires a ch connection, got '{config.backend}'."
        raise UnsupportedConnectionTypeError(message)
    target_table = _normalize_non_empty_string(table_name, "table_name")
    query_sql = _normalize_single_query(query)
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    only_shard = _normalize_only_shard(ch_only_shard)
    policy = _resolve_legacy_ctas_policy(
        ch_engine, cluster_name, ch_sharding_key, only_shard=only_shard
    )
    result = create_table_as_with_policy(
        policy=policy,
        target_key=config.connection_key,
        target_table=target_table,
        source_sql=query_sql,
        partition_by=partition_by,
        order_by=order_by,
        ch_only_shard=only_shard,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
        insert_data=bool(insert_data),
        drop_target_if_exists=bool(drop_target_if_exists),
        ch_replace_table=False,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        table_schema=normalize_table_schema(table_schema),
    )
    return cast("SqlPlan | SqlOperationResult | None", result)


def _resolve_legacy_ctas_policy(
    engine: str,
    cluster: str,
    sharding_key: str,
    *,
    only_shard: bool,
) -> ClickHouseCreationPolicy:
    scope = legacy_clickhouse_scope()
    if only_shard:
        scope = replace(
            scope, create_distributed_pair=False, shard=replace(scope.shard, on_cluster=None)
        )
    return resolve_clickhouse_creation_policy(
        scope,
        ch_engine=engine,
        ch_cluster=None if only_shard else cluster,
        ch_sharding_key=sharding_key,
        ch_distributed_table=not only_shard,
        ch_only_shard=only_shard,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        warn_ch_cluster=False,
    )


def build_ch_create_table_as_sqls(
    table_name: str,
    joined_columns: str,
    query: str,
    *,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = True,
    ch_only_shard: bool = False,
    query_label: str | None = None,
) -> list[str]:
    target_table = _normalize_non_empty_string(table_name, "table_name")
    _normalize_single_query(query)
    columns_sql = _normalize_non_empty_string(joined_columns, "joined_columns")
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    ch_sharding_key = _normalize_non_empty_string(ch_sharding_key, "ch_sharding_key")
    engine = _normalize_non_empty_string(ch_engine, "ch_engine")
    ch_only_shard = _normalize_only_shard(ch_only_shard)
    if ch_only_shard:
        return [
            apply_query_label(
                build_ch_local_create_table_sql(
                    table_name=target_table,
                    joined_columns=columns_sql,
                    partition_by=partition_by,
                    order_by=order_by,
                    ch_engine=engine,
                    ch_replace_table=ch_replace_table,
                ),
                query_label,
            )
        ]
    return build_create_ch_distributed_table_pair_sqls(
        table_name=target_table,
        joined_columns=columns_sql,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=engine,
        ch_cluster=cluster_name,
        ch_sharding_key=ch_sharding_key,
        ch_replace_table=ch_replace_table,
        query_label=query_label,
    )


def _build_ch_create_table_as_dry_run_drop_sqls(
    options: ChCreateTableAsOptions,
) -> list[str]:
    if options.ch_only_shard:
        return build_drop_ch_table_sqls(
            options.target_table,
            ch_cluster=None,
        )
    return build_drop_ch_distributed_table_pair_sqls(
        options.target_table,
        ch_cluster=options.ch_cluster,
    )


def _build_ch_create_table_as_dry_run_create_sqls(
    options: ChCreateTableAsOptions,
) -> list[str]:
    if options.table_schema is None:
        if options.ch_only_shard:
            return [f"CREATE TABLE IF NOT EXISTS {options.target_table} (<query schema>)"]
        target_shard_table = build_ch_shard_table_name(options.target_table)
        return [
            f"CREATE TABLE IF NOT EXISTS {target_shard_table} (<query schema>)",
            f"CREATE TABLE IF NOT EXISTS {target_shard_table} (<query schema>)",
            f"CREATE TABLE IF NOT EXISTS {options.target_table} (<query schema>)",
            f"CREATE TABLE IF NOT EXISTS {options.target_table} (<query schema>)",
        ]

    joined_columns = build_table_schema_column_definitions("ch", options.table_schema)
    return build_ch_create_table_as_sqls(
        table_name=options.target_table,
        joined_columns=joined_columns,
        query=options.query_sql,
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_replace_table=options.drop_target_if_exists,
        ch_only_shard=options.ch_only_shard,
    )


def _normalize_only_shard(ch_only_shard: bool) -> bool:
    if not isinstance(ch_only_shard, bool):
        raise ValueError("ch_only_shard must be a boolean.")
    return ch_only_shard


def _normalize_single_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise InvalidSqlInputError("Query string must not be empty.")

    statements = [
        statement.strip().rstrip(";").rstrip()
        for statement in sqlparse.split(normalized)
        if statement.strip()
    ]
    if len(statements) != 1:
        raise InvalidSqlInputError("ch_create_table_as expects exactly one SQL statement.")
    return statements[0]


def _infer_ch_query_columns(connection: Any, query: str) -> str:
    result = _query_ch_create_table_as_source(connection, query)
    column_names = list(getattr(result, "column_names", ()) or ())
    column_types = list(getattr(result, "column_types", ()) or ())
    if not column_names:
        raise ValueError("ch_create_table_as query must return at least one column.")
    if len(column_names) != len(column_types):
        raise ValueError("Could not infer ClickHouse column types from query result.")

    column_defs = [
        f"{quote_identifier(str(column_name), 'ch')} {_ch_type_name(column_type)}"
        for column_name, column_type in zip(column_names, column_types)
    ]
    return ", ".join(column_defs)


def _inspect_ch_query_column_names(connection: Any, query: str) -> list[str]:
    result = _query_ch_create_table_as_source(connection, query)
    column_names = list(getattr(result, "column_names", ()) or ())
    if not column_names:
        raise ValueError("ch_create_table_as query must return at least one column.")
    return [str(column_name) for column_name in column_names]


def _query_ch_create_table_as_source(connection: Any, query: str) -> Any:
    return connection.query(f"SELECT *\nFROM (\n{query}\n) AS _ch_create_table_as_source\nLIMIT 0")


def _ch_type_name(column_type: Any) -> str:
    type_name = getattr(column_type, "name", None)
    if type_name is None:
        type_name = str(column_type)
    return _normalize_non_empty_string(str(type_name), "column_type")


def _build_insert_select_sql(table_name: str, query: str) -> str:
    return f"INSERT INTO {table_name}\n{query}"


_CLICKHOUSE_MISSING_TABLE_RE = re.compile(
    r"\bTable\s+"
    r"(?P<table>(?:[`\"]?[A-Za-z_][\w$-]*[`\"]?\.)*[`\"]?[A-Za-z_][\w$-]*[`\"]?)"
    r"\s+does\s+not\s+exist\b",
    re.IGNORECASE,
)


def _annotate_ch_cte_join_exception(
    exc: Exception,
    query: str,
    *,
    backend: str,
) -> None:
    if backend != "ch":
        return

    cte_names = _extract_query_cte_names(query)
    if not cte_names:
        return

    for missing_table in _clickhouse_missing_table_names(exc):
        cte_name = cte_names.get(_normalize_clickhouse_identifier(missing_table))
        if cte_name is None:
            continue
        _add_exception_note_once(exc, _ch_cte_join_note(cte_name))
        return


def _extract_query_cte_names(query: str) -> dict[str, str]:
    try:
        tree = parse_one(query, read="clickhouse")
    except Exception:
        return {}

    cte_names: dict[str, str] = {}
    for cte in tree.find_all(exp.CTE):
        if cte.args.get("scalar"):
            continue
        cte_name = str(cte.alias).strip()
        if not cte_name:
            continue
        cte_names.setdefault(_normalize_clickhouse_identifier(cte_name), cte_name)
    return cte_names


def _clickhouse_missing_table_names(exc: Exception) -> list[str]:
    message = str(exc)
    if "UNKNOWN_TABLE" not in message.upper() and not _CLICKHOUSE_MISSING_TABLE_RE.search(message):
        return []

    missing_names: list[str] = []
    for match in _CLICKHOUSE_MISSING_TABLE_RE.finditer(message):
        table_name = match.group("table")
        relation_name = table_name.rsplit(".", maxsplit=1)[-1]
        normalized_name = relation_name.strip('`"')
        if normalized_name:
            missing_names.append(normalized_name)
    return missing_names


def _normalize_clickhouse_identifier(identifier: str) -> str:
    return identifier.strip().strip('`"').lower()


def _ch_cte_join_note(cte_name: str) -> str:
    return (
        f"ClickHouse could not resolve CTE '{cte_name}' on a remote shard. "
        "For distributed queries that join a small CTE, use GLOBAL "
        "LEFT/INNER/RIGHT JOIN syntax, for example GLOBAL LEFT JOIN "
        f"{cte_name} AS alias, or inline the CTE right side."
    )


def _add_exception_note_once(exc: Exception, note: str) -> None:
    if note in getattr(exc, "__notes__", ()):
        return
    add_note = getattr(exc, "add_note", None)
    if add_note is not None:
        add_note(note)
        return
    notes = list(getattr(exc, "__notes__", ()))
    notes.append(note)
    try:
        setattr(exc, "__notes__", notes)
    except Exception:
        return
