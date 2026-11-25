"""Exact Trino timestamp map keys after the actual parameter conversion."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from analytics_toolkit.sql.core.exact_values import (
    MAX_EXACT_SCALE,
    ExactTemporal,
    is_null,
    unwrap_enum,
)

_MAX_PRECISION = 12
_MICROSECOND_PRECISION = 6
_EPOCH_DATE = dt.date(1970, 1, 1)
_TEMPORAL_VALUES = (dt.date, pd.Timestamp, np.datetime64, ExactTemporal)


@dataclass(frozen=True)
class _TimestampType:
    precision: int
    with_timezone: bool


@lru_cache(maxsize=1024)
def _timestamp_type(native_type: str | None) -> _TimestampType | None:
    from analytics_toolkit.sql.backends.type_resolution import parse_native_type  # noqa: PLC0415

    native_type = native_type or ""
    family = parse_native_type(native_type).base.name
    if family not in {"timestamp", "timestamp with time zone", "timestamp without time zone"}:
        return None
    match = re.fullmatch(
        r"timestamp\s*(?:\(\s*(\d+)\s*\))?(?:\s+(with|without)\s+time\s+zone)?",
        native_type.strip(),
        flags=re.IGNORECASE,
    )
    precision = int(match[1]) if match and match[1] is not None else 3
    if match is None or not 0 <= precision <= _MAX_PRECISION:
        message = "A native Trino TIMESTAMP precision between 0 and 12 is required."
        raise ValueError(message)
    return _TimestampType(precision, (match[2] or "").lower() == "with")


def contains_timestamp_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import nested_descriptor  # noqa: PLC0415
    from analytics_toolkit.sql.backends.type_resolution import parse_native_type  # noqa: PLC0415

    if _timestamp_type(native_type) is not None:
        return True
    kind, fields = nested_descriptor(native_type)
    if kind == "tuple":
        # The CH Tuple parser renames TIMESTAMP to DateTime64. Inspect its raw
        # fields with Trino's ROW grammar before rejecting this container alias.
        arguments = parse_native_type(native_type).arguments
        _kind, fields = nested_descriptor("ROW(" + ", ".join(arguments) + ")")
    children = [contains_timestamp_type(child) for _name, child in fields]
    if kind in {"gp_array", "tuple"} and any(children):
        message = "Trino TIMESTAMP containers require native ARRAY, MAP or ROW syntax."
        raise ValueError(message)
    return any(children)


def _parameter_text(value: Any, native_type: str, destination_timezone: str | None) -> str:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415
        ResolvedColumn,
        timestamp_scale,
    )

    from .parameters import timestamp_text  # noqa: PLC0415 -- backend registry cycle.

    value = unwrap_enum(value)
    if is_null(value):
        message = "Trino TIMESTAMP map keys cannot be NULL."
        raise ValueError(message)
    if not isinstance(value, _TEMPORAL_VALUES):
        message = "Trino TIMESTAMP map keys require native temporal values."
        raise TypeError(message)
    if isinstance(value, ExactTemporal) and not 0 <= value.scale <= MAX_EXACT_SCALE:
        message = "Trino timestamp map keys exceed the supported source fractional precision."
        raise ValueError(message)
    value = ResolvedColumn(
        native_type, target_backend="trino", destination_timezone=destination_timezone
    ).convert(value)
    if (
        isinstance(value, (dt.datetime, pd.Timestamp))
        and value.tzinfo is not None
        and value.utcoffset() is None
    ):
        message = "Trino TIMESTAMP map keys require a resolved UTC offset."
        raise ValueError(message)
    return timestamp_text(value, timestamp_scale(native_type))


def _calendar_ticks(parameter: str) -> tuple[int, int, bool]:
    aware = parameter.endswith(" UTC")
    calendar_text = parameter[:-4] if aware else parameter
    calendar_text, _dot, fraction = calendar_text.partition(".")
    calendar = dt.datetime.fromisoformat(calendar_text)
    seconds = (
        (calendar.date() - _EPOCH_DATE).days * 86400
        + calendar.hour * 3600
        + calendar.minute * 60
        + calendar.second
    )
    scale = len(fraction)
    return seconds * 10**scale + int(fraction or "0"), scale, aware


def _native_ticks(ticks: int, scale: int, precision: int) -> int:
    if scale <= precision:
        factor: int = 10 ** (precision - scale)
        return ticks * factor
    # Bare TIMESTAMP is natively precision 3 although the historical writer
    # formats six places. Explicit precisions were already truncated by it.
    unit: int = 10 ** (scale - precision)
    return (ticks + unit // 2) // unit


def _key_payload(ticks: int, spec: _TimestampType, *, utc_carrier: bool) -> Any:
    # Plain TIMESTAMP's UTC carrier is a stable calendar coordinate, not a
    # claimed source timezone. Its CAST keeps the same fields and prevents a
    # known policy from localizing an already converted calendar a second time.
    zone = "UTC" if utc_carrier else None
    if spec.precision <= _MICROSECOND_PRECISION:
        return ExactTemporal(ticks, spec.precision, zone).calendar_datetime(utc=True)
    scale = spec.precision
    while scale and ticks % 10 == 0:
        ticks //= 10
        scale -= 1
    return ExactTemporal(ticks, scale, zone, wall_time=not utc_carrier)


def timestamp_key_value_and_identity(
    value: Any, native_type: str | None, *, destination_timezone: str | None = None
) -> tuple[bool, Any, int | None]:
    """Return a native temporal carrier and exact destination-precision identity."""
    spec = _timestamp_type(native_type)
    if spec is None:
        return False, None, None
    parameter = _parameter_text(value, native_type or "", destination_timezone)
    ticks, scale, aware = _calendar_ticks(parameter)
    if spec.with_timezone and not aware:
        message = (
            "Cannot certify naive Trino TIMESTAMP WITH TIME ZONE map keys. "
            "Supply an aware instant or apply a destination timezone policy first."
        )
        raise ValueError(message)
    ticks = _native_ticks(ticks, scale, spec.precision)
    utc_carrier = aware or destination_timezone is not None
    return True, _key_payload(ticks, spec, utc_carrier=utc_carrier), ticks


def timestamp_key_identity(
    value: Any, native_type: str | None, *, destination_timezone: str | None = None
) -> tuple[bool, int | None]:
    handled, _payload, identity = timestamp_key_value_and_identity(
        value, native_type, destination_timezone=destination_timezone
    )
    return handled, identity
