"""Preserve every converted native map entry before selecting its container."""

from __future__ import annotations

import struct
from typing import Any

from analytics_toolkit.sql.core.exact_values import OrderedMapPairs


def converted_map(
    pairs: list[tuple[Any, Any]], backend: str, *, mapping_input: bool, key_type: str | None = None
) -> dict[Any, Any] | OrderedMapPairs:
    pairs = _key_payload_pairs(pairs, backend, key_type)
    unique_required = backend == "trino" or (backend != "ch" and mapping_input)
    seen: set[Any] = set()
    duplicates = False
    comparable = True
    hashable_payload = True
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
        try:
            hash(key)
        except (TypeError, ValueError):
            hashable_payload = False
    if unique_required and not hashable_payload:
        msg = "Converted map keys require a supported hashable representation for the destination."
        raise ValueError(msg)
    if unique_required or (mapping_input and comparable and hashable_payload and not duplicates):
        return dict(pairs)
    return OrderedMapPairs(pairs)


def _key_payload_pairs(
    pairs: list[tuple[Any, Any]], backend: str, key_type: str | None
) -> list[tuple[Any, Any]]:
    if backend != "trino":
        return pairs
    return [(_trino_key_payload(key, key_type), value) for key, value in pairs]


def profile_map_key_identity(key: Any, key_type: str, backend: str) -> Any:
    """Use native clock equality without replacing raw precision-profile values."""
    if backend != "trino":
        return key
    from .trino.time_values import contains_time_type  # noqa: PLC0415 -- clock key grammar.

    return _key_identity(key, key_type, backend) if contains_time_type(key_type) else key


def _trino_key_payload(key: Any, key_type: str | None) -> Any:
    from .nested_types import nested_descriptor  # noqa: PLC0415 -- native key shape.
    from .nested_values import row_items  # noqa: PLC0415 -- exact row arity.
    from .trino.time_values import (  # noqa: PLC0415 -- destination clock cast semantics.
        time_key_value_and_identity,
    )

    if key is None:
        return None
    kind, fields = nested_descriptor(key_type or "")
    if kind in {"row", "tuple"}:
        return tuple(
            _trino_key_payload(item, native)
            for item, (_name, native) in zip(row_items(key, fields), fields)
        )
    if kind in {"array", "gp_array"} and isinstance(key, (list, tuple)):
        values = [_trino_key_payload(item, fields[0][1]) for item in key]
        return tuple(values) if isinstance(key, tuple) else values
    handled, canonical, _identity = time_key_value_and_identity(key, key_type)
    return canonical if handled else key


def _key_identity(key: Any, key_type: str | None, backend: str) -> Any:
    from .nested_types import nested_descriptor  # noqa: PLC0415 - final native key structure
    from .nested_values import row_items  # noqa: PLC0415 - exact row arity and field identity
    from .type_resolution import _permits_null  # noqa: PLC0415 -- native nullability.

    if key is None:
        if backend == "ch" and key_type is not None and _permits_null(key_type):
            return None
        msg = "Converted map keys cannot contain NULL components."
        raise ValueError(msg)
    kind, fields = nested_descriptor(key_type or "")
    if kind in {"row", "tuple"}:
        return tuple(
            _key_identity(item, native, backend)
            for item, (_name, native) in zip(row_items(key, fields), fields)
        )
    if kind in {"array", "gp_array"}:
        if not isinstance(key, (list, tuple)):
            msg = "Converted array map keys require a positional sequence."
            raise ValueError(msg)
        return tuple(_key_identity(item, fields[0][1], backend) for item in key)
    if kind == "map" and backend == "trino":
        msg = "Converted map keys contain an unsupported native key structure."
        raise ValueError(msg)
    return _scalar_key_identity(key, key_type, backend)


def _scalar_key_identity(key: Any, key_type: str | None, backend: str) -> Any:
    from .type_resolution import (  # noqa: PLC0415 -- resolved native key semantics.
        _FLOAT32_BITS,
        _float_width,
        parse_native_type,
    )

    native = parse_native_type(key_type).base
    if backend == "trino":
        from .trino.time_values import (  # noqa: PLC0415 -- destination clock identity.
            time_key_identity,
        )

        handled, identity = time_key_identity(key, key_type)
        if handled:
            return identity
    if _float_width(key_type, backend) == _FLOAT32_BITS:
        try:
            return struct.unpack(">f", struct.pack(">f", float(key)))[0]
        except (OverflowError, TypeError, ValueError) as exc:
            msg = "Converted map key exceeds its native floating-point representation."
            raise ValueError(msg) from exc
    if backend == "trino" and native.name in {"char", "character"} and isinstance(key, str):
        return key.rstrip(" ")
    return key
