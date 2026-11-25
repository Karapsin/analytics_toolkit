from __future__ import annotations

from functools import partial
from typing import Any

from analytics_toolkit import general
from analytics_toolkit.sql.backends.execution_outcome import ExecutionOutcome
from analytics_toolkit.sql.backends.utils import extract_row_count_or_none
from analytics_toolkit.sql.dml.io.execute_sql import (
    _iterate_statements_with_progress,
    _maybe_print_query,
    _split_sql_statements,
)
from analytics_toolkit.sql.execution.cursor_cleanup import cursor_scope
from analytics_toolkit.sql.execution.query_timing import run_timed_query


def execute_sql(
    self: Any,
    connection: Any,
    sql: str,
    options: dict[str, bool],
) -> Any:
    print_queries = options["print_queries"]
    progress = options["progress"]
    gp_break_query = options["gp_break_query"]
    gp_commit_each_statement = options["gp_commit_each_statement"]

    statement: str | None = None
    with cursor_scope(connection, context_manager=True) as cursor:
        try:
            should_commit_at_end = True
            if not gp_break_query:
                general.time_print("Executing 1 statement set", backend=self.backend)
                statement = sql
                _maybe_print_query(statement, print_queries, split_preview=False)
                run_timed_query(
                    self.backend,
                    partial(cursor.execute, statement),
                )
            else:
                statements = _split_sql_statements(sql)
                general.time_print(
                    f"Executing {len(statements)} statement(s)",
                    backend=self.backend,
                )
                for statement in _iterate_statements_with_progress(
                    statements,
                    self.backend,
                    progress=progress,
                ):
                    _maybe_print_query(statement, print_queries, split_preview=True)
                    run_timed_query(
                        self.backend,
                        partial(cursor.execute, statement),
                    )
                    if gp_commit_each_statement:
                        connection.commit()
                        should_commit_at_end = False
            if should_commit_at_end:
                connection.commit()
            return ExecutionOutcome(affected_rows=extract_row_count_or_none(cursor))
        except Exception:
            failed_query = statement if statement is not None else sql
            general.time_print(f"Failed SQL:\n{failed_query}", backend=self.backend)
            raise
