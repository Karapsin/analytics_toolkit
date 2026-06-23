from __future__ import annotations

from typing import Any

def build_source_count_sql(
    self: Any,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> str:
    stripped = self.strip_query_semicolon(source_sql)
    return _apply_query_label(
        f"SELECT COUNT(*) FROM ({stripped}) AS source_count_probe",
        query_label,
    )


def count_source_rows(
    self: Any,
    connection: Any,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(self.build_source_count_sql(source_sql, query_label=query_label))
        row = fetch_first_row(cursor)
        return int(row[0]) if row else 0
    finally:
        cursor.close()


def source_sql_for_count_limited_read(
    self: Any,
    *,
    source_sql: str,
    expected_rows: int | None,
    enabled: bool,
) -> str:
    del self, expected_rows, enabled
    return source_sql


def disable_query_limit_for_transfer_reads(self: Any) -> bool:
    del self
    return False


def strip_query_semicolon(self: Any, query: str) -> str:
    del self
    stripped = query.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return stripped


def fetch_first_row(cursor: Any) -> Any:
    fetchone = getattr(cursor, "fetchone", None)
    if callable(fetchone):
        return fetchone()

    fetchall = getattr(cursor, "fetchall", None)
    if callable(fetchall):
        rows = fetchall()
        return rows[0] if rows else None

    if hasattr(cursor, "_rows"):
        rows = getattr(cursor, "_rows")
        return rows[0] if rows else None

    raise TypeError("Cursor must provide fetchone() or fetchall().")


def _apply_query_label(sql: str, query_label: str | None) -> str:
    if not query_label:
        return sql
    escaped_label = query_label.replace("*/", "* /")
    return f"/* analytics_toolkit query_label={escaped_label} */\n{sql}"
