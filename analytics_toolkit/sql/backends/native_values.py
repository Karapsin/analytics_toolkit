"""Dispatch exact conversions for backend-specific scalar and nested values."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .ch.value_validation import validate_constrained_value
from .gp.json_values import convert_json_value, is_json_type
from .nested_types import convert_nested, nested_descriptor

if TYPE_CHECKING:
    from .type_resolution import ResolvedColumn


def convert_native_value(value: Any, resolution: ResolvedColumn) -> tuple[bool, Any]:
    """Return whether a specialized codec handled the destination value."""
    from .type_resolution import parse_native_type  # noqa: PLC0415 -- native grammar cycle.

    if resolution.target_backend == "gp" and is_json_type(resolution.target_type):
        return True, convert_json_value(
            value,
            resolution.target_type,
            decoded=resolution.source_backend == "gp" and is_json_type(resolution.source_type),
        )
    if parse_native_type(resolution.target_type).base.name in {"enum8", "enum16", "fixedstring"}:
        return True, validate_constrained_value(value, resolution.target_type)
    if nested_descriptor(resolution.target_type)[0]:
        return True, convert_nested(
            value,
            resolution.target_type,
            resolution.target_backend,
            destination_timezone=resolution.destination_timezone,
        )
    return False, value
