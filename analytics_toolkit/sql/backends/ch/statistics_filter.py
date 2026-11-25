"""Evaluate metadata predicates after exact logical totals have been resolved."""

from __future__ import annotations

from numbers import Integral
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import pandas as pd

from analytics_toolkit.sql.backends.metadata import normalized_metadata_query

from .dialect import sql_string_literal
from .outage_context import logical_statistics

_NUMERIC_COLUMNS = {"row_count", "table_size_bytes"}


def _literal(value: Any, *, numeric: bool) -> str:
    if value is None or pd.isna(value):
        literal = "NULL"
    elif numeric:
        if not isinstance(value, Integral) and int(value) != value:
            message = "ClickHouse table metadata contained a non-integral count estimate."
            raise ValueError(message)
        literal = str(int(value))
    else:
        literal = sql_string_literal(str(value))
    return f"CAST({literal} AS Nullable(UInt64))" if numeric else literal


def filter_resolved_statistics(
    connection_key: str,
    tables: pd.DataFrame,
    conditions: str,
    *,
    read_sql: Callable[[str, str], Any],
) -> pd.DataFrame:
    """Keep SQL predicate semantics and the original aggregation coverage evidence."""
    if tables.empty:
        return tables
    from .identifiers import quote_identifier  # noqa: PLC0415

    columns = list(tables.columns)
    rows = [
        "SELECT "
        + ", ".join(
            f"{_literal(value, numeric=column in _NUMERIC_COLUMNS)} AS {quote_identifier(column)}"
            for column, value in zip(columns, row)
        )
        for row in tables.itertuples(index=False, name=None)
    ]
    query = normalized_metadata_query("\nUNION ALL\n".join(rows), [f"({conditions})"])
    with logical_statistics():
        result = read_sql(connection_key, query)
    collected = dict(result.attrs)
    result.attrs.update(tables.attrs)
    result.attrs["data_complete"] = tables.attrs.get("data_complete", True) and collected.get(
        "data_complete", True
    )
    result.attrs["ch_cluster_status"] = [
        *tables.attrs.get("ch_cluster_status", []),
        *collected.get("ch_cluster_status", []),
    ]
    return result
