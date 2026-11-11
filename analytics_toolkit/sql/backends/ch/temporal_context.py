"""Read-only effective timestamp policy shared by codecs and preflight validation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta, tzinfo
from typing import Any

_MISSING = object()


def canonical_timezone(timezone: Any) -> Any:
    """Resolve named policies identically across transports; keep explicit fixed offsets."""
    from analytics_toolkit.sql.backends.temporal_context import (  # noqa: PLC0415
        canonical_named_zone,
    )

    if timezone is None:
        return None
    name = (
        timezone
        if isinstance(timezone, str)
        else (getattr(timezone, "key", None) or getattr(timezone, "zone", None))
    )
    return canonical_named_zone(name) if isinstance(name, str) and name else timezone


def _fixed_offset_name(offset: Any) -> str | None:
    if not isinstance(offset, timedelta):
        return None
    seconds = int(offset.total_seconds())
    if seconds != offset.total_seconds():
        return None
    if seconds == 0:
        return "UTC"
    hours, remainder = divmod(abs(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    suffix = f":{seconds:02d}" if seconds else ""
    return f"{'-' if offset.total_seconds() < 0 else '+'}{hours:02d}:{minutes:02d}{suffix}"


def timezone_name(timezone: Any) -> str | None:
    """Return an IANA name or an unambiguous fixed ISO offset, without host guessing."""
    if isinstance(timezone, str):
        return timezone or None
    if not isinstance(timezone, tzinfo):
        return None
    name = getattr(timezone, "key", None) or getattr(timezone, "zone", None)
    if isinstance(name, str) and name:
        return name
    try:
        return _fixed_offset_name(timezone.utcoffset(None))
    except (NotImplementedError, TypeError, ValueError):
        return None


def native_context_timezone(context: Any) -> Any:
    """Use the native driver's selected server/client policy; unresolved stays unknown."""
    settings = getattr(context, "settings", None)
    if isinstance(settings, Mapping) and settings.get("use_client_time_zone", False):
        from clickhouse_driver.util.compat import get_localzone_name_compat  # noqa: PLC0415

        return get_localzone_name_compat()
    server_info = getattr(context, "server_info", None)
    getter = getattr(server_info, "get_timezone", None)
    return getter() if callable(getter) else None


def effective_write_timezone(connection: Any) -> str | None:
    """Expose client-local write policy without opening a connection or issuing SQL."""
    policy = getattr(connection, "effective_write_timezone", _MISSING)
    if policy is not _MISSING:
        return timezone_name(policy)
    server_timezone = getattr(connection, "server_tz", None)
    if server_timezone is not None:
        return timezone_name(server_timezone)
    native = getattr(connection, "connection", None)
    return timezone_name(native_context_timezone(getattr(native, "context", None)))


def effective_read_timezone(connection: Any) -> str | None:
    """Return the actual parsed query's naive-result policy, without issuing SQL."""
    policy = getattr(connection, "effective_read_timezone", _MISSING)
    if policy is not _MISSING:
        return timezone_name(policy)
    native = getattr(connection, "connection", None)
    context = getattr(native, "context", None)
    if getattr(context, "_atk_exact_source_values", False):
        return "UTC"
    return timezone_name(native_context_timezone(context))
