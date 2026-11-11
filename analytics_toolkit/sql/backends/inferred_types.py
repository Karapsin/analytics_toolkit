"""Certify inferred native representations before publishing a destination schema."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile


def certify_native_candidate(
    profile: ColumnProfile,
    target: str | None,
    backend: str,
    source_type: str | None,
    source_backend: str,
) -> str | None:
    from .source_schema import nullable_clickhouse_type  # noqa: PLC0415 - native backend cycle
    from .type_resolution import (  # noqa: PLC0415 - inference cycle
        _resolve_native,
        validate_profile,
    )

    def certified(candidate: str | None) -> str | None:
        if candidate is None:
            return None
        if backend == "ch" and profile.null_count:
            candidate = nullable_clickhouse_type(candidate)
        selected = converted_profile(profile, candidate)
        try:
            validate_profile(
                selected,
                candidate,
                backend,
                allow_timestamp_precision_loss=False,
                source_type=source_type,
                source_backend=source_backend,
            )
        except ValueError:
            return None
        return candidate

    result = certified(target)
    if result is not None:
        return result
    observed = _resolve_native(profile, backend, source_type, source_backend)
    return certified(observed) if observed != target else None


def converted_profile(profile: ColumnProfile, target: str) -> ColumnProfile:
    """Profile the schema-selected exact conversions without reading rows again."""
    from .nested_types import nested_descriptor  # noqa: PLC0415 - recursive native schemas
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle

    native = parse_native_type(target).base.name
    if (
        native in {"bytea", "varbinary"}
        and profile.families <= {"string", "binary"}
        and profile.utf8_valid
    ):
        return replace(profile, families={"binary"})
    kind, fields = nested_descriptor(target)
    if not kind:
        return profile
    children = [
        converted_profile(child, field[1]) for child, field in zip(profile.children, fields)
    ]
    leaves = profile.array_leaves
    if kind == "gp_array" and leaves is not None:
        leaf = fields[0][1]
        while leaf.endswith("[]"):
            leaf = leaf[:-2]
        leaves = converted_profile(leaves, leaf)
    return replace(profile, children=children, array_leaves=leaves)


def empty_temporal_type(source_type: str | None, backend: str) -> str | None:
    from .source_schema import type_has_timezone  # noqa: PLC0415 - native backend cycle
    from .type_resolution import (  # noqa: PLC0415 - inference cycle
        is_timestamp_type,
        parse_native_type,
        timestamp_scale,
    )

    native = parse_native_type(source_type).base.name
    if native in {"date", "date32"}:
        return "Date32" if backend == "ch" else "DATE"
    if not is_timestamp_type(source_type):
        return None
    aware = type_has_timezone((source_type or "").lower())
    if backend == "ch":
        zone = ", 'UTC'" if aware else ""
        return f"DateTime64({min(9, max(6, timestamp_scale(source_type)))}{zone})"
    return ("TIMESTAMP(6)" if backend == "trino" else "TIMESTAMP") + (
        " WITH TIME ZONE" if aware else ""
    )
