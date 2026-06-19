from __future__ import annotations

from typing import Any, Callable, Iterator

import sqlparse
from tqdm import tqdm

from ...backend_adapters import get_backend_adapter
from ...backends import get_backend_names
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    sql_preview,
)
from ...connection.config import get_connection_config
from ...connection.get_sql_connection import get_sql_connection
from ...connection.protocols import ClickHouseClient, DbApiConnection
from ...execution.labels import apply_query_label
from ...execution.operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_progress_option,
    validate_retry_options,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ...execution.query_timing import run_timed_query
from analytics_toolkit.general import time_print
from .models import ExecuteSqlOptions


ExecuteBackend = Callable[[Any, str, bool, bool, bool, bool], Any]


def _execute_trino(
    conn: DbApiConnection,
    query: str,
    print_queries: bool = False,
    progress: bool = False,
) -> Any:
    return get_backend_adapter("trino").execute_sql(
        conn,
        query,
        print_queries=print_queries,
        gp_break_query=False,
        gp_commit_each_statement=False,
        progress=progress,
    )


def _execute_gp(
    conn: DbApiConnection,
    query: str,
    print_queries: bool = False,
    gp_break_query: bool = False,
    gp_commit_each_statement: bool = False,
    progress: bool = False,
) -> Any:
    return get_backend_adapter("gp").execute_sql(
        conn,
        query,
        print_queries=print_queries,
        gp_break_query=gp_break_query,
        gp_commit_each_statement=gp_commit_each_statement,
        progress=progress,
    )


def _execute_ch(
    client: ClickHouseClient,
    query: str,
    print_queries: bool = False,
    progress: bool = False,
) -> Any:
    return get_backend_adapter("ch").execute_sql(
        client,
        query,
        print_queries=print_queries,
        gp_break_query=False,
        gp_commit_each_statement=False,
        progress=progress,
    )


