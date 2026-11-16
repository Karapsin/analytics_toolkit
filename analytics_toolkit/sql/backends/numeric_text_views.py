"""Transform only declared numeric text leaves in authoritative validation views."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from analytics_toolkit.sql.backends.nested_values import row_items
from analytics_toolkit.sql.core.exact_values import (
    OrderedMap,
    OrderedMapPairs,
    is_null,
    unwrap_enum,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_MAP_ARGUMENT_COUNT = 2


def _sequence_items(value: Any) -> list[Any] | None:
    if isinstance(value, np.ndarray):
        return None if value.ndim == 0 else list(value)
    return list(value) if isinstance(value, (list, tuple)) else None


def _array_view(
    value: Any,
    child: str,
    converter: Callable[[Any, str], tuple[bool, Any]],
    canonicalize: Callable[[str], str],
    *,
    gp_rank: bool,
) -> Any:
    items = _sequence_items(value)
    if items is None:
        return value
    converted = [
        _array_view(item, child, converter, canonicalize, gp_rank=True)
        if gp_rank and _sequence_items(item) is not None
        else numeric_text_view(item, child, converter, canonicalize)
        for item in items
    ]
    return value if all(left is right for left, right in zip(items, converted)) else converted


def _array_child(native_type: str, *, gp_rank: bool) -> str:
    if gp_rank:
        while native_type.endswith("[]"):
            native_type = native_type[:-2]
    return native_type


def _row_view(
    value: Any,
    children: list[tuple[str, str]],
    converter: Callable[[Any, str], tuple[bool, Any]],
    canonicalize: Callable[[str], str],
) -> Any:
    try:
        items = row_items(value, children)
    except ValueError:
        msg = "Selected input is incompatible with authoritative nested schema."
        raise ValueError(msg) from None
    converted = [
        numeric_text_view(item, child, converter, canonicalize)
        for item, (_name, child) in zip(items, children)
    ]
    if all(left is right for left, right in zip(items, converted)):
        return value
    return tuple(converted)


def _map_view(
    value: Any,
    children: list[tuple[str, str]],
    converter: Callable[[Any, str], tuple[bool, Any]],
    canonicalize: Callable[[str], str],
) -> Any:
    pairs = list(value.items())
    converted_pairs = [
        (
            numeric_text_view(key, children[0][1], converter, canonicalize),
            numeric_text_view(item, children[1][1], converter, canonicalize),
        )
        for key, item in pairs
    ]
    if all(
        key is new_key and item is new_item
        for (key, item), (new_key, new_item) in zip(pairs, converted_pairs)
    ):
        return value
    if not isinstance(value, (OrderedMap, OrderedMapPairs)):
        try:
            mapping = dict(converted_pairs)
        except (TypeError, ValueError):
            pass
        else:
            if len(mapping) == len(converted_pairs):
                return mapping
    return OrderedMapPairs(tuple(converted_pairs))


def numeric_text_view(
    value: Any,
    native_type: str,
    converter: Callable[[Any, str], tuple[bool, Any]],
    canonicalize: Callable[[str], str],
) -> Any:
    """Retain unrelated native cells without running context-sensitive final codecs."""
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    value = unwrap_enum(value)
    if is_null(value):
        return value
    logical = canonicalize(native_type)
    kind, children = nested_descriptor(logical)
    if kind in {"array", "gp_array"} and children:
        child = _array_child(children[0][1], gp_rank=kind == "gp_array")
        return _array_view(value, child, converter, canonicalize, gp_rank=kind == "gp_array")
    if kind in {"row", "tuple"}:
        return _row_view(value, children, converter, canonicalize)
    if kind == "map" and len(children) == _MAP_ARGUMENT_COUNT and hasattr(value, "items"):
        return _map_view(value, children, converter, canonicalize)
    if kind:
        return value
    return converter(value, native_type)[1]


__all__ = ["numeric_text_view"]
