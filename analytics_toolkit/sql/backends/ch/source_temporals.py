"""Attach the actual source read policy before a naive result can be re-encoded."""

from __future__ import annotations

import datetime as dt
from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .temporal_context import effective_read_timezone


def source_read_timezone(adapter: Any, connection: Any) -> str | None:
    del adapter
    return effective_read_timezone(connection)


def normalize_source_temporal_batch(
    adapter: Any,
    connection: Any,
    batch: Any,
    column_types: dict[str, str | None],
    cache: dict[str, Any],
) -> Any:
    del adapter
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 - registry cycle
        convert_nested,
        nested_descriptor,
    )
    from analytics_toolkit.sql.backends.temporal_context import (  # noqa: PLC0415 - registry cycle
        effective_timezone,
        localize_value,
    )
    from analytics_toolkit.sql.core.type_resolution import is_timestamp_type  # noqa: PLC0415

    zone = effective_read_timezone(connection)
    previous = cache.get("source_timezone")
    if previous is not None and previous != zone:
        message = "Source timestamp read timezone changed during selected input acquisition."
        raise InvalidSqlInputError(message)
    if zone is not None:
        cache["source_timezone"] = zone

    def convert(value: Any, native: str | None) -> Any:
        if value is None or native is None:
            return value
        if is_timestamp_type(native):
            if (
                isinstance(value, dt.datetime)
                and value.tzinfo is None
                and effective_timezone(native, zone) is None
            ):
                message = "Cannot resolve the source timestamp read timezone before transfer."
                raise InvalidSqlInputError(message)
            return localize_value(value, native, zone)
        if nested_descriptor(native)[0]:
            return convert_nested(value, native, "ch", destination_timezone=zone)
        return value

    return type(batch)(
        columns=list(batch.columns),
        rows=[
            tuple(convert(value, column_types.get(name)) for name, value in zip(batch.columns, row))
            for row in batch.rows
        ],
    )
