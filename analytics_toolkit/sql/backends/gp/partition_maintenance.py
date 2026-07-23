# ruff: noqa: EM101, EM102, FBT001, I001, PERF401, PLC0415, PLR0913, PLR2004, PYI041, TID252, TRY003
# mypy: disable-error-code="index,union-attr"
from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, cast

from ...backend_adapters import get_backend_adapter
from ...connection.config import get_connection_config
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    UnsupportedConnectionTypeError,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
from ...core.identifiers import TableIdentifier
from ...execution.operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from analytics_toolkit.general import time_print


@dataclass(frozen=True)
class _Options:
    connection_key: str
    table_names: tuple[str, ...]
    partition_names: tuple[str, ...] | None
    concurrency: int
    retry_cnt: int
    timeout_increment: int | float
    query_label: str | None
    dry_run: bool
    return_sql: bool
    return_metadata: bool


_LEAF_PARTITIONS_SQL = """
WITH RECURSIVE descendants AS (
    SELECT inheritance.inhrelid AS relation_oid
    FROM pg_catalog.pg_inherits AS inheritance
    WHERE inheritance.inhparent = to_regclass('{parent_table}')
    UNION
    SELECT inheritance.inhrelid
    FROM pg_catalog.pg_inherits AS inheritance
    JOIN descendants ON descendants.relation_oid = inheritance.inhparent
)
SELECT namespace.nspname AS schema_name, relation.relname AS relation_name
FROM descendants
JOIN pg_catalog.pg_class AS relation ON relation.oid = descendants.relation_oid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_inherits AS child_inheritance
    WHERE child_inheritance.inhparent = relation.oid
)
ORDER BY namespace.nspname, relation.relname
""".strip()


@timed_public_sql_function
def gp_analyze_partitioned_table(
    db_key: str,
    table_name: str | Sequence[str],
    partition_names: str | Sequence[str] | None = None,
    *,
    concurrency: int = 1,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
) -> SqlPlan | SqlOperationResult | None:
    options = _build_options(
        db_key,
        table_name,
        partition_names,
        concurrency,
        retry_cnt,
        timeout_increment,
        query_label,
        dry_run,
        return_sql,
        return_metadata,
    )
    names_by_table = _resolve_partitions(options)
    names = [name for group in names_by_table for name in group]
    plan = _build_plan(options, names)
    if options.dry_run or options.return_sql:
        return plan
    if names:
        time_print(
            f"Analyzing {len(names)} Greenplum partition(s)",
            connection=options.connection_key,
            backend="gp",
        )
        attempts = []
        statement_offset = 0
        for group in names_by_table:
            group_sqls = plan.sqls[statement_offset : statement_offset + len(group)]
            attempts.extend(_execute_all(options, group, group_sqls))
            statement_offset += len(group)
        plan.metadata.retry_attempts = max(item.retry_attempts or 0 for item in attempts)
    plan.metadata.operation_status = "success"
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=plan.metadata, plan=plan)
    return None


def _build_options(
    db_key: str,
    table_name: str | Sequence[str],
    partition_names: str | Sequence[str] | None,
    concurrency: int,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    dry_run: bool,
    return_sql: bool,
    return_metadata: bool,
) -> _Options:
    config = get_connection_config(db_key)
    if config.backend != "gp":
        raise UnsupportedConnectionTypeError(
            f"gp_analyze_partitioned_table requires a gp connection, got '{config.backend}'."
        )
    if concurrency.__class__ is not int or concurrency < 1:
        raise ValueError("concurrency must be an integer >= 1.")
    validate_retry_options(retry_cnt, timeout_increment)
    return _Options(
        connection_key=config.connection_key,
        table_names=cast("tuple[str, ...]", _normalize_names(table_name, "table_name")),
        partition_names=_normalize_names(partition_names, "partition_names"),
        concurrency=concurrency,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
    )


