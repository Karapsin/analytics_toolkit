from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from analytics_toolkit._sql_statements import terminal_parts
from analytics_toolkit.sql.backends.row_limits import apply_row_limit


def count_source_rows(
    self: Any,
    connection: Any,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> int:
    result = connection.query(self.build_source_count_sql(source_sql, query_label=query_label))
    rows = getattr(result, "result_rows", None) or []
    return int(rows[0][0]) if rows else 0


def source_sql_for_count_limited_read(
    self: Any,
    *,
    source_sql: str,
    expected_rows: int | None,
    enabled: bool,
) -> str:
    if (
        not enabled
        or expected_rows is None
        or expected_rows <= 0
        or _has_outer_limit(self, source_sql)
    ):
        return source_sql
    body, suffix, _terminated = terminal_parts(source_sql)
    try:
        tree = parse_one(body, read=self.sqlglot_dialect)
    except SqlglotError:
        return source_sql
    source_sql = body
    if isinstance(tree, exp.SetOperation):
        # A CH trailing LIMIT belongs to the last UNION branch. Keep branch
        # limits/order/offset intact and cap the complete result outside it.
        source_sql = f"SELECT * FROM (\n{body}\n) AS _atk_counted_source"  # noqa: S608 - existing SQL is a parsed query
    limited, _ = apply_row_limit(source_sql, int(expected_rows), dialect=self.sqlglot_dialect)
    return limited.rstrip() + suffix


def disable_query_limit_for_transfer_reads(self: Any) -> bool:
    del self
    return True


def _has_outer_limit(self: Any, source_sql: str) -> bool:
    try:
        tree = parse_one(
            self.strip_query_semicolon(source_sql),
            read=self.sqlglot_dialect,
        )
    except Exception:
        return False
    return (
        isinstance(tree, (exp.Select, exp.SetOperation, exp.Values, exp.Subquery))
        and tree.args.get("limit") is not None
    )
