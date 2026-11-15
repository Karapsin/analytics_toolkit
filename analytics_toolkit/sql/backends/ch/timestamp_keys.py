"""Compare dataframe timestamp keys using the exact ClickHouse writer units."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd


def is_timestamp_key_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- supported nested key fields.
        nested_descriptor,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    from .storage_types import logical_storage_type  # noqa: PLC0415 -- raw alias domains.

    logical = logical_storage_type(native_type)
    if parse_native_type(logical).base.name in {"datetime", "datetime64"}:
        return True
    kind, fields = nested_descriptor(logical)
    return kind in {"array", "row", "tuple"} and any(
        is_timestamp_key_type(child) for _name, child in fields
    )


def _timestamp_identity(value: Any, native_type: str, destination_timezone: str | None) -> Any:
    from analytics_toolkit.sql.backends.temporal_context import (  # noqa: PLC0415 -- frozen write policy.
        effective_timezone,
    )
    from analytics_toolkit.sql.backends.temporal_values import (  # noqa: PLC0415 -- declared native scale.
        timestamp_scale,
    )
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

    from .exact_temporal import timestamp_ticks  # noqa: PLC0415 -- same primitive as both writers.

    zone = effective_timezone(native_type, destination_timezone)
    wall = (
        value.wall_time
        if isinstance(value, ExactTemporal)
        else isinstance(value, dt.date) and getattr(value, "tzinfo", None) is None
    )
    if wall and zone is None:
        # The load preflight supplies its frozen context before this view is
        # used. Standalone callers cannot certify wall coordinates as UTC.
        return value
    if isinstance(value, np.bool_):
        value = bool(value)
    return timestamp_ticks(value, timestamp_scale(native_type), zone)


def _converted_key_identity(value: Any, native_type: str, zone: str | None) -> Any:
    from analytics_toolkit.sql.backends.map_values import (  # noqa: PLC0415 -- existing non-temporal key domains.
        _key_identity,
    )
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- exact tuple field types.
        nested_descriptor,
    )
    from analytics_toolkit.sql.backends.nested_values import (  # noqa: PLC0415 -- named/positional tuple order.
        row_items,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- explicit conversion grammar.
        parse_native_type,
    )

    from .storage_types import logical_storage_type  # noqa: PLC0415 -- raw alias domains.

    logical = logical_storage_type(native_type)
    if value is not None:
        kind, fields = nested_descriptor(logical)
        if kind == "array":
            return tuple(_converted_key_identity(item, fields[0][1], zone) for item in value)
        if kind in {"row", "tuple"}:
            return tuple(
                _converted_key_identity(item, child, zone)
                for item, (_name, child) in zip(row_items(value, fields), fields)
            )
        if parse_native_type(logical).base.name in {"datetime", "datetime64"}:
            return _timestamp_identity(value, logical, zone)
    return _key_identity(value, native_type, "ch", zone)


def dataframe_key_identity(
    value: Any, native_type: str, destination_timezone: str | None = None
) -> Any:
    """Preserve ordinary key domains and normalize supported timestamp cells."""
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- explicit conversion grammar.
        ResolvedColumn,
    )

    value = ResolvedColumn(
        native_type, target_backend="ch", destination_timezone=destination_timezone
    ).convert(value)
    return _converted_key_identity(value, native_type, destination_timezone)


def _source_key_value(value: Any) -> Any:
    if (
        isinstance(value, dt.datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    ):
        return value.astimezone(dt.timezone.utc)
    if isinstance(value, tuple):
        items = tuple(_source_key_value(item) for item in value)
        if any(item is not original for item, original in zip(items, value)):
            return items
    return value


def source_key_validation_frame(
    df: pd.DataFrame, key_columns: Sequence[str] | None
) -> pd.DataFrame:
    """Keep intrinsic aware instants distinct before authoritative schema discovery."""
    if not key_columns:
        return df
    import pandas as pd  # noqa: PLC0415 -- object identities do not coerce caller values.

    frame = df
    for name in key_columns:
        original = list(df[name])
        values = [_source_key_value(value) for value in original]
        if any(value is not before for value, before in zip(values, original)):
            if frame is df:
                frame = df.copy(deep=False)
            frame[name] = pd.Series(values, index=df.index, dtype=object)
    return frame