@timed_public_sql_function
def execute_sql(
    db_key: str,
    query: str,
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
) -> Any:
    options = _build_execute_sql_options(
        db_key=db_key,
        query=query,
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
    )

    if options.dry_run or options.return_sql:
        return build_execute_sql_plan(options)

    statements = _planned_execute_statements(options)
    metadata = SqlOperationMetadata(
        statement_count=len(statements),
        query_label=options.query_label,
    )

    def operation(connection_ref: dict[str, Any], attempt: int) -> Any:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="execute_sql",
            alias=options.connection_key,
            backend=options.backend,
            phase="execute",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=options.sql,
        ):
            result = _execute_backend(
                options.backend,
                connection_ref["connection"],
                options.sql,
                print_queries=options.print_queries,
                gp_break_query=options.gp_break_query,
                gp_commit_each_statement=options.gp_commit_each_statement,
                progress=options.progress,
            )
            metadata.affected_rows = None
            return result

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="execute_sql",
            alias=options.connection_key,
            backend=options.backend,
            phase="execute",
            retry_attempt=attempt,
            sql_preview=sql_preview(options.sql),
        )

    result = run_connection_operation(
        operation_name=f"executing SQL on {options.connection_key} ({options.backend})",
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if options.return_metadata:
        return SqlOperationResult(
            rows=None,
            metadata=metadata,
        )
    return result


def _build_execute_sql_options(
    *,
    db_key: str,
    query: str,
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
) -> ExecuteSqlOptions:
    config = get_connection_config(db_key)
    connection_key = config.connection_key
    backend = config.backend
    sql = query.strip()

    if not sql:
        raise InvalidSqlInputError("Query string must not be empty.")
    validate_retry_options(retry_cnt, timeout_increment)
    _validate_progress(progress)
    sql = apply_query_label(sql, query_label)
    return ExecuteSqlOptions(
        connection_key=connection_key,
        backend=backend,
        sql=sql,
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
    )


def build_execute_sql_plan(options: ExecuteSqlOptions) -> SqlPlan:
    statements = _planned_execute_statements(options)
    plan = SqlPlan(
        operation="execute_sql",
        target_alias=options.connection_key,
        target_backend=options.backend,
        options={
            "print_queries": options.print_queries,
            "gp_break_query": options.gp_break_query,
            "gp_commit_each_statement": options.gp_commit_each_statement,
        },
        metadata=SqlOperationMetadata(
            statement_count=len(statements),
            query_label=options.query_label,
        ),
    )
    for statement in statements:
        plan.add(
            statement,
            alias=options.connection_key,
            backend=options.backend,
            phase="execute",
        )
    return plan


def _planned_execute_statements(options: ExecuteSqlOptions) -> list[str]:
    return get_backend_adapter(options.backend).planned_execute_statements(
        options.sql,
        gp_break_query=options.gp_break_query,
    )


def _execute_ch_statement(client: ClickHouseClient, query: str) -> None:
    client.command(query)


def _execute_trino_statement(cursor: Any, query: str) -> None:
    cursor.execute(query)


def _split_sql_statements(query: str) -> list[str]:
    return [
        statement.strip().rstrip(";").rstrip()
        for statement in sqlparse.split(query)
        if statement.strip()
    ]


def _iterate_statements_with_progress(
    statements: list[str],
    connection_type: str,
    *,
    progress: bool = False,
) -> Iterator[str]:
    if len(statements) <= 1 or not progress:
        return iter(statements)

    return iter(
        tqdm(
            statements,
            desc=f"{connection_type} statements",
            unit="stmt",
        )
    )


def _maybe_print_query(query: str, print_queries: bool, split_preview: bool) -> None:
    if print_queries:
        if split_preview:
            statements = _split_sql_statements(query)
            statement_to_print = statements[0] if statements else query.strip()
        else:
            statement_to_print = query.strip()
        time_print(f"Executing query:\n{statement_to_print}")


def _validate_progress(progress: bool) -> None:
    validate_progress_option(progress)


def _execute_trino_backend(
    connection: Any,
    sql: str,
    print_queries: bool,
    gp_break_query: bool,
    gp_commit_each_statement: bool,
    progress: bool,
) -> Any:
    del gp_break_query, gp_commit_each_statement
    return _execute_trino(
        connection,
        sql,
        print_queries=print_queries,
        progress=progress,
    )


def _execute_gp_backend(
    connection: Any,
    sql: str,
    print_queries: bool,
    gp_break_query: bool,
    gp_commit_each_statement: bool,
    progress: bool,
) -> Any:
    return _execute_gp(
        connection,
        sql,
        print_queries=print_queries,
        gp_break_query=gp_break_query,
        gp_commit_each_statement=gp_commit_each_statement,
        progress=progress,
    )


def _execute_ch_backend(
    connection: Any,
    sql: str,
    print_queries: bool,
    gp_break_query: bool,
    gp_commit_each_statement: bool,
    progress: bool,
) -> Any:
    del gp_break_query, gp_commit_each_statement
    return _execute_ch(
        connection,
        sql,
        print_queries=print_queries,
        progress=progress,
    )


def _make_execute_backend(backend: str) -> ExecuteBackend:
    def execute_backend(
        connection: Any,
        sql: str,
        print_queries: bool,
        gp_break_query: bool,
        gp_commit_each_statement: bool,
        progress: bool,
    ) -> Any:
        return get_backend_adapter(backend).execute_sql(
            connection,
            sql,
            print_queries=print_queries,
            gp_break_query=gp_break_query,
            gp_commit_each_statement=gp_commit_each_statement,
            progress=progress,
        )

    return execute_backend


_EXECUTE_BACKENDS: dict[str, ExecuteBackend] = {
    backend: _make_execute_backend(backend) for backend in get_backend_names()
}
_EXECUTE_BACKENDS.update(
    {
        "trino": _execute_trino_backend,
        "gp": _execute_gp_backend,
        "ch": _execute_ch_backend,
    }
)


def _execute_backend(
    backend: str,
    connection: Any,
    sql: str,
    *,
    print_queries: bool,
    gp_break_query: bool,
    gp_commit_each_statement: bool,
    progress: bool,
) -> Any:
    execute_backend = _EXECUTE_BACKENDS.get(backend) or _make_execute_backend(backend)
    return execute_backend(
        connection,
        sql,
        print_queries,
        gp_break_query,
        gp_commit_each_statement,
        progress,
    )