def _normalize_names(
    value: str | Sequence[str] | None, argument_name: str
) -> tuple[str, ...] | None:
    if value is None:
        return None
    raw_names = (
        [value] if isinstance(value, str) else list(value) if isinstance(value, Sequence) else None
    )
    if raw_names is None:
        raise InvalidSqlInputError(
            f"{argument_name} must be a fully qualified table name or sequence of names."
        )
    if not raw_names:
        raise InvalidSqlInputError(f"{argument_name} must not be empty.")
    names: list[str] = []
    for raw_name in raw_names:
        if not isinstance(raw_name, str):
            raise InvalidSqlInputError(f"{argument_name} must contain strings.")
        try:
            identifier = TableIdentifier.parse(raw_name.strip(), "gp")
        except ValueError as exc:
            raise InvalidSqlInputError(
                f"{argument_name} must contain valid fully qualified table names."
            ) from exc
        if len(identifier.parts) != 2:
            raise InvalidSqlInputError(
                f"{argument_name} must contain schema-qualified table names."
            )
        names.append(identifier.render_quoted("gp"))
    if len(set(names)) != len(names):
        raise InvalidSqlInputError(f"{argument_name} must not contain duplicates.")
    return tuple(names)


def _resolve_partitions(options: _Options) -> list[list[str]]:
    requested = set(options.partition_names or ())
    resolved: list[list[str]] = []
    for parent_table in options.table_names:
        discovered = _discover(options, parent_table)
        selected = (
            discovered
            if options.partition_names is None
            else [name for name in discovered if name in requested]
        )
        requested.difference_update(selected)
        resolved.append(selected)
    if requested:
        raise InvalidSqlInputError("partition_names must identify leaf partitions of table_name.")
    return resolved


def _discover(options: _Options, parent_table: str) -> list[str]:
    from ...dml.io.read_sql import read_sql

    partitions = read_sql(
        options.connection_key,
        _LEAF_PARTITIONS_SQL.format(parent_table=parent_table.replace("'", "''")),
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        query_label=options.query_label,
    )
    if not {"schema_name", "relation_name"}.issubset(partitions.columns):
        raise RuntimeError("Greenplum partition discovery returned an invalid result.")
    return [
        TableIdentifier.parse(f"{row.schema_name}.{row.relation_name}", "gp").render_quoted("gp")
        for row in partitions[["schema_name", "relation_name"]].itertuples(index=False)
    ]


def _build_plan(options: _Options, names: Sequence[str]) -> SqlPlan:
    plan = SqlPlan(
        operation="gp_analyze_partitioned_table",
        target_alias=options.connection_key,
        target_backend="gp",
        options={
            "partition_names": list(names),
            "table_names": list(options.table_names),
            "concurrency": options.concurrency,
        },
        metadata=SqlOperationMetadata(statement_count=len(names), query_label=options.query_label),
    )
    adapter = get_backend_adapter("gp")
    for name in names:
        plan.add(
            adapter.analyze_table_sql(name, query_label=options.query_label),
            alias=options.connection_key,
            backend="gp",
            phase="analyze_partition",
            target_table=name,
        )
    return plan


def _execute_all(
    options: _Options, names: Sequence[str], sqls: Sequence[str]
) -> list[SqlOperationMetadata]:
    if options.concurrency == 1:
        return [_execute_one(options, name, sql) for name, sql in zip(names, sqls)]
    completed: list[SqlOperationMetadata] = []
    next_index = 0
    futures: set[Future[SqlOperationMetadata]] = set()
    with ThreadPoolExecutor(max_workers=min(options.concurrency, len(names))) as executor:
        while next_index < len(names) and len(futures) < options.concurrency:
            futures.add(executor.submit(_execute_one, options, names[next_index], sqls[next_index]))
            next_index += 1
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                completed.append(future.result())
            while next_index < len(names) and len(futures) < options.concurrency:
                futures.add(
                    executor.submit(_execute_one, options, names[next_index], sqls[next_index])
                )
                next_index += 1
    return completed


def _execute_one(options: _Options, name: str, sql: str) -> SqlOperationMetadata:
    metadata = SqlOperationMetadata(statement_count=1, query_label=options.query_label)

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="gp_analyze_partitioned_table",
            alias=options.connection_key,
            backend="gp",
            phase="analyze_partition",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=sql,
        ):
            get_backend_adapter("gp").execute_commands(connection_ref["connection"], [sql])

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="gp_analyze_partitioned_table",
            alias=options.connection_key,
            backend="gp",
            phase="analyze_partition",
            target_table=name,
            retry_attempt=attempt,
            sql_preview=sql_preview(sql),
        )

    run_connection_operation(
        operation_name=f"analyzing partition {name} on {options.connection_key}",
        connection_key=options.connection_key,
        backend="gp",
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    return metadata
