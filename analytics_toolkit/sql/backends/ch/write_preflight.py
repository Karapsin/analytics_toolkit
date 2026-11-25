"""Resolve the same effective implicit timestamp policy used by the client codec."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from analytics_toolkit.sql.backends.source_schema import type_has_timezone
from analytics_toolkit.sql.execution.labels import apply_query_label

from .temporal_context import effective_write_timezone, timezone_name


def destination_write_timezone(
    adapter: Any, connection: Any, *, required: bool = False
) -> str | None:
    del adapter
    zone = effective_write_timezone(connection)
    if zone is None and required:
        query = getattr(connection, "query", None)
        if callable(query):
            result = query(apply_query_label("SELECT timezone()", None))
            rows = getattr(result, "result_rows", None)
            zone = effective_write_timezone(connection)
            if zone is None and rows and isinstance(rows[0][0], str):
                zone = timezone_name(rows[0][0])
    return zone


def requires_destination_timezone(adapter: Any, schema: dict[str, str]) -> bool:
    del adapter
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 - registry cycle
        nested_descriptor,
    )
    from analytics_toolkit.sql.core.type_resolution import (  # noqa: PLC0415 - registry cycle
        is_timestamp_type,
    )

    def implicit(value: str) -> bool:
        if is_timestamp_type(value):
            return not type_has_timezone(value.lower())
        _kind, children = nested_descriptor(value)
        return any(implicit(child) for _name, child in children)

    return any(implicit(value) for value in schema.values())


def requires_dataframe_timezone(adapter: Any, df: Any) -> bool:
    del adapter

    def naive(value: Any) -> bool:
        if isinstance(value, dt.datetime):
            return value.tzinfo is None
        if isinstance(value, Mapping):
            return any(naive(key) or naive(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(naive(item) for item in value)
        return False

    return any(naive(value) for column in range(len(df.columns)) for value in df.iloc[:, column])
