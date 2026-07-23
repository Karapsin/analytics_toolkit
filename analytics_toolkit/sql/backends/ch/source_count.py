from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one


def count_source_rows(
    self: Any,
    connection: Any,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> int:
    result = connection.query(
        self.build_source_count_sql(source_sql, query_label=query_label)
    )
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
    return f"{self.strip_query_semicolon(source_sql)}\nLIMIT {int(expected_rows)}"


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
    return isinstance(tree, exp.Select) and tree.args.get("limit") is not None
