"""Preserve every converted native map entry before selecting its container."""

from __future__ import annotations

import struct
from typing import Any

from analytics_toolkit.sql.core.exact_values import OrderedMapPairs


def converted_map(
    pairs: list[tuple[Any, Any]], backend: str, *, mapping_input: bool, key_type: str | None = None
) -> dict[Any, Any] | OrderedMapPairs:
    unique_required = backend == "trino" or (backend != "ch" and mapping_input)
    seen: set[Any] = set()
    duplicates = False
    comparable = True
    for key, _value in pairs:
        if key is None:
            msg = "Converted map keys cannot be NULL."
            raise ValueError(msg)
        identity = _key_identity(key, key_type, backend)
        try:
            duplicate = identity in seen
            seen.add(identity)
        except (TypeError, ValueError) as exc:
            if unique_required:
                msg = "Converted map keys are not comparable for the destination."
                raise ValueError(msg) from exc
            comparable = False
        else:
            if duplicate and unique_required:
                msg = "Converted map keys are not unique for the destination."
                raise ValueError(msg)
            duplicates |= duplicate
    if unique_required or (mapping_input and comparable and not duplicates):
        return dict(pairs)
    return OrderedMapPairs(pairs)


def _key_identity(key: Any, key_type: str | None, backend: str) -> Any:
    from .type_resolution import (  # noqa: PLC0415 - resolved native key semantics
        _FLOAT32_BITS,
        _float_width,
        parse_native_type,
    )

    native = parse_native_type(key_type).base
    if _float_width(key_type, backend) == _FLOAT32_BITS:
        try:
            return struct.unpack(">f", struct.pack(">f", float(key)))[0]
        except (OverflowError, TypeError, ValueError) as exc:
            msg = "Converted map key exceeds its native floating-point representation."
            raise ValueError(msg) from exc
    if backend == "trino" and native.name in {"char", "character"} and isinstance(key, str):
        return key.rstrip(" ")
    return key
