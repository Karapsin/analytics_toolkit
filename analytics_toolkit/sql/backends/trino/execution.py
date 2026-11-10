from __future__ import annotations

from functools import partial
from typing import Any

from analytics_toolkit import general
from analytics_toolkit.sql.backends.execution_outcome import ExecutionOutcome, close_cursor
from analytics_toolkit.sql.backends.utils import extract_row_count_or_none
from analytics_toolkit.sql.dml.io.execute_sql import (
    _iterate_statements_with_progress,
    _maybe_print_query,
    _split_sql_statements,
)
from analytics_toolkit.sql.execution.query_timing import run_timed_query


def execute_sql(
    self: Any,
    connection: Any,
    sql: str,
    options: dict[str, bool],
) -> Any:
    print_queries = options["print_queries"]
    progress = options["progress"]

    cursor = connection.cursor()
    statements = _split_sql_statements(sql)
    general.time_print(f"Executing {len(statements)} statement(s)", backend=self.backend)
    statement: str | None = None
    try:
        for statement in _iterate_statements_with_progress(
            statements,
            self.backend,
            progress=progress,
        ):
            _maybe_print_query(statement, print_queries, split_preview=True)
            run_timed_query(
                self.backend,
                partial(_execute_and_consume, cursor, statement),
            )
        return ExecutionOutcome(affected_rows=extract_row_count_or_none(cursor))
    except Exception:
        failed_query = statement if statement is not None else sql
        general.time_print(f"Failed SQL:\n{failed_query}", backend=self.backend)
        raise
    finally:
        close_cursor(cursor, backend=self.backend)


def _execute_and_consume(cursor: Any, statement: str) -> Any:
    result = cursor.execute(statement)
    fetch = getattr(cursor, "fetchall", None)
    if callable(fetch):
        fetch()
    return result
