"""Own validated dataframe cells before destination I/O or retry can observe them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Collection


class FrameSnapshotError(ValueError):
    """An uncertifiable client payload must fail before destination mutation."""

    analytics_toolkit_sql_retry_safe = False


def _freeze_cell(value: Any) -> Any:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- exact codec imports this compatibility export.
        decode_value,
        encode_value,
    )

    try:
        return decode_value(encode_value(value))
    except (TypeError, ValueError, OverflowError, RuntimeError):
        message = "Cannot freeze an exact owned dataframe payload before destination mutation."
        raise FrameSnapshotError(message) from None


def freeze_dataframe_values(
    frame: pd.DataFrame, *, opaque_columns: Collection[Any] = ()
) -> pd.DataFrame:
    """Snapshot known cells; opaque native values await their owned assignment stage.

    Resolve inferred schema before calling this helper: the returned dataframe
    deliberately uses positional object columns to preserve its exact scalars.
    Exempted cells retain their original objects without invoking copy protocols.
    """
    opaque = set(opaque_columns)
    index = frame.index.copy(deep=True)
    columns = {}
    for position, name in enumerate(frame.columns):
        values = frame.iloc[:, position]
        copied = list(values) if name in opaque else [_freeze_cell(value) for value in values]
        columns[position] = pd.Series(copied, index=index, dtype=object)
    result = pd.DataFrame(columns, index=index)
    result.columns = frame.columns.copy(deep=True)
    return result


__all__ = ["FrameSnapshotError", "freeze_dataframe_values"]
