"""Keep decoded native floating NaNs distinct from SQL-null masks."""

from __future__ import annotations

from math import isnan
from typing import Any

from analytics_toolkit.sql.backends.native_scalars import NativeFloatNaN


def decoded_float_values(values: Any, nulls_map: Any = None) -> tuple[Any, ...]:
    return tuple(
        None
        if nulls_map is not None and nulls_map[index]
        else NativeFloatNaN(value)
        if isinstance(value, float) and isnan(value)
        else value
        for index, value in enumerate(values)
    )
