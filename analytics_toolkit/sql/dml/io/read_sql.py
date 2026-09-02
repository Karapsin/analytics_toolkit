from __future__ import annotations

from typing import Any, cast

import pandas as pd
import sqlparse

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends.models import ReadColumnResult

from ...backends import get_backend_adapter
from ...connection.config import get_connection_config
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
from ...connection.protocols import DbApiConnection
from ...execution.labels import apply_query_label
from ...execution.operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult
from ...execution.query_timing import run_timed_query
from .models import ReadOutputType, ReadSqlOptions


_READ_OUTPUT_TYPES = ("df", "scalar", "list", "dict")


def _read_dbapi_query(conn: DbApiConnection, query: str) -> pd.DataFrame:
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        columns = [column[0] for column in cursor.description or []]
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=columns)
    finally:
        cursor.close()


def _read_dbapi_columns(conn: DbApiConnection, query: str) -> ReadColumnResult:
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        column_names = tuple(str(column[0]) for column in cursor.description or [])
        rows = cursor.fetchall()
        columns: tuple[list[Any], ...] = tuple([] for _ in column_names)
        for row in rows:
            for column, value in zip(columns, row):
                column.append(value)
        return ReadColumnResult(
            column_names=column_names,
            columns=columns,
            row_count=len(rows),
        )
    finally:
        cursor.close()


@timed_public_sql_function
def read_sql(
    db_key: str,
    query: str,
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    return_metadata: bool = False,
    output_type: ReadOutputType = "df",
    to_excel: str | None = None,
    to_csv: str | None = None,
) -> Any | SqlOperationResult:
    return _read_sql_impl(
        db_key=db_key,
        query=query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        return_metadata=return_metadata,
        output_type=output_type,
        to_excel=to_excel,
        to_csv=to_csv,
    )


def read_sql_with_metadata(
    db_key: str,
    query: str,
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    output_type: ReadOutputType = "df",
    to_excel: str | None = None,
    to_csv: str | None = None,
) -> SqlOperationResult:
    return _read_sql_impl(
        db_key=db_key,
        query=query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        return_metadata=True,
        output_type=output_type,
        to_excel=to_excel,
        to_csv=to_csv,
    )


