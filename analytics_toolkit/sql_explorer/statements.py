from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import sqlglot
import sqlparse
from sqlglot import exp

from analytics_toolkit._sql_statements import (
    has_sql_content,
    join_statements,
    split_statements,
)
from analytics_toolkit.sql.backends.row_limits import apply_row_limit

from .errors import SqlExplorerConfigurationError

DISPLAY_ROW_LIMIT = 200
FETCH_ROW_LIMIT = DISPLAY_ROW_LIMIT + 1
_DIALECTS = {"gp": "postgres", "trino": "trino", "ch": "clickhouse"}
_RESULT_KEYWORDS = {"DESC", "DESCRIBE", "EXPLAIN", "SELECT", "SHOW", "TABLE", "VALUES"}
_DIRECT_RESULT_KEYWORDS = _RESULT_KEYWORDS - {"SELECT"}
_RETURNING_RE = re.compile(r"\bRETURNING\b", flags=re.IGNORECASE)
_SELECT_INTO_RE = re.compile(r"\bSELECT\b[\s\S]*?\bINTO\b", flags=re.IGNORECASE)
_EXPLAIN_MUTATION_RE = re.compile(
    r"\bANALY[ZS]E\b[\s\S]*\b(?:DELETE|INSERT|MERGE|UPDATE)\b",
    flags=re.IGNORECASE,
)


class ExecutionRoute(str, Enum):
    READ = "read"
    EXECUTE_READ = "execute_read"
    EXECUTE = "execute"


@dataclass(frozen=True)
class ExplorerExecutionPlan:
    statements: tuple[str, ...]
    execution_sql: str
    route: ExecutionRoute
    returns_rows: bool
    requires_confirmation: bool
    server_limited: bool
    user_sql: str | None = None
    source_file: str | None = None

    @property
    def statement_count(self) -> int:
        return len(self.statements)

    @property
    def full_execution_sql(self) -> str:
        return join_statements(self.statements)

    @property
    def changes_metadata(self) -> bool:
        return any(
            _first_keyword(sqlparse.format(statement, strip_comments=True))
            in {"CREATE", "ALTER", "DROP", "RENAME", "GRANT", "REVOKE"}
            or bool(_SELECT_INTO_RE.search(statement))
            for statement in self.statements
        )


def build_execution_plan(sql_text: str, backend: str) -> ExplorerExecutionPlan:
    statements = tuple(split_statements(str(sql_text)))
    if not statements:
        message = "Enter a SQL statement before running it."
        raise SqlExplorerConfigurationError(message)

    dialect = _DIALECTS.get(backend)
    final_returns_rows = _returns_rows(statements[-1], dialect)
    if final_returns_rows:
        route = ExecutionRoute.READ if len(statements) == 1 else ExecutionRoute.EXECUTE_READ
    else:
        route = ExecutionRoute.EXECUTE

    requires_confirmation = any(
        not _is_pure_result_read(statement, dialect) for statement in statements
    )
    bounded_final, server_limited = apply_row_limit(
        statements[-1],
        FETCH_ROW_LIMIT if final_returns_rows else None,
        dialect=dialect,
    )
    execution_statements = (*statements[:-1], bounded_final)
    execution_sql = join_statements(execution_statements)
    return ExplorerExecutionPlan(
        statements=statements,
        execution_sql=execution_sql,
        route=route,
        returns_rows=final_returns_rows,
        requires_confirmation=requires_confirmation,
        server_limited=server_limited,
        user_sql=str(sql_text),
    )


def _returns_rows(statement: str, dialect: str | None) -> bool:
    statement = sqlparse.format(statement, strip_comments=True)
    first_keyword = _first_keyword(statement)
    if first_keyword in _DIRECT_RESULT_KEYWORDS:
        return True
    expression = _parse_expression(statement, dialect)
    if expression is not None:
        if isinstance(expression, exp.Query):
            return expression.args.get("into") is None
        return expression.__class__.__name__ in {"Describe", "Explain", "Show", "Values"} or (
            expression.args.get("returning") is not None
        )

    parsed = sqlparse.parse(statement)
    statement_type = _sqlparse_statement_type(parsed)
    if statement_type in {"SELECT", "SHOW", "DESCRIBE"}:
        return not _SELECT_INTO_RE.search(statement)
    return bool(
        statement_type in {"INSERT", "UPDATE", "DELETE"} and _RETURNING_RE.search(statement)
    )


def _is_pure_result_read(statement: str, dialect: str | None) -> bool:
    statement = sqlparse.format(statement, strip_comments=True)
    if _RETURNING_RE.search(statement):
        return False
    first_keyword = _first_keyword(statement)
    if first_keyword == "EXPLAIN" and _EXPLAIN_MUTATION_RE.search(statement):
        return False
    if first_keyword in _DIRECT_RESULT_KEYWORDS:
        return True
    expression = _parse_expression(statement, dialect)
    if expression is not None:
        if isinstance(expression, exp.Query):
            return expression.args.get("into") is None
        if expression.__class__.__name__ in {"Describe", "Explain", "Show", "Values"}:
            return True
    parsed = sqlparse.parse(statement)
    statement_type = _sqlparse_statement_type(parsed)
    return statement_type in {"SELECT", "SHOW", "DESCRIBE"} and not _SELECT_INTO_RE.search(
        statement
    )


def _parse_expression(statement: str, dialect: str | None) -> Any | None:
    try:
        return sqlglot.parse_one(statement, read=dialect)
    except (ValueError, sqlglot.errors.ParseError):
        return None


def _first_keyword(statement: str) -> str:
    without_comments = sqlparse.format(statement, strip_comments=True).lstrip()
    match = re.match(r"([A-Za-z]+)", without_comments)
    return match.group(1).upper() if match else ""


def _has_sql_content(statement: str) -> bool:
    return has_sql_content(statement)


def _sqlparse_statement_type(parsed: tuple[Any, ...]) -> str:
    if not parsed:
        return ""
    return str(parsed[0].get_type()).upper()


__all__ = [
    "DISPLAY_ROW_LIMIT",
    "ExecutionRoute",
    "ExplorerExecutionPlan",
    "build_execution_plan",
]
