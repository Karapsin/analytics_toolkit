from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any


def extract_row_count(executed: Any) -> int:
    row_count = _coerce_row_count(getattr(executed, "rowcount", None))
    if row_count is not None:
        return row_count

    if isinstance(executed, Mapping):
        row_count = _extract_row_count_from_mapping(executed)
        if row_count is not None:
            return row_count

    summary = getattr(executed, "summary", None)
    if isinstance(summary, Mapping):
        row_count = _extract_row_count_from_mapping(summary)
        if row_count is not None:
            return row_count

    for attribute in ("written_rows", "writtenRows", "processed_rows", "rows"):
        row_count = _coerce_row_count(getattr(executed, attribute, None))
        if row_count is not None:
            return row_count

    return 0


def user_filter(column_sql: str, current_user_sql: str, user: str | None) -> str:
    if user is None:
        return f"{column_sql} = {current_user_sql}"
    return f"{column_sql} = {sql_string_literal(user)}"


def sql_in_list(column_sql: str, values: list[str]) -> str:
    if not values:
        raise ValueError("values must not be empty.")
    joined_values = ", ".join(sql_string_literal(value) for value in values)
    return f"{column_sql} in ({joined_values})"


def sql_string_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, (date, datetime)):
        return sql_string_literal(value.isoformat())
    return sql_string_literal(value)


def _extract_row_count_from_mapping(value: Mapping[str, Any]) -> int | None:
    for key in (
        "rowcount",
        "row_count",
        "written_rows",
        "writtenRows",
        "processedRows",
        "rows",
    ):
        row_count = _coerce_row_count(value.get(key))
        if row_count is not None:
            return row_count
    return None


def _coerce_row_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        row_count = int(value)
    except (TypeError, ValueError):
        return None
    if row_count < 0:
        return None
    return row_count
