from __future__ import annotations

import contextvars
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Iterator

import sqlparse
from tqdm import tqdm

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.execution.cancellation import (
    current_cancellation_scope,
    shutdown_executor,
)

from ...backends import get_backend_adapter
from ...connection.config import get_connection_config
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
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
from .models import ExecuteSqlOptions

_DEFAULT_HARD_CONCURRENCY_CAP = 5


@timed_public_sql_function
def execute_sql(
    db_key: str,
    query: str | list[str],
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
    concurrency: int = 1,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = _DEFAULT_HARD_CONCURRENCY_CAP,
) -> Any:
    _validate_execute_concurrency_options(
        concurrency=concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
    )
    queries, is_batch = _normalize_execute_queries(query)
    options_list = [
        _build_execute_sql_options(
            db_key=db_key,
            query=query_text,
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
        for query_text in queries
    ]

    if not is_batch:
        return _execute_sql_options(options_list[0])
    effective_concurrency = _effective_execute_concurrency(
        concurrency=concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
    )
    if dry_run or return_sql:
        return [build_execute_sql_plan(options) for options in options_list]
    return _execute_sql_batch(options_list, concurrency=effective_concurrency)


def _execute_sql_options(options: ExecuteSqlOptions) -> Any:
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


def _execute_sql_batch(
    options_list: list[ExecuteSqlOptions],
    *,
    concurrency: int,
) -> list[Any]:
    if concurrency == 1:
        return [_execute_sql_options(options) for options in options_list]

    executor = ThreadPoolExecutor(
        max_workers=min(concurrency, len(options_list)),
        thread_name_prefix="sql-execute",
    )
    cancellation_scope = current_cancellation_scope()
    if cancellation_scope is not None:
        cancellation_scope.register_executor(executor)

    executor_shutdown = False
    future_to_index: dict[Future[Any], int] = {}
    try:
        for index, options in enumerate(options_list):
            context = contextvars.copy_context()
            future = executor.submit(context.run, _execute_sql_options, options)
            future_to_index[future] = index

        results: list[Any] = [None] * len(options_list)
        try:
            for future in as_completed(future_to_index):
                results[future_to_index[future]] = future.result()
        except BaseException:
            for pending in future_to_index:
                pending.cancel()
            shutdown_executor(executor, wait=True, cancel_futures=True)
            executor_shutdown = True
            raise
        return results
    finally:
        if cancellation_scope is not None:
            cancellation_scope.unregister_executor(executor)
        if not executor_shutdown:
            shutdown_executor(executor, wait=True, cancel_futures=False)


def _normalize_execute_queries(query: str | list[str]) -> tuple[list[str], bool]:
    if isinstance(query, str):
        return [query], False
    if not isinstance(query, list):
        message = "query must be a string or a non-empty list of strings."
        raise TypeError(message)
    if not query:
        message = "Query list must not be empty."
        raise InvalidSqlInputError(message)
    for index, query_text in enumerate(query):
        if not isinstance(query_text, str):
            message = f"Query at index {index} must be a string."
            raise TypeError(message)
        if not query_text.strip():
            message = f"Query at index {index} must not be empty."
            raise InvalidSqlInputError(message)
    return list(query), True


def _validate_execute_concurrency_options(
    *,
    concurrency: int,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
) -> None:
    if concurrency.__class__ is not int or concurrency < 1:
        message = "concurrency must be an integer >= 1."
        raise ValueError(message)
    if soft_concurrency_cap is not None and (
        soft_concurrency_cap.__class__ is not int or soft_concurrency_cap < 1
    ):
        message = "soft_concurrency_cap must be an integer >= 1."
        raise ValueError(message)
    if hard_concurrency_cap.__class__ is not int or hard_concurrency_cap < 1:
        message = "hard_concurrency_cap must be an integer >= 1."
        raise ValueError(message)


def _effective_execute_concurrency(
    *,
    concurrency: int,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
) -> int:
    effective_concurrency = (
        concurrency if soft_concurrency_cap is None else min(concurrency, soft_concurrency_cap)
    )
    if effective_concurrency > hard_concurrency_cap:
        message = (
            "effective concurrency exceeds hard_concurrency_cap "
            f"({effective_concurrency} > {hard_concurrency_cap}). Reduce concurrency, "
            "set soft_concurrency_cap at or below hard_concurrency_cap, or increase "
            "hard_concurrency_cap."
        )
        raise ValueError(message)
    return effective_concurrency


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
    sql = get_backend_adapter(backend).prepare_sql(config, sql)
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
    return get_backend_adapter(backend).execute_sql(
        connection,
        sql,
        print_queries=print_queries,
        gp_break_query=gp_break_query,
        gp_commit_each_statement=gp_commit_each_statement,
        progress=progress,
    )
