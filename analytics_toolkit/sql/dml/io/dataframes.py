from __future__ import annotations

from numbers import Integral
from typing import TYPE_CHECKING, Any

import pandas as pd

from analytics_toolkit.sql.backends.models import ReadColumnResult

if TYPE_CHECKING:
    from collections.abc import Sequence

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_UINT64_MAX = 2**64 - 1


def column_result_from_rows(
    column_names: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> ReadColumnResult:
    """Preserve DBAPI scalars in positional, duplicate-name-safe columns."""
    names = tuple(str(name) for name in column_names)
    columns: tuple[list[Any], ...] = tuple([] for _ in names)
    for row in rows:
        for column, value in zip(columns, row):
            column.append(value)
    return ReadColumnResult(
        column_names=names,
        columns=columns,
        row_count=len(rows),
    )


def dataframe_from_column_result(result: ReadColumnResult) -> pd.DataFrame:
    """Build a dataframe without routing nullable integers through float64."""
    if not result.column_names:
        return pd.DataFrame(index=pd.RangeIndex(result.row_count))
    arrays = {index: _modern_array(column) for index, column in enumerate(result.columns)}
    dataframe = pd.DataFrame(arrays)
    dataframe.columns = list(result.column_names)
    return dataframe


def dataframe_from_columns(
    column_names: Sequence[str],
    columns: Sequence[Sequence[Any]],
    *,
    row_count: int,
) -> pd.DataFrame:
    result = ReadColumnResult(
        column_names=tuple(str(name) for name in column_names),
        columns=tuple(list(column) for column in columns),
        row_count=row_count,
    )
    return dataframe_from_column_result(result)


def _modern_array(values: Sequence[Any]) -> Any:
    if not values:
        return pd.array([], dtype=object)

    non_missing = [
        value
        for value in values
        if value is not None and value is not pd.NA and value is not pd.NaT
    ]
    if non_missing and all(
        isinstance(value, Integral) and not isinstance(value, bool) for value in non_missing
    ):
        integers = [int(value) for value in non_missing]
        minimum = min(integers)
        maximum = max(integers)
        if minimum >= _INT64_MIN and maximum <= _INT64_MAX:
            return pd.array(values, dtype="Int64")
        if minimum >= 0 and maximum <= _UINT64_MAX:
            return pd.array(values, dtype="UInt64")
        return pd.array(values, dtype=object)

    try:
        return pd.array(values)
    except (OverflowError, TypeError, ValueError):
        return pd.array(values, dtype=object)


__all__ = [
    "column_result_from_rows",
    "dataframe_from_column_result",
    "dataframe_from_columns",
]
