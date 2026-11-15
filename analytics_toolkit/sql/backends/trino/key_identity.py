"""Trino scalar equality leaves for the shared recursive key dispatcher."""

from __future__ import annotations

from typing import Any

from .boolean_values import boolean_value, is_boolean_type
from .date_values import date_value, is_date_type
from .ip_values import ip_value_identity, is_ip_type
from .time_values import time_key_identity
from .timestamp_map_keys import timestamp_key_identity


def scalar_key_identity(
    key: Any, native_type: str | None, *, destination_timezone: str | None = None
) -> tuple[bool, Any]:
    """Compare native casts without substituting identity tokens into payloads."""
    if is_date_type(native_type):
        return True, date_value(key)
    if is_boolean_type(native_type):
        return True, boolean_value(key)
    if is_ip_type(native_type or ""):
        return True, ip_value_identity(key)
    handled, identity = time_key_identity(key, native_type)
    if not handled:
        handled, identity = timestamp_key_identity(
            key, native_type, destination_timezone=destination_timezone
        )
    if handled:
        return True, identity
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    if parse_native_type(native_type).base.name in {"char", "character"} and isinstance(key, str):
        return True, key.rstrip(" ")
    return False, None
