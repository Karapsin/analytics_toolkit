"""Keep raw identity checks separate from destination-native key certification."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from analytics_toolkit.sql.backends.native_values import source_key_validation_frame
from analytics_toolkit.sql.core.exact_values import dataframe_from_columns, encode_value, is_null

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pandas as pd


def validate_key_nulls(df: pd.DataFrame, key_columns: Sequence[str] | None) -> None:
    if not key_columns:
        return
    null_columns = [name for name in key_columns if any(is_null(value) for value in df[name])]
    if null_columns:
        message = "Null key values found in DataFrame for key_columns: " + ", ".join(null_columns)
        raise ValueError(message)


def validate_source_keys(
    df: pd.DataFrame,
    key_columns: list[str] | None,
    backend: str,
    validator: Callable[[pd.DataFrame, list[str] | None], None],
) -> None:
    """Reject identical raw inputs early without conflating distinct clock representations."""
    if not key_columns:
        return
    validate_key_nulls(source_key_validation_frame(df, key_columns, backend), key_columns)
    try:
        columns = [
            [
                json.dumps(encode_value(value), sort_keys=True, separators=(",", ":"))
                for value in df[name]
            ]
            for name in key_columns
        ]
    except (TypeError, ValueError):
        # Opaque native values require the actual assignment checkpoint. This
        # optional early check cannot certify their converted equality.
        return
    validator(
        dataframe_from_columns(columns, key_columns, index=df.index, object_dtype=True),
        key_columns,
    )
