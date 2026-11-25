"""Defer GP native temporal text and requested typmods to owned assignment."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .type_identity import canonical_builtin_type

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile


def _temporal_scalar(native_type: str | None) -> tuple[str, bool] | None:
    native = canonical_builtin_type(native_type or "")
    if native is None:
        return None
    array = native.endswith("[]")
    while native.endswith("[]"):
        native = native[:-2].rstrip()
    if native != "DATE" and not native.startswith(("TIMESTAMP", "TIME", "INTERVAL")):
        return None
    return native, array


def contains_temporal_type(native_type: str | None) -> bool:
    """Recognize native GP temporal scalars and their array leaves."""
    return _temporal_scalar(native_type) is not None


def _leaf_profile(profile: ColumnProfile, *, array: bool) -> ColumnProfile:
    return profile.array_leaves if array and profile.array_leaves is not None else profile


def requires_temporal_assignment(profile: ColumnProfile, native_type: str) -> bool:
    """Select actual text leaves or a requested reduction that needs GP rounding."""
    spec = _temporal_scalar(native_type)
    if spec is None:
        return False
    from analytics_toolkit.sql.backends.type_resolution import timestamp_scale  # noqa: PLC0415

    scalar, array = spec
    leaf = _leaf_profile(profile, array=array)
    if "string" in leaf.families or (scalar == "DATE" and "timestamp" in leaf.families):
        return True
    if scalar.startswith("INTERVAL"):
        return _requires_interval_assignment(leaf, scalar)
    return scalar != "DATE" and leaf.temporal_scale > timestamp_scale(scalar)


def _requires_interval_assignment(leaf: ColumnProfile, scalar: str) -> bool:
    from .temporal_values import _temporal_type  # noqa: PLC0415 -- native field policy.

    interval = _temporal_type(scalar)
    if interval is None:
        return False
    if leaf.temporal_scale > interval.precision:
        return True
    if interval.least_field in {"year", "month"}:
        return (
            leaf.interval_days_nonzero
            or bool(leaf.interval_time_gcd)
            or (interval.least_field == "year" and bool(leaf.interval_months_gcd % 12))
        )
    unit = {"day": 86400, "hour": 3600, "minute": 60}.get(interval.least_field)
    return bool(leaf.interval_time_gcd % (unit * 10**9)) if unit else False


def _without_text(profile: ColumnProfile) -> ColumnProfile:
    # Preserve all shape, NULL, range, scale and awareness evidence. Only the
    # family of values that the server will parse is deferred.
    return replace(
        profile,
        families=profile.families - {"string"},
        children=[_without_text(child) for child in profile.children],
        array_leaves=_without_text(profile.array_leaves)
        if profile.array_leaves is not None
        else None,
    )


def pending_temporal_assignment(
    profile: ColumnProfile,
    native_type: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
) -> bool:
    """Validate unaffected native leaves before deferring complete assignment."""
    if not requires_temporal_assignment(profile, native_type):
        return False
    from analytics_toolkit.sql.backends.type_resolution import validate_profile  # noqa: PLC0415

    validate_profile(
        _without_text(profile),
        native_type,
        "gp",
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
    )
    return True


def temporal_text_value_required(value: Any, native_type: str) -> bool:
    """Inspect owned values without mistaking typed timestamps for text inputs."""
    spec = _temporal_scalar(native_type)
    if spec is None:
        return False
    from analytics_toolkit.sql.backends.native_scalars import ExactArray  # noqa: PLC0415
    from analytics_toolkit.sql.core.exact_values import unwrap_enum  # noqa: PLC0415

    value = unwrap_enum(value)
    if isinstance(value, ExactArray):
        return any(temporal_text_value_required(item, spec[0]) for item in value.values)
    if spec[1] and isinstance(value, (list, tuple)):
        return any(temporal_text_value_required(item, native_type) for item in value)
    return isinstance(value, str)


def temporal_assignment_value(
    value: Any, native_type: str, *, destination_timezone: str | None = None
) -> Any:
    """Preserve the server's input, including precision until actual assignment."""
    del destination_timezone
    from analytics_toolkit.sql.backends.native_scalars import ExactArray  # noqa: PLC0415
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        is_null,
        unwrap_enum,
    )

    value = unwrap_enum(value)
    if is_null(value):
        return None
    spec = _temporal_scalar(native_type)
    if spec is None:
        return value
    scalar, array = spec
    if isinstance(value, ExactArray):
        return replace(
            value, values=tuple(temporal_assignment_value(item, scalar) for item in value.values)
        )
    if array and isinstance(value, (list, tuple)):
        return [temporal_assignment_value(item, native_type) for item in value]
    return _scalar_assignment_value(value, scalar)


def _scalar_assignment_value(value: Any, scalar: str) -> Any:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- precise scalar carrier.
        ExactClock,
        ExactTemporal,
    )

    if scalar == "DATE" and not isinstance(value, str):
        from .date_values import date_value  # noqa: PLC0415 -- retain native DATE parameter policy.

        return date_value(value)
    if isinstance(value, (ExactTemporal, ExactClock)) and value.ticks % 10 ** max(
        0, value.scale - 6
    ):
        return value.isoformat()
    if getattr(value, "nanosecond", 0):
        return value.isoformat()
    return value


def contains_timestamp_type(native_type: str | None) -> bool:
    spec = _temporal_scalar(native_type)
    return spec is not None and spec[0].startswith("TIMESTAMP")


def requires_timestamp_text_assignment(profile: ColumnProfile, native_type: str) -> bool:
    return contains_timestamp_type(native_type) and requires_temporal_assignment(
        profile, native_type
    )


def pending_timestamp_assignment(
    profile: ColumnProfile,
    native_type: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
) -> bool:
    return contains_timestamp_type(native_type) and pending_temporal_assignment(
        profile,
        native_type,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
    )


timestamp_text_value_required = temporal_text_value_required
timestamp_assignment_value = temporal_assignment_value