def _read_sql_impl(
    db_key: str,
    query: str,
    *,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    return_metadata: bool,
    output_type: ReadOutputType,
    to_excel: str | None = None,
    to_csv: str | None = None,
) -> Any | SqlOperationResult:
    options = _build_read_sql_options(
        db_key=db_key,
        query=query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        return_metadata=return_metadata,
        output_type=output_type,
        to_excel=to_excel,
        to_csv=to_csv,
    )
    metadata = SqlOperationMetadata(
        statement_count=1,
        query_label=options.query_label,
    )

    def operation(
        connection_ref: dict[str, Any],
        attempt: int,
    ) -> pd.DataFrame | ReadColumnResult:
        with tracked_sql_operation(
            metadata=metadata,
            operation_name="read_sql",
            alias=options.connection_key,
            backend=options.backend,
            phase="read",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=options.sql,
        ):
            result = _read_backend(
                options.backend,
                connection_ref["connection"],
                options.sql,
                print_queries=options.print_queries,
                output_type=options.output_type,
            )
            row_count = _read_result_row_count(result)
            metadata.read_rows = row_count
            metadata.source_rows = row_count
            return result

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="read_sql",
            alias=options.connection_key,
            backend=options.backend,
            phase="read",
            retry_attempt=attempt,
            sql_preview=sql_preview(options.sql),
        )

    result = run_connection_operation(
        operation_name=f"reading query on {options.connection_key} ({options.backend})",
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    output = _format_read_output(result, options.output_type)
    if options.to_excel is not None:
        cast("pd.DataFrame", output).to_excel(options.to_excel, index=False)
    if options.to_csv is not None:
        cast("pd.DataFrame", output).to_csv(options.to_csv, index=False)
    if return_metadata:
        return SqlOperationResult(
            rows=metadata.read_rows,
            metadata=metadata,
            data=output,
        )
    return output


def _build_read_sql_options(
    *,
    db_key: str,
    query: str,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
    return_metadata: bool,
    output_type: ReadOutputType,
    to_excel: str | None = None,
    to_csv: str | None = None,
) -> ReadSqlOptions:
    config = get_connection_config(db_key)
    connection_key = config.connection_key
    backend = config.backend
    sql = query.strip()

    if not sql:
        raise InvalidSqlInputError("Query string must not be empty.")
    if output_type not in _READ_OUTPUT_TYPES:
        supported = ", ".join(repr(value) for value in _READ_OUTPUT_TYPES)
        message = f"Unsupported output_type {output_type!r}. Supported values: {supported}."
        raise InvalidSqlInputError(message)
    if to_excel is not None and to_csv is not None:
        message = "to_excel and to_csv cannot be used together."
        raise InvalidSqlInputError(message)
    if to_excel is not None and output_type != "df":
        message = "to_excel is only supported when output_type='df'."
        raise InvalidSqlInputError(message)
    if to_csv is not None and output_type != "df":
        message = "to_csv is only supported when output_type='df'."
        raise InvalidSqlInputError(message)
    validate_retry_options(retry_cnt, timeout_increment)

    statements = [statement.strip() for statement in sqlparse.split(sql) if statement.strip()]
    if len(statements) != 1:
        raise InvalidSqlInputError("read_sql expects exactly one SQL statement.")
    sql = apply_query_label(statements[0].rstrip(";").rstrip(), query_label)
    sql = get_backend_adapter(backend).prepare_sql(config, sql)
    return ReadSqlOptions(
        connection_key=connection_key,
        backend=backend,
        sql=sql,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        return_metadata=return_metadata,
        output_type=output_type,
        to_excel=to_excel,
        to_csv=to_csv,
    )


def _maybe_print_query(query: str, print_queries: bool) -> None:
    if print_queries:
        statements = [statement.strip() for statement in sqlparse.split(query) if statement.strip()]
        statement_to_print = statements[0] if statements else query.strip()
        time_print(f"Executing query:\n{statement_to_print}")


def _read_backend(  # noqa: PLR0913
    backend: str,
    connection: Any,
    sql: str,
    *,
    print_queries: bool,
    output_type: ReadOutputType,
    action_name: str = "SQL query",
    phase: str | None = None,
) -> pd.DataFrame | ReadColumnResult:
    adapter = get_backend_adapter(backend)
    if output_type == "dict":
        return run_timed_query(
            backend,
            lambda: adapter.read_columns(
                connection,
                sql,
                print_queries=print_queries,
                print_query=_maybe_print_query,
                read_dbapi_columns=_read_dbapi_columns,
            ),
            action_name=action_name,
            phase=phase,
        )
    return run_timed_query(
        backend,
        lambda: adapter.read_dataframe(
            connection,
            sql,
            print_queries=print_queries,
            print_query=_maybe_print_query,
            read_dbapi_query=_read_dbapi_query,
        ),
        action_name=action_name,
        phase=phase,
    )


def _read_result_row_count(result: pd.DataFrame | ReadColumnResult) -> int:
    if isinstance(result, ReadColumnResult):
        return result.row_count
    return len(result)


def _format_read_output(
    result: pd.DataFrame | ReadColumnResult,
    output_type: ReadOutputType,
) -> Any:
    if output_type == "dict":
        column_result = cast("ReadColumnResult", result)
        duplicate_names = sorted(
            {
                name
                for name in column_result.column_names
                if column_result.column_names.count(name) > 1
            }
        )
        if duplicate_names:
            message = (
                f"output_type='dict' requires unique column names; duplicates: {duplicate_names}."
            )
            raise InvalidSqlInputError(message)
        return {
            name: list(column)
            for name, column in zip(column_result.column_names, column_result.columns)
        }

    dataframe = cast("pd.DataFrame", result)
    if output_type == "scalar":
        if dataframe.shape != (1, 1):
            message = (
                "output_type='scalar' requires exactly one row and one column; "
                f"received shape {dataframe.shape}."
            )
            raise InvalidSqlInputError(message)
        return dataframe.iloc[0, 0]
    if output_type == "list":
        if dataframe.shape[1] != 1:
            message = (
                f"output_type='list' requires exactly one column; received {dataframe.shape[1]}."
            )
            raise InvalidSqlInputError(message)
        return dataframe.iloc[:, 0].tolist()
    return dataframe
