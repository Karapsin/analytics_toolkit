"""Exact native candidates chosen from the complete observed value profile."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, cast

from .decimal_values import resolve_decimal_type
from .numeric_profiles import exact_decimal_profile

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile

_SCALAR_TEXT = {"gp": "TEXT", "trino": "VARCHAR", "ch": "String"}
_SCALAR_FLOAT = {"gp": "DOUBLE PRECISION", "trino": "DOUBLE", "ch": "Float64"}
_DEFAULT_TIMESTAMP_SCALE = 6
_MAX_TRINO_TIME_SCALE = 12


def _empty_type(backend: str, source_type: str | None) -> str:
    # Recursive native inference policy.
    from .inferred_types import empty_temporal_type  # noqa: PLC0415
    from .type_resolution import (  # noqa: PLC0415 - recursive native inference policy.
        _integer_type,
        decimal_domain,
        integer_domain,
    )

    domain = integer_domain(source_type)
    if domain is not None:
        return _integer_type(domain[0], domain[1], backend, source_type) or "BIGINT"
    shape = decimal_domain(source_type)
    if shape and shape[0] <= {"gp": 1000, "trino": 38, "ch": 76}[backend]:
        name = {"gp": "NUMERIC", "trino": "DECIMAL", "ch": "Decimal"}[backend]
        return f"{name}({shape[0]}, {shape[1]})"
    if shape:
        return {"gp": "NUMERIC", "trino": "DECIMAL(38, 10)", "ch": "Decimal(38, 10)"}[backend]
    return empty_temporal_type(source_type, backend) or _SCALAR_TEXT[backend]


def _numeric_type(profile: ColumnProfile, backend: str, source_type: str | None) -> str | None:
    from .type_resolution import _integer_type  # noqa: PLC0415 - recursive native inference policy.

    families = profile.families
    if families <= {"integer"}:
        return _integer_type(
            profile.minimum_integer or 0, profile.maximum_integer or 0, backend, source_type
        )
    if "float" not in families:
        return resolve_decimal_type(profile, backend)
    if families <= {"integer", "float"} and profile.integers_fit_float64:
        return _SCALAR_FLOAT[backend]
    if profile.floating_values_finite and not profile.float_negative_zero:
        decimal = resolve_decimal_type(exact_decimal_profile(profile), backend)
        if decimal is not None:
            return decimal
    return (
        _SCALAR_FLOAT[backend]
        if profile.integers_fit_float64 and profile.decimals_fit_float64
        else None
    )


def _timestamp_type(profile: ColumnProfile, backend: str) -> str | None:
    if (profile.aware and profile.naive) or (profile.aware and "date" in profile.families):
        return None
    if backend in {"gp", "trino"}:
        if profile.temporal_scale > _DEFAULT_TIMESTAMP_SCALE:
            return None
        return ("TIMESTAMP(6)" if backend == "trino" else "TIMESTAMP") + (
            " WITH TIME ZONE" if profile.aware else ""
        )
    zone = ", 'UTC'" if profile.aware else ""
    return f"DateTime64({max(6, profile.temporal_scale)}{zone})"


def _clock_type(profile: ColumnProfile, backend: str) -> str | None:
    if backend == "gp":
        from .gp.temporal_values import (  # noqa: PLC0415 - recursive native inference policy.
            infer_clock_interval_type,
        )

        return infer_clock_interval_type(profile)
    if (
        backend == "trino"
        and profile.families == {"time"}
        and profile.trino_time_constraints_valid
        and not (profile.aware and profile.naive)
        and not profile.time_zone_unresolved
        and profile.temporal_scale <= _MAX_TRINO_TIME_SCALE
    ):
        return f"TIME({max(6, profile.temporal_scale)})" + (
            " WITH TIME ZONE" if profile.aware else ""
        )
    return None


def resolve_observed_type(
    profile: ColumnProfile, backend: str, source_type: str | None, source_backend: str | None = None
) -> str | None:
    from .nested_types import (  # noqa: PLC0415 - recursive native inference policy.
        nested_descriptor,
        resolve_nested,
    )

    families = profile.families
    if backend == "ch" and profile.null_count and families & {"array", "tuple", "map"}:
        return None
    if not families:
        return (
            resolve_nested(profile, backend, source_type)
            if nested_descriptor(source_type or "")[0]
            else _empty_type(backend, source_type)
        )
    if families <= {"integer", "decimal", "float"}:
        return _numeric_type(profile, backend, source_type)
    if families <= {"date", "timestamp"} and "timestamp" in families:
        return _timestamp_type(profile, backend)
    if families in ({"time"}, {"interval"}):
        return _clock_type(profile, backend)
    return _other_type(profile, backend, source_type, source_backend)


def _other_type(
    profile: ColumnProfile, backend: str, source_type: str | None, source_backend: str | None
) -> str | None:
    from .nested_types import resolve_nested  # noqa: PLC0415 - recursive native inference policy.

    # Recursive native inference policy.
    from .text_types import resolve_text_family  # noqa: PLC0415

    families = profile.families
    if families in ({"boolean"}, {"uuid"}):
        return "UUID" if "uuid" in families else "Bool" if backend == "ch" else "BOOLEAN"
    if families == {"date"}:
        lower, upper = cast("dt.date", profile.minimum_date), cast("dt.date", profile.maximum_date)
        if backend == "ch" and (lower < dt.date(1970, 1, 1) or upper > dt.date(2149, 6, 6)):
            return "Date32"
        return "Date" if backend == "ch" else "DATE"
    if families <= {"ipv4", "ipv6"} and backend == "ch" and not profile.network_scope_present:
        return "IPv6" if "ipv6" in families else "IPv4"
    if families <= {"string", "binary"}:
        return resolve_text_family(profile, backend, source_type, source_backend)
    return resolve_nested(profile, backend, source_type)
