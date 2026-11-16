"""Exact native parameter values for Trino's ARRAY/MAP/ROW driver contract."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from numbers import Integral
from typing import Any, cast
from uuid import UUID

import numpy as np
import pandas as pd

from .date_values import date_value
from .integer_values import convert_integer_value

_MICROSECOND_PRECISION = 6
_NANOSECOND_PRECISION = 9
_TIME_FAMILIES = {"time", "time with time zone", "time without time zone"}


class _FixedDecimal(Decimal):
    """Keep native Decimal values valid for SDKs using str for DECIMAL literals."""

    def __str__(self) -> str:
        return format(self, "f")


def _fixed_decimal_parameters(value: Any) -> Any:
    if isinstance(value, Decimal):
        return (
            value
            if isinstance(value, _FixedDecimal) or str(value) == format(value, "f")
            else _FixedDecimal(value)
        )
    if isinstance(value, (list, tuple)):
        converted = [_fixed_decimal_parameters(item) for item in value]
        if all(item is original for item, original in zip(converted, value)):
            return value
        return tuple(converted) if isinstance(value, tuple) else converted
    if isinstance(value, dict):
        pairs = [
            (_fixed_decimal_parameters(key), _fixed_decimal_parameters(item))
            for key, item in value.items()
        ]
        if all(
            key is original_key and item is original_item
            for (key, item), (original_key, original_item) in zip(pairs, value.items())
        ):
            return value
        return dict(pairs)
    return value


@lru_cache(maxsize=1)
def _policies() -> Any:
    # The registry imports insertion helpers before core.capabilities exists.
    from analytics_toolkit.sql.backends import nested_types, type_resolution  # noqa: PLC0415
    from analytics_toolkit.sql.core import exact_values  # noqa: PLC0415

    return nested_types, type_resolution, exact_values


def is_null(value: Any) -> bool:
    return bool(_policies()[2].is_null(value))


def _validate(value: Any, native: str) -> None:
    nested, types, _exact = _policies()
    profile = types.ColumnProfile(native_type=native or None, source_backend="trino")
    profile.add(value)
    if native:
        nested.validate_nested(profile, native, "trino")
    elif profile.map_duplicate_keys or profile.map_null_keys or not profile.map_comparable_keys:
        message = "Trino map parameters require distinct, non-NULL, comparable keys."
        raise ValueError(message)


def normalize_value(value: Any, target_type: str | None) -> Any:
    nested, _types, _exact = _policies()
    kind, _children = nested.nested_descriptor(target_type or "")
    if not is_null(value) and (
        kind
        or (
            not target_type and isinstance(value, (dict, _exact.OrderedMap, _exact.OrderedMapPairs))
        )
    ):
        _validate(value, (target_type or "") if kind else "")
    return _normalize(value, target_type)


def _normalize(value: Any, target_type: str | None) -> Any:  # noqa: PLR0911 -- driver value families.
    nested, _types, exact = _policies()
    if is_null(value):
        return None
    kind, children = nested.nested_descriptor(target_type or "")
    if kind in {"array", "gp_array"}:
        return [_normalize(item, children[0][1]) for item in value]
    if kind in {"row", "tuple"}:
        items = [value[name] for name, _native in children] if isinstance(value, dict) else value
        return tuple(_normalize(item, native) for item, (_name, native) in zip(items, children))
    if kind == "map":
        pairs = [
            (_normalize(key, children[0][1]), _normalize(item, children[1][1]))
            for key, item in value.items()
        ]
        _validate(exact.OrderedMapPairs(tuple(pairs)), "")
        return dict(pairs)
    if isinstance(value, (dict, exact.OrderedMap, exact.OrderedMapPairs)) and not target_type:
        _validate(value, "")
        pairs = [(_normalize(key, None), _normalize(item, None)) for key, item in value.items()]
        _validate(exact.OrderedMapPairs(tuple(pairs)), "")
        return dict(pairs)
    if isinstance(value, list) and not target_type:
        return [_normalize(item, None) for item in value]
    if isinstance(value, tuple) and not target_type:
        return tuple(_normalize(item, None) for item in value)
    return _scalar(value, target_type)


def _scalar(value: Any, target_type: str | None) -> Any:
    _nested, types, exact = _policies()
    family = types.parse_native_type(target_type).base.name
    if family in {"varchar", "char", "string", "text"}:
        return _text_parameter(value, target_type)
    if isinstance(
        value, (dt.datetime, pd.Timestamp, np.datetime64, exact.ExactTemporal, exact.ExactClock)
    ):
        return _temporal_parameter(value, target_type, family)
    _, value = convert_integer_value(value, target_type or "")
    if target_type:
        value = types.ResolvedColumn(target_type, target_backend="trino").convert(value)
    if isinstance(value, (dt.time, exact.ExactClock)) and family in _TIME_FAMILIES:
        return _clock_parameter(value, target_type)
    return _primitive_parameter(value, target_type)


def _primitive_parameter(value: Any, target_type: str | None) -> Any:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, (np.float32, np.float64)):
        return float(value)
    if isinstance(value, np.ndarray):
        return _normalize(value.tolist(), target_type)
    return _fixed_decimal_parameters(value)


def _text_parameter(value: Any, target_type: str | None) -> str:
    _nested, types, exact = _policies()
    if isinstance(value, exact.ExactClock):
        return cast("str", types.ResolvedColumn(target_type, target_backend="trino").convert(value))
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, (dict, list))
        else str(value)
    )


def _clock_parameter(value: Any, target_type: str | None) -> Any:
    from .time_values import (  # noqa: PLC0415
        convert_time_value,
        is_time_type,
        time_parameter_value,
    )

    _nested, types, exact = _policies()
    if target_type and not is_time_type(target_type):
        return types.ResolvedColumn(target_type, target_backend="trino").convert(value)
    if target_type is None:
        ordinary = value.time()
        if isinstance(ordinary, exact.ExactClock):
            message = (
                "Sub-microsecond Trino clock parameters require an explicit TIME precision "
                "in table_schema or resolved target metadata."
            )
            raise ValueError(message)
        target_type = "TIME(6)" + (" WITH TIME ZONE" if value.offset_minutes is not None else "")
        return convert_time_value(value, target_type)
    return time_parameter_value(value, target_type)


def _temporal_parameter(value: Any, target_type: str | None, family: str) -> Any:
    _nested, types, exact = _policies()
    if isinstance(value, exact.ExactClock):
        return _clock_parameter(value, target_type)
    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    if family == "date":
        return date_value(value)
    precision = types.timestamp_scale(target_type) if target_type else _MICROSECOND_PRECISION
    if not target_type and _submicrosecond(value):
        message = (
            "Sub-microsecond Trino parameters require an explicit TIMESTAMP precision "
            "in table_schema or resolved target metadata."
        )
        raise ValueError(message)
    if precision > _MICROSECOND_PRECISION:
        return timestamp_text(value, precision)
    truncated = exact.truncate_temporal(value, precision)
    if isinstance(truncated, exact.ExactTemporal):
        truncated = truncated.calendar_datetime(utc=True)
    return truncated.astimezone(dt.timezone.utc) if truncated.tzinfo is not None else truncated


def _submicrosecond(value: Any) -> bool:
    _nested, _types, exact = _policies()
    if isinstance(value, exact.ExactTemporal):
        return bool(
            value.scale > _MICROSECOND_PRECISION
            and value.ticks % 10 ** (value.scale - _MICROSECOND_PRECISION)
        )
    return bool(pd.Timestamp(value).nanosecond)


def timestamp_text(value: Any, precision: int) -> str:
    _nested, _types, exact = _policies()
    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    value = exact.truncate_temporal(value, precision)
    if isinstance(value, exact.ExactTemporal):
        calendar = value.calendar_datetime(utc=True)
        fraction = value.ticks % 10**value.scale
        digits = f"{fraction:0{value.scale}d}" if value.scale else ""
    else:
        calendar = (
            value.tz_convert("UTC")
            if isinstance(value, pd.Timestamp) and value.tzinfo is not None
            else value
        )
        if calendar.tzinfo is not None:
            calendar = calendar.astimezone(dt.timezone.utc)
        digits = f"{calendar.microsecond:06d}{getattr(calendar, 'nanosecond', 0):03d}"
    digits = digits[:precision].ljust(precision, "0")
    base = str(calendar.date().isoformat()) + str(calendar.strftime(" %H:%M:%S"))
    return (
        base + ("." + digits if precision else "") + (" UTC" if calendar.tzinfo is not None else "")
    )


def literal(value: Any, target_type: str | None) -> str:  # noqa: C901, PLR0911, PLR0912 -- exact SQL value families.
    nested, types, exact = _policies()
    if is_null(value):
        return "NULL"
    if isinstance(value, exact.ExactClock):
        return _clock_literal(value, target_type)
    kind, children = nested.nested_descriptor(target_type or "")
    if kind:
        _validate(value, target_type or "")
        if kind in {"array", "gp_array"}:
            body = "ARRAY[" + ", ".join(literal(item, children[0][1]) for item in value) + "]"
        elif kind in {"row", "tuple"}:
            items = (
                [value[name] for name, _native in children] if isinstance(value, dict) else value
            )
            body = (
                "ROW("
                + ", ".join(literal(item, native) for item, (_name, native) in zip(items, children))
                + ")"
            )
        else:
            body = (
                "MAP(ARRAY["
                + ", ".join(literal(key, children[0][1]) for key, _item in value.items())
                + "], ARRAY["
                + ", ".join(literal(item, children[1][1]) for _key, item in value.items())
                + "])"
            )
        return f"CAST({body} AS {target_type})"
    family = types.parse_native_type(target_type).base.name
    if isinstance(value, dt.time) and family in _TIME_FAMILIES:
        return _clock_literal(value, target_type)
    if isinstance(value, (dt.datetime, pd.Timestamp, np.datetime64, exact.ExactTemporal)):
        if family == "date":
            return "DATE '" + date_value(value).isoformat() + "'"
        precision = (
            types.timestamp_scale(target_type)
            if target_type
            else _literal_timestamp_precision(value)
        )
        return "TIMESTAMP '" + timestamp_text(value, precision) + "'"
    if not target_type:
        container = _untyped_container_literal(value)
        if container is not None:
            return container
    value = normalize_value(value, target_type)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Decimal):
        return "DECIMAL '" + format(value, "f") + "'"
    if isinstance(value, UUID):
        return "UUID '" + str(value) + "'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, dt.date):
        return "DATE '" + value.isoformat() + "'"
    container = _untyped_container_literal(value)
    if container is not None:
        return f"CAST({container} AS {target_type})" if target_type else container
    escaped = str(value).replace("'", "''")
    if isinstance(value, str) or not target_type:
        return f"'{escaped}'"
    return f"CAST('{escaped}' AS {target_type})"


def _literal_timestamp_precision(value: Any) -> int:
    _nested, _types, exact = _policies()
    if not _submicrosecond(value):
        return _MICROSECOND_PRECISION
    return (
        max(_NANOSECOND_PRECISION, value.scale)
        if isinstance(value, exact.ExactTemporal)
        else _NANOSECOND_PRECISION
    )


def _clock_literal(value: Any, target_type: str | None) -> str:
    from .time_values import is_time_type, time_parameter_value  # noqa: PLC0415

    if target_type and not is_time_type(target_type):
        return literal(_clock_parameter(value, target_type), target_type)
    native = target_type or (
        f"TIME({value.scale})" + (" WITH TIME ZONE" if value.offset_minutes is not None else "")
    )
    converted = cast("str", time_parameter_value(value, native))
    return f"CAST('{converted}' AS {native})"


def _untyped_container_literal(value: Any) -> str | None:
    _nested, _types, exact = _policies()
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, (dict, exact.OrderedMap, exact.OrderedMapPairs)):
        _validate(value, "")
        keys = "ARRAY[" + ", ".join(literal(key, None) for key, _item in value.items()) + "]"
        values = "ARRAY[" + ", ".join(literal(item, None) for _key, item in value.items()) + "]"
        return f"MAP({keys}, {values})"
    if isinstance(value, list):
        return "ARRAY[" + ", ".join(literal(item, None) for item in value) + "]"
    if isinstance(value, tuple):
        return "ROW(" + ", ".join(literal(item, None) for item in value) + ")"
    return None
