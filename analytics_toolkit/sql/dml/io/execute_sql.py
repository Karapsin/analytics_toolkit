from __future__ import annotations

# ruff: noqa: BLE001, C901, PLR0912, TRY300
import contextvars
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from typing import Any, Iterator
from uuid import uuid4

import sqlparse
from tqdm import tqdm

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.dml.io.execute_safety import (
    AmbiguousSqlMutationError,
    ExecuteAttemptState,
    ExecuteRetryPolicy,
    SqlBatchExecutionError,
    SqlBatchItemResult,
    TrackingConnection,
    is_read_only_sql,
    validate_execute_retry_policy,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    is_non_retryable_sql_error,
    run_with_retry,
)
from analytics_toolkit.sql.execution.cancellation import (
    current_cancellation_scope,
    shutdown_executor,
)

from ...backends import get_backend_adapter
from ...connection.config import get_connection_config
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    annotate_sql_exception,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
from ...execution.labels import apply_query_label
from ...execution.operation_runner import (
    timed_public_sql_function,
    tracked_sql_operation,
    validate_progress_option,
    validate_retry_options,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
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
    retry_policy: ExecuteRetryPolicy = "safe",
) -> Any:
    _validate_execute_concurrency_options(
        concurrency=concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
    )
    queries, is_batch = _normalize_execute_queries(query)
    resolved_retry_policy = validate_execute_retry_policy(retry_policy)
    batch_id = uuid4().hex if is_batch else None
    options_list = [
        _build_execute_sql_options(
            db_key=db_key,
            query=query_text,
            print_queries=print_queries,
            gp_break_query=gp_break_query,
            gp_commit_each_statement=gp_commit_each_statement,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            retry_policy=resolved_retry_policy,
            query_label=query_label,
            dry_run=dry_run,
            return_sql=return_sql,
            return_metadata=return_metadata,
            progress=progress,
            batch_id=batch_id,
            batch_index=index if is_batch else None,
        )
        for index, query_text in enumerate(queries)
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

    execution_sql = _execution_sql(options)
    read_only = is_read_only_sql(execution_sql)
    adapter = get_backend_adapter(options.backend)

    def operation(attempt: int) -> Any:
        options.attempt_numbers.append(attempt)
        connection: Any | None = None
        state = ExecuteAttemptState()
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
            try:
                connection = get_sql_connection(options.connection_key)
                execution_connection = connection
                if not read_only and bool(getattr(adapter, "supports_transactions", False)):
                    execution_connection = TrackingConnection(connection, state)
                elif not read_only:
                    state.submitted = True
                result = _execute_backend(
                    options.backend,
                    execution_connection,
                    execution_sql,
                    print_queries=options.print_queries,
                    gp_break_query=options.gp_break_query,
                    gp_commit_each_statement=options.gp_commit_each_statement,
                    progress=options.progress,
                )
                metadata.affected_rows = None
                return result
            except Exception as exc:
                current_context = context(attempt)
                annotate_sql_exception(exc, current_context)
                if connection is None:
                    raise
                if read_only or options.retry_policy == "always":
                    adapter.rollback_quietly(connection)
                    raise
                if is_non_retryable_sql_error(exc):
                    adapter.rollback_quietly(connection)
                    raise
                if (
                    bool(getattr(adapter, "supports_transactions", False))
                    and not state.commit_started
                    and not state.committed
                    and _rollback_confirmed(connection)
                ):
                    raise
                ambiguous = AmbiguousSqlMutationError(
                    "Mutating SQL may have completed before the connection failed; "
                    "the toolkit will not replay it automatically.",
                    context=current_context,
                    original_error=exc,
                )
                raise ambiguous from exc
            finally:
                if connection is not None:
                    _close_execute_connection(connection, options)

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="execute_sql",
            alias=options.connection_key,
            backend=options.backend,
            phase="execute",
            retry_attempt=attempt,
            sql_preview=sql_preview(options.sql),
        )

    result = run_with_retry(
        operation_name=f"executing SQL on {options.connection_key} ({options.backend})",
        retry_cnt=(1 if options.retry_policy == "never" else options.retry_cnt),
        timeout_increment=options.timeout_increment,
        operation=operation,
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
        return _execute_sql_batch_sequential(options_list)

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
        outcomes: dict[int, SqlBatchItemResult] = {}
        first_error: BaseException | None = None
        try:
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                options = options_list[index]
                try:
                    value = future.result()
                except CancelledError as exc:
                    outcomes[index] = _batch_cancelled_item(index, options, exc)
                except Exception as exc:
                    outcomes[index] = _batch_failed_item(index, options, exc)
                    if first_error is None:
                        first_error = exc
                        for pending in future_to_index:
                            if pending is not future and not pending.done():
                                pending.cancel()
                else:
                    results[index] = value
                    outcomes[index] = _batch_success_item(index, options, value)
        except BaseException:
            for pending in future_to_index:
                pending.cancel()
            shutdown_executor(executor, wait=True, cancel_futures=True)
            executor_shutdown = True
            raise
        if first_error is not None:
            batch_error = SqlBatchExecutionError(
                options_list[0].batch_id or "unknown",
                [outcomes[index] for index in range(len(options_list))],
            )
            raise batch_error from first_error
        return results
    finally:
        if cancellation_scope is not None:
            cancellation_scope.unregister_executor(executor)
        if not executor_shutdown:
            shutdown_executor(executor, wait=True, cancel_futures=False)


def _execute_sql_batch_sequential(options_list: list[ExecuteSqlOptions]) -> list[Any]:
    results: list[Any] = []
    outcomes: list[SqlBatchItemResult] = []
    for index, options in enumerate(options_list):
        try:
            value = _execute_sql_options(options)
        except Exception as exc:
            outcomes.append(_batch_failed_item(index, options, exc))
            outcomes.extend(
                _batch_cancelled_item(
                    pending_index,
                    pending,
                    CancelledError("query was not started after an earlier batch failure"),
                )
                for pending_index, pending in enumerate(
                    options_list[index + 1 :],
                    start=index + 1,
                )
            )
            batch_error = SqlBatchExecutionError(
                options.batch_id or "unknown",
                outcomes,
            )
            raise batch_error from exc
        results.append(value)
        outcomes.append(_batch_success_item(index, options, value))
    return results


def _batch_success_item(
    index: int,
    options: ExecuteSqlOptions,
    value: Any,
) -> SqlBatchItemResult:
    return SqlBatchItemResult(
        index=index,
        query=options.source_sql or options.sql,
        status="success",
        attempts=len(options.attempt_numbers),
        result=value,
    )


def _batch_failed_item(
    index: int,
    options: ExecuteSqlOptions,
    error: BaseException,
) -> SqlBatchItemResult:
    return SqlBatchItemResult(
        index=index,
        query=options.source_sql or options.sql,
        status=("ambiguous" if isinstance(error, AmbiguousSqlMutationError) else "failed"),
        attempts=len(options.attempt_numbers),
        error=error,
    )


def _batch_cancelled_item(
    index: int,
    options: ExecuteSqlOptions,
    error: BaseException,
) -> SqlBatchItemResult:
    return SqlBatchItemResult(
        index=index,
        query=options.source_sql or options.sql,
        status="cancelled",
        attempts=len(options.attempt_numbers),
        error=error,
    )


def _execution_sql(options: ExecuteSqlOptions) -> str:
    if options.batch_id is None or options.batch_index is None:
        return options.sql
    return apply_query_label(
        options.sql,
        f"batch={options.batch_id} item={options.batch_index}",
    )


def _rollback_confirmed(connection: Any) -> bool:
    try:
        connection.rollback()
    except Exception:
        return False
    return True


def _close_execute_connection(connection: Any, options: ExecuteSqlOptions) -> None:
    time_print(
        "Closing connection",
        connection=options.connection_key,
        backend=options.backend,
        phase="close",
    )
    try:
        connection.close()
    except Exception as exc:  # query outcome is already known; do not replay it for close failure.
        time_print(
            f"Connection close failed after SQL outcome was established: {type(exc).__name__}",
            level="warning",
            connection=options.connection_key,
            backend=options.backend,
            phase="close",
        )


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
    retry_policy: ExecuteRetryPolicy = "safe",
    batch_id: str | None = None,
    batch_index: int | None = None,
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
        retry_policy=retry_policy,
        source_sql=query.strip(),
        batch_id=batch_id,
        batch_index=batch_index,
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
            "retry_policy": options.retry_policy,
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
