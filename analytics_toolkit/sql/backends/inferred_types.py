"""Certify inferred native representations before publishing a destination schema."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .type_resolution import ResolvedColumn
    from .value_profiles import ColumnProfile


def declared_target(
    source_type: str | None, source_backend: str, target_backend: str
) -> str | None:
    """Map source metadata without claiming that selected values already fit."""
    if not source_type:
        return None
    from analytics_toolkit.sql.backends.models import (  # noqa: PLC0415 - registry cycle.
        SourceColumn,
    )
    from analytics_toolkit.sql.backends.source_schema import (  # noqa: PLC0415 -- backend registry recursion.
        map_source_column,
    )

    return map_source_column(SourceColumn("", source_type), target_backend, source_backend or None)


def certify_native_candidate(  # noqa: PLR0913 - frozen context and source identity
    profile: ColumnProfile,
    target: str | None,
    backend: str,
    source_type: str | None,
    source_backend: str,
    *,
    destination_timezone: str | None = None,
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
                destination_timezone=destination_timezone,
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
    from .numeric_profiles import (  # noqa: PLC0415 - inferred conversion cycle.
        exact_decimal_profile,
    )
    from .type_resolution import (  # noqa: PLC0415 - native grammar cycle
        _float_width,
        decimal_domain,
        parse_native_type,
    )

    native = parse_native_type(target).base.name
    if "float" in profile.families and (
        decimal_domain(target) is not None or native in {"numeric", "decimal"}
    ):
        return exact_decimal_profile(profile)
    if (
        _float_width(target, "") is not None
        and profile.families <= {"integer", "decimal", "float"}
        and profile.integers_fit_float64
        and profile.decimals_fit_float64
    ):
        return replace(profile, families={"float"} if profile.families else set())
    if (
        native in {"ipv4", "ipv6"}
        and profile.families <= {"ipv4", "ipv6"}
        and not profile.network_scope_present
    ):
        return replace(profile, families={"string"} if profile.families else set())
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
    if (
        native.startswith("time")
        and not native.startswith("timestamp")
        and backend in {"gp", "trino"}
    ):
        scale = timestamp_scale(source_type)
        if scale <= (6 if backend == "gp" else 12):
            return f"TIME({scale})" + (
                " WITH TIME ZONE" if "with time zone" in native or native == "timetz" else ""
            )
    if native.startswith("interval") and backend == "gp":
        return "INTERVAL(6)"
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


def resolve_column_type(  # noqa: PLR0913 - preserve column and authority context
    column: Any,
    target_backend: str,
    profile: ColumnProfile,
    source_backend: str | None = None,
    authoritative_type: str | None = None,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
    allow_pending_native_assignment: bool = False,
) -> ResolvedColumn:
    from .type_resolution import resolve_profile  # noqa: PLC0415 - native resolution cycle

    return resolve_profile(
        profile,
        target_backend,
        source_type=column.native_type,
        source_backend=source_backend or "",
        authoritative_type=authoritative_type,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
        allow_pending_native_assignment=allow_pending_native_assignment,
    )
