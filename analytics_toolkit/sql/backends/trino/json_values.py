"""Certify native parameters for Trino's CAST(value AS JSON), without parsing text."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from analytics_toolkit.sql.core.exact_values import (
    ExactTemporal,
    decimal_digits,
    integer_text,
    is_null,
    unwrap_enum,
)

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_PRECISION_LIMIT = 38
_INT32_LIMIT = 2**31
_INT64_LIMIT = 2**63
_MESSAGE = (
    "Selected input is incompatible with authoritative Trino JSON native casts; "
    "use supported scalar values or containers with compatible native SQL element types."
)
_FAMILIES = {
    "boolean",
    "integer",
    "float",
    "decimal",
    "string",
    "date",
    "timestamp",
    "array",
    "tuple",
    "map",
}
_NUMERIC = {"integer", "decimal", "double"}


@dataclass(frozen=True)
class _CastType:
    kind: str
    precision: int = 0
    scale: int = 0
    integral: int = 0
    binary_exact: bool = True
    children: tuple[_CastType, ...] = ()


def is_json_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(native_type)
    if native.name != "json":
        return False
    if native.arguments:
        message = "Trino JSON does not accept type arguments."
        raise ValueError(message)
    return True


def validate_json_profile(profile: ColumnProfile, native_type: str) -> bool:
    """Recognize the cast family; the complete transport pass certifies actual cells."""
    if not is_json_type(native_type):
        return False
    pending = [profile]
    while pending:
        item = pending.pop()
        if (
            not item.families <= _FAMILIES
            or not item.utf8_valid
            or item.map_null_keys
            or item.map_duplicate_keys
            or not item.map_comparable_keys
        ):
            raise ValueError(_MESSAGE)
        pending.extend(item.children)
    return True


def validate_json_transport_value(value: Any, native_type: str) -> None:
    """Certify JSON leaves without changing the existing bound parameter payload."""
    if not _contains_json_type(native_type):
        return
    try:
        _parameter_type(value, native_type)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(_MESSAGE) from None


def _parameter_type(value: Any, native_type: str) -> _CastType:
    if is_json_type(native_type):
        return _value_type(unwrap_enum(value), root=True)
    if is_null(value):
        return _CastType("unknown")
    if not _contains_json_type(native_type):
        return _CastType(native_type.lower())
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- declared container grammar.
        nested_descriptor,
    )

    kind, fields = nested_descriptor(native_type)
    if kind in {"array", "gp_array"}:
        return _CastType("array", children=(_common_parameters(value, fields[0][1]),))
    if kind in {"row", "tuple"}:
        items = [value[name] for name, _ in fields] if isinstance(value, dict) else value
        children = tuple(
            _parameter_type(item, native) for item, (_name, native) in zip(items, fields)
        )
        return _CastType("row", children=children)
    return _CastType(
        "map",
        children=(
            _common_parameters(value.keys(), fields[0][1]),
            _common_parameters(value.values(), fields[1][1]),
        ),
    )


def _common_parameters(values: Any, native_type: str) -> _CastType:
    result = _CastType("unknown")
    for value in values:
        result = _common_type(result, _parameter_type(value, native_type))
    return result


@lru_cache(maxsize=1024)
def _contains_json_type(native_type: str) -> bool:
    if is_json_type(native_type):
        return True
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- declared container grammar.
        nested_descriptor,
    )

    return any(_contains_json_type(native) for _name, native in nested_descriptor(native_type)[1])


def _number(value: int | Decimal) -> _CastType:
    if isinstance(value, int):
        if not -_INT64_LIMIT <= value < _INT64_LIMIT:
            raise ValueError(_MESSAGE)
        precision = 10 if -_INT32_LIMIT <= value < _INT32_LIMIT else 19
        digits = len(integer_text(abs(value))) if value else 0
        return _CastType(
            "integer", precision, integral=digits, binary_exact=int(float(value)) == value
        )
    integral, scale = decimal_digits(value)
    precision = max(1, integral + scale)
    if precision > _PRECISION_LIMIT:
        raise ValueError(_MESSAGE)
    approximate = float(value)
    binary_exact = np.isfinite(approximate) and Decimal.from_float(approximate) == value
    return _CastType("decimal", precision, scale, integral, bool(binary_exact))


def _value_type(value: Any, *, root: bool = False) -> _CastType:  # noqa: C901, PLR0911, PLR0912 -- native SDK families.
    if value is None or (root and is_null(value)):
        return _CastType("unknown")
    if root and isinstance(value, np.ndarray):
        return _value_type(value.tolist())
    if root and isinstance(value, (np.bool_, np.integer, np.floating)):
        value = value.item()
    if isinstance(value, bool):
        return _CastType("boolean")
    if isinstance(value, int):
        return _number(value)
    if isinstance(value, float):
        return _CastType("double")
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, str):
        value.encode("utf-8")
        return _CastType("string")
    if root and isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    if root and isinstance(value, ExactTemporal):
        if value.timezone is not None or value.ticks % 10 ** max(value.scale - 6, 0):
            raise ValueError(_MESSAGE)
        value = value.calendar_datetime()
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None or getattr(value, "nanosecond", False):
            raise ValueError(_MESSAGE)
        return _CastType("timestamp")
    if isinstance(value, dt.date):
        return _CastType("date")
    if isinstance(value, list):
        return _CastType("array", children=(_common_values(value),))
    if isinstance(value, tuple):
        if not value:
            raise ValueError(_MESSAGE)
        return _CastType("row", children=tuple(_value_type(item) for item in value))
    if isinstance(value, dict):
        keys = list(value)
        if any(is_null(key) for key in keys):
            raise ValueError(_MESSAGE)
        key = _common_values(keys)
        if key.kind not in {"unknown", "boolean", "integer", "decimal", "double", "string"}:
            raise ValueError(_MESSAGE)
        return _CastType("map", children=(key, _common_values(list(value.values()))))
    raise ValueError(_MESSAGE)


def _common_values(values: list[Any]) -> _CastType:
    result = _CastType("unknown")
    for value in values:
        result = _common_type(result, _value_type(value))
    return result


def _common_type(left: _CastType, right: _CastType) -> _CastType:  # noqa: PLR0911 -- native SQL common types.
    if left.kind == "unknown":
        return right
    if right.kind == "unknown":
        return left
    if left.kind in _NUMERIC and right.kind in _NUMERIC:
        if "double" in {left.kind, right.kind}:
            if not left.binary_exact or not right.binary_exact:
                raise ValueError(_MESSAGE)
            return _CastType("double")
        integral = max(left.integral, right.integral)
        precision = max(left.precision, right.precision)
        if left.kind == right.kind == "integer":
            return _CastType(
                "integer",
                precision,
                integral=integral,
                binary_exact=left.binary_exact and right.binary_exact,
            )
        scale = max(left.scale, right.scale)
        precision = min(
            _PRECISION_LIMIT,
            max(left.precision - left.scale, right.precision - right.scale) + scale,
        )
        if integral > precision - scale:
            raise ValueError(_MESSAGE)
        return _CastType(
            "decimal", precision, scale, integral, left.binary_exact and right.binary_exact
        )
    if {left.kind, right.kind} <= {"date", "timestamp"}:
        return _CastType("timestamp" if "timestamp" in {left.kind, right.kind} else "date")
    if left.kind != right.kind or len(left.children) != len(right.children):
        raise ValueError(_MESSAGE)
    return _CastType(
        left.kind, children=tuple(_common_type(a, b) for a, b in zip(left.children, right.children))
    )
