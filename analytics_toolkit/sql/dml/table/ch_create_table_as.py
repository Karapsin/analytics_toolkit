from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import sqlparse
from sqlglot import exp, parse_one

from ...ch_options import resolve_ch_retry_per_host_drops_concurrency
from ...connection.config import get_connection_config
from ...connection.errors import InvalidSqlInputError, UnsupportedConnectionTypeError
from ...connection.get_sql_connection import (
    get_ch_connection_for_host,
    get_sql_connection,
)
from ...labels import apply_query_label
from ...operation_runner import timed_public_sql_function, tracked_sql_operation
from ...plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ...ch_lifecycle import (
    build_create_ch_distributed_table_pair_sqls,
    build_drop_ch_distributed_table_pair_sqls,
    drop_ch_distributed_table_pair,
)
from ...ddl.create_sql_table import (
    _normalize_non_empty_string,
    _wait_for_ch_distributed_table_pair,
    build_table_schema_column_definitions,
    build_ch_shard_table_name,
    normalize_table_schema,
    quote_identifier,
)
from analytics_toolkit.general import time_print
from .models import ChCreateTableAsOptions
from .table_ops import _execute_ch_command


@timed_public_sql_function
def ch_create_table_as(
    db_key: str,
    table_name: str,
    query: str,
    *,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    sharding_key: str = "rand()",
    ch_retry_per_host_drops: bool = True,
    ch_retry_per_host_drops_concurrency: int | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: dict[str, str] | None = None,
) -> SqlPlan | SqlOperationResult | None:
    config = get_connection_config(db_key)
    if config.backend != "ch":
        raise UnsupportedConnectionTypeError(
            f"ch_create_table_as requires a ch connection, got '{config.backend}'."
        )

    target_table = _normalize_non_empty_string(table_name, "table_name")
    query_sql = _normalize_single_query(query)
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    options = ChCreateTableAsOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        target_table=target_table,
        query_sql=query_sql,
        table_schema=normalize_table_schema(table_schema),
        ch_partition_by=ch_partition_by,
        ch_order_by=ch_order_by,
        ch_engine=ch_engine,
        ch_cluster=cluster_name,
        ch_sharding_key=sharding_key,
        ch_retry_per_host_drops=bool(ch_retry_per_host_drops),
        ch_retry_per_host_drops_concurrency=(
            resolve_ch_retry_per_host_drops_concurrency(
                ch_retry_per_host_drops=bool(ch_retry_per_host_drops),
                ch_retry_per_host_drops_concurrency=(
                    ch_retry_per_host_drops_concurrency
                ),
            )
        ),
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
    )

    if options.dry_run or options.return_sql:
        target_shard_table = build_ch_shard_table_name(options.target_table)
        sqls = [
            *build_drop_ch_distributed_table_pair_sqls(
                options.target_table,
                ch_cluster=options.ch_cluster,
            ),
            *_build_ch_create_table_as_dry_run_create_sqls(options),
            _build_insert_select_sql(options.target_table, options.query_sql),
        ]
        plan = SqlPlan(
            operation="ch_create_table_as",
            target_alias=options.connection_key,
            target_backend=options.backend,
            target_table=options.target_table,
            options={
                "ch_partition_by": options.ch_partition_by,
                "ch_order_by": options.ch_order_by,
                "ch_engine": options.ch_engine,
                "ch_cluster": options.ch_cluster,
                "sharding_key": options.ch_sharding_key,
                "table_schema": options.table_schema,
            },
            metadata=SqlOperationMetadata(
                statement_count=len(sqls),
                query_label=options.query_label,
            ),
        )
        phases = ["drop_target"] * 4 + ["create_target"] * 4 + ["insert_target"]
        for sql, phase in zip(sqls, phases):
            plan.add(
                sql,
                alias=options.connection_key,
                backend=options.backend,
                phase=phase,
                target_table=options.target_table,
                query_label=options.query_label,
            )
        return plan

    metadata = SqlOperationMetadata(query_label=options.query_label)
    connection = get_sql_connection(config.connection_key)
    try:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="ch_create_table_as",
            alias=options.connection_key,
            backend=options.backend,
            phase="create_target",
            query_label=options.query_label,
            preview_sql=options.query_sql,
        ):
            target_shard_table = build_ch_shard_table_name(options.target_table)
            time_print(
                f"Creating ClickHouse table {options.target_table} from query on "
                f"{options.connection_key}"
            )
            time_print(
                f"Dropping target ClickHouse table pair {options.target_table} / "
                f"{target_shard_table}"
            )
            drop_ch_distributed_table_pair(
                connection,
                options.target_table,
                ch_cluster=options.ch_cluster,
                query_label=options.query_label,
                wait_for_absence=True,
                ch_retry_per_host_drops=options.ch_retry_per_host_drops,
                ch_retry_per_host_drops_concurrency=(
                    options.ch_retry_per_host_drops_concurrency
                ),
                per_host_connection_factory=(
                    lambda host: get_ch_connection_for_host(
                        options.connection_key,
                        host,
                    )
                ),
            )

            if options.table_schema is None:
                time_print(f"Inferring ClickHouse schema for {options.target_table}")
                try:
                    joined_columns = _infer_ch_query_columns(
                        connection,
                        options.query_sql,
                    )
                except Exception as exc:
                    _annotate_ch_cte_join_exception(
                        exc,
                        options.query_sql,
                        backend=options.backend,
                    )
                    raise
            else:
                time_print(f"Validating ClickHouse schema for {options.target_table}")
                try:
                    source_columns = _inspect_ch_query_column_names(
                        connection,
                        options.query_sql,
                    )
                except Exception as exc:
                    _annotate_ch_cte_join_exception(
                        exc,
                        options.query_sql,
                        backend=options.backend,
                    )
                    raise
                joined_columns = build_table_schema_column_definitions(
                    "ch",
                    options.table_schema,
                    columns=source_columns,
                )
            shard_sql, local_shard_sql, distributed_sql, local_distributed_sql = (
                build_ch_create_table_as_sqls(
                    table_name=options.target_table,
                    joined_columns=joined_columns,
                    query=options.query_sql,
                    ch_partition_by=options.ch_partition_by,
                    ch_order_by=options.ch_order_by,
                    ch_engine=options.ch_engine,
                    ch_cluster=options.ch_cluster,
                    ch_sharding_key=options.ch_sharding_key,
                    ch_replace_table=True,
                    query_label=options.query_label,
                )
            )
            metadata.statement_count = 8

            time_print(f"Creating target shard table {target_shard_table}")
            _execute_ch_command(connection, shard_sql)
            time_print(f"Creating local shard table {target_shard_table}")
            _execute_ch_command(connection, local_shard_sql)
            time_print(f"Creating target distributed table {options.target_table}")
            _execute_ch_command(connection, distributed_sql)
            time_print(f"Creating local distributed table {options.target_table}")
            _execute_ch_command(connection, local_distributed_sql)
            time_print(f"Waiting for target table pair {options.target_table}")
            _wait_for_ch_distributed_table_pair(
                connection,
                options.target_table,
                ch_cluster=options.ch_cluster,
            )
            time_print(f"Inserting query results into {options.target_table}")
            try:
                connection.command(
                    apply_query_label(
                        _build_insert_select_sql(
                            options.target_table,
                            options.query_sql,
                        ),
                        options.query_label,
                    )
                )
            except Exception as exc:
                _annotate_ch_cte_join_exception(
                    exc,
                    options.query_sql,
                    backend=options.backend,
                )
                raise
            time_print(f"Finished creating ClickHouse table {options.target_table}")
    finally:
        time_print(f"Closing {config.connection_key} connection")
        connection.close()
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata)
    return None


