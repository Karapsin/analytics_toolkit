from __future__ import annotations

# ruff: noqa: EM101, PLR0913, PYI041, TID252, TRY003
from dataclasses import replace

import sqlparse

from ...backends.utils import extract_row_count
from ...connection.errors import InvalidSqlInputError
from ...core.identifiers import TableIdentifier
from ...execution.labels import apply_query_label
from ...execution.operation_runner import timed_public_sql_function
from ...execution.plans import (
    SqlOperationMetadata,
    SqlOperationResult,
    SqlPlan,
    SqlStatement,
)
from .execute_safety import ExecuteRetryPolicy, validate_execute_retry_policy
from .execute_sql import (
    _build_execute_sql_options,
    _execute_sql_options,
    _split_sql_statements,
    build_execute_sql_plan,
)


@timed_public_sql_function
def insert(
    db_key: str,
    table_name: str,
    query: str,
    *,
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    retry_policy: ExecuteRetryPolicy = "safe",
) -> int | SqlPlan | SqlOperationResult:
    """Insert one query result into an existing table by column position."""
    statements = _normalize_result_statements(query)
    if len(statements) != 1:
        raise InvalidSqlInputError(
            "sql.insert accepts exactly one result query; use sql.execute_insert "
            "when setup statements are required."
        )
    return _run_query_insert(
        operation="insert",
        db_key=db_key,
        table_name=table_name,
        statements=statements,
        print_queries=print_queries,
        gp_break_query=False,
        gp_commit_each_statement=False,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        progress=False,
        retry_policy=retry_policy,
    )


@timed_public_sql_function
def execute_insert(
    db_key: str,
    table_name: str,
    query: str,
    *,
    print_queries: bool = False,
    gp_break_query: bool = False,
    gp_commit_each_statement: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    progress: bool = False,
    retry_policy: ExecuteRetryPolicy = "safe",
) -> int | SqlPlan | SqlOperationResult:
    """Execute setup SQL and insert the final query result into an existing table."""
    return _run_query_insert(
        operation="execute_insert",
        db_key=db_key,
        table_name=table_name,
        statements=_normalize_result_statements(query),
        print_queries=print_queries,
        gp_break_query=gp_break_query,
        gp_commit_each_statement=gp_commit_each_statement,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        progress=progress,
        retry_policy=retry_policy,
    )


def _run_query_insert(
    *,
    operation: str,
    db_key: str,
    table_name: str,
    statements: list[str],
    print_queries: bool,
    gp_break_query: bool,
    gp_commit_each_statement: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    dry_run: bool,
    return_sql: bool,
    return_metadata: bool,
    progress: bool,
    retry_policy: ExecuteRetryPolicy,
) -> int | SqlPlan | SqlOperationResult:
    resolved_retry_policy = validate_execute_retry_policy(retry_policy)
    target_table = _normalize_target_table(table_name)
    final_query = statements[-1]
    _validate_result_query(final_query)
    write_statements = [
        *statements[:-1],
        f"INSERT INTO {target_table}\n{final_query}",
    ]
    if query_label is not None:
        write_statements = [apply_query_label(sql, query_label) for sql in write_statements]
    write_sql = _join_statements(write_statements)
    options = _build_execute_sql_options(
        db_key=db_key,
        query=write_sql,
        print_queries=print_queries,
        gp_break_query=gp_break_query,
        gp_commit_each_statement=gp_commit_each_statement,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=None,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=False,
        progress=progress,
        retry_policy=resolved_retry_policy,
    )
    options = replace(options, query_label=query_label)
    _validate_target_table(target_table, options.backend)
    plan = _build_query_insert_plan(
        operation=operation,
        table_name=target_table,
        setup_count=len(statements) - 1,
        execute_plan=build_execute_sql_plan(options),
    )
    if dry_run or return_sql:
        return plan

    result = _execute_sql_options(options)
    affected_rows = extract_row_count(result)
    if return_metadata:
        plan.metadata.affected_rows = affected_rows
        plan.metadata.inserted_rows = affected_rows
        return SqlOperationResult(
            rows=affected_rows,
            metadata=plan.metadata,
            plan=plan,
        )
    return affected_rows


def _build_query_insert_plan(
    *,
    operation: str,
    table_name: str,
    setup_count: int,
    execute_plan: SqlPlan,
) -> SqlPlan:
    statements = execute_plan.statements
    if setup_count and len(statements) == 1:
        statements = [
            SqlStatement(
                sql=sql,
                alias=execute_plan.target_alias,
                backend=execute_plan.target_backend,
            )
            for sql in _split_sql_statements(statements[0].sql)
        ]
    metadata = SqlOperationMetadata(
        statement_count=len(statements),
        query_label=execute_plan.metadata.query_label,
    )
    plan = SqlPlan(
        operation=operation,
        target_alias=execute_plan.target_alias,
        target_backend=execute_plan.target_backend,
        target_table=table_name,
        options=dict(execute_plan.options),
        metadata=metadata,
    )
    plan.statements = [
        replace(
            statement,
            phase="setup" if index < setup_count else "insert_target",
            target_table=table_name if index >= setup_count else None,
        )
        for index, statement in enumerate(statements)
    ]
    return plan


def _normalize_result_statements(query: str) -> list[str]:
    if not isinstance(query, str):
        raise TypeError("query must be a string.")
    statements = _split_sql_statements(query.strip())
    if not statements:
        raise InvalidSqlInputError("Query string must not be empty.")
    return statements


def _validate_result_query(query: str) -> None:
    parsed = [statement for statement in sqlparse.parse(query) if str(statement).strip()]
    if len(parsed) != 1 or parsed[0].get_type().upper() != "SELECT":  # type: ignore[no-untyped-call]
        raise InvalidSqlInputError("The final statement must be a SELECT query.")


def _normalize_target_table(table_name: str) -> str:
    if not isinstance(table_name, str):
        raise TypeError("table_name must be a string.")
    normalized = table_name.strip()
    if not normalized:
        raise InvalidSqlInputError("table_name must not be empty.")
    return normalized


def _validate_target_table(table_name: str, backend: str) -> None:
    try:
        TableIdentifier.parse(table_name, backend)
    except ValueError as exc:
        raise InvalidSqlInputError("table_name must be a valid table identifier.") from exc


def _join_statements(statements: list[str]) -> str:
    return ";\n".join(statement.rstrip(";") for statement in statements)


__all__ = ["execute_insert", "insert"]
