"""Compare SQL formatting without relaxing identifiers, values or call order."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
import sqlparse
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlparse import tokens

if TYPE_CHECKING:
    from collections.abc import Sequence


def _sql_tokens(sql: str, *, dialect: str) -> list[list[tuple[str, str]]]:
    try:
        parsed = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    except SqlglotError as error:
        message = f"Cannot compare invalid {dialect} SQL: {sql!r}"
        raise AssertionError(message) from error
    assert parsed, "Expected SQL statements"
    assert all(statement is not None for statement in parsed), "Expected SQL statements"
    result = []
    for statement in sqlparse.parse(sql):
        significant = [token for token in statement.flatten() if not token.is_whitespace]
        # A final terminator is optional; boundaries between statements remain significant.
        if significant and significant[-1].value == ";":
            significant.pop()
        result.append(
            [
                (
                    str(token.ttype),
                    " ".join(token.value.upper().split())
                    if token.ttype in tokens.Keyword
                    else token.value,
                )
                for token in significant
            ]
        )
    return result


def assert_sql_equal(actual: str, expected: str, *, dialect: str) -> None:
    assert _sql_tokens(actual, dialect=dialect) == _sql_tokens(expected, dialect=dialect), (
        f"SQL differs ({dialect})\nExpected: {expected}\nActual: {actual}"
    )


def assert_sql_calls(actual: Sequence[str], expected: Sequence[str], *, dialect: str) -> None:
    assert len(actual) == len(expected), f"Expected {len(expected)} SQL calls, got {len(actual)}"
    for index, (actual_sql, expected_sql) in enumerate(zip(actual, expected)):
        assert _sql_tokens(actual_sql, dialect=dialect) == _sql_tokens(
            expected_sql, dialect=dialect
        ), (
            f"SQL call {index + 1} differs ({dialect})\n"
            f"Expected: {expected_sql}\nActual: {actual_sql}"
        )


def strip_query_label(sql: str) -> str:
    """Remove only the leading toolkit label when a fake dispatches a query."""
    stripped = sql.lstrip()
    if stripped.startswith("/* analytics_toolkit query_label=") and "*/" in stripped:
        return stripped.split("*/", 1)[1].lstrip()
    return stripped