def build_ch_create_table_as_sqls(
    table_name: str,
    joined_columns: str,
    query: str,
    *,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = True,
    query_label: str | None = None,
) -> list[str]:
    target_table = _normalize_non_empty_string(table_name, "table_name")
    _normalize_single_query(query)
    columns_sql = _normalize_non_empty_string(joined_columns, "joined_columns")
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    sharding_key = _normalize_non_empty_string(ch_sharding_key, "ch_sharding_key")
    engine = _normalize_non_empty_string(ch_engine, "ch_engine")
    return build_create_ch_distributed_table_pair_sqls(
        table_name=target_table,
        joined_columns=columns_sql,
        ch_partition_by=ch_partition_by,
        ch_order_by=ch_order_by,
        ch_engine=engine,
        ch_cluster=cluster_name,
        ch_sharding_key=sharding_key,
        ch_replace_table=ch_replace_table,
        query_label=query_label,
    )


def _build_ch_create_table_as_dry_run_create_sqls(
    options: ChCreateTableAsOptions,
) -> list[str]:
    if options.table_schema is None:
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
        ch_partition_by=options.ch_partition_by,
        ch_order_by=options.ch_order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_replace_table=True,
    )


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
        raise InvalidSqlInputError(
            "ch_create_table_as expects exactly one SQL statement."
        )
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
    return connection.query(
        "SELECT *\n"
        "FROM (\n"
        f"{query}\n"
        ") AS _ch_create_table_as_source\n"
        "LIMIT 0"
    )


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
    if "UNKNOWN_TABLE" not in message.upper() and not _CLICKHOUSE_MISSING_TABLE_RE.search(
        message
    ):
        return []

    missing_names: list[str] = []
    for match in _CLICKHOUSE_MISSING_TABLE_RE.finditer(message):
        table_name = match.group("table")
        relation_name = table_name.rsplit(".", maxsplit=1)[-1]
        normalized_name = relation_name.strip("`\"")
        if normalized_name:
            missing_names.append(normalized_name)
    return missing_names


def _normalize_clickhouse_identifier(identifier: str) -> str:
    return identifier.strip().strip("`\"").lower()


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
