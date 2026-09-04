from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import sqlglot
import sqlparse
from sqlglot import exp

from .errors import SqlExplorerConfigurationError

DISPLAY_ROW_LIMIT = 200
FETCH_ROW_LIMIT = DISPLAY_ROW_LIMIT + 1
_DIALECTS = {"gp": "postgres", "trino": "trino", "ch": "clickhouse"}
_RESULT_KEYWORDS = {"DESC", "DESCRIBE", "EXPLAIN", "SELECT", "SHOW", "TABLE", "VALUES"}
_DIRECT_RESULT_KEYWORDS = _RESULT_KEYWORDS - {"SELECT"}
_RETURNING_RE = re.compile(r"\bRETURNING\b", flags=re.IGNORECASE)
_SELECT_INTO_RE = re.compile(r"\bSELECT\b[\s\S]*?\bINTO\b", flags=re.IGNORECASE)
_CLICKHOUSE_FORMAT_RE = re.compile(r"\bFORMAT\s+[A-Za-z0-9_]+\s*\Z", flags=re.IGNORECASE)
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

    @property
    def statement_count(self) -> int:
        return len(self.statements)

    @property
    def full_execution_sql(self) -> str:
        return ";\n".join(self.statements)


def build_execution_plan(sql_text: str, backend: str) -> ExplorerExecutionPlan:
    statements = tuple(
        _strip_terminal_semicolon(statement)
        for statement in sqlparse.split(str(sql_text))
        if _has_sql_content(statement)
    )
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
    bounded_final, server_limited = _bounded_result_statement(
        statements[-1],
        dialect,
        returns_rows=final_returns_rows,
    )
    execution_statements = (*statements[:-1], bounded_final)
    execution_sql = ";\n".join(execution_statements)
    return ExplorerExecutionPlan(
        statements=statements,
        execution_sql=execution_sql,
        route=route,
        returns_rows=final_returns_rows,
        requires_confirmation=requires_confirmation,
        server_limited=server_limited,
    )


def _returns_rows(statement: str, dialect: str | None) -> bool:
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


def _bounded_result_statement(
    statement: str,
    dialect: str | None,
    *,
    returns_rows: bool,
) -> tuple[str, bool]:
    if not returns_rows or not _is_wrappable_query(statement, dialect):
        return statement, False
    stripped = statement.rstrip().rstrip(";").rstrip()
    return (
        "SELECT * FROM ("  # noqa: S608 -- bounded wrapper around user-authored SQL.
        f"{stripped}"
        ") AS analytics_toolkit_explorer_result\n"
        f"LIMIT {FETCH_ROW_LIMIT}",
        True,
    )


def _is_wrappable_query(statement: str, dialect: str | None) -> bool:
    if _CLICKHOUSE_FORMAT_RE.search(statement.rstrip().rstrip(";")):
        return False
    if _first_keyword(statement) not in {"SELECT", "VALUES", "WITH"}:
        return False
    expression = _parse_expression(statement, dialect)
    if expression is not None:
        return isinstance(expression, exp.Query) or expression.__class__.__name__ == "Values"
    parsed = sqlparse.parse(statement)
    statement_type = _sqlparse_statement_type(parsed)
    return statement_type == "SELECT" or _first_keyword(statement) == "VALUES"


def _parse_expression(statement: str, dialect: str | None) -> Any | None:
    try:
        return sqlglot.parse_one(statement, read=dialect)
    except (ValueError, sqlglot.errors.ParseError):
        return None


def _first_keyword(statement: str) -> str:
    without_comments = sqlparse.format(statement, strip_comments=True).lstrip()
    match = re.match(r"([A-Za-z]+)", without_comments)
    return match.group(1).upper() if match else ""


def _strip_terminal_semicolon(statement: str) -> str:
    stripped = statement.strip()
    return stripped[:-1].rstrip() if stripped.endswith(";") else stripped


def _has_sql_content(statement: str) -> bool:
    without_comments = sqlparse.format(statement, strip_comments=True)
    return bool(without_comments.strip().strip(";"))


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
