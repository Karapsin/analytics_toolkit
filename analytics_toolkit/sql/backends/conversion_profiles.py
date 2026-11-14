"""Certify the actual transport representation against its resolved schema."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

from .native_values import (
    canonical_native_type,
    requires_native_assignment,
    validate_transport_value,
)
from .type_resolution import validate_profile
from .value_profiles import ColumnProfile

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .type_resolution import ResolvedColumn


def validate_converted_rows(
    rows: Iterable[Sequence[Any]],
    resolutions: Sequence[ResolvedColumn],
    *,
    backend: str,
    destination_timezone: str | None = None,
) -> list[ColumnProfile]:
    if any(
        resolution.native_assignment_required
        and (
            resolution.target_backend != backend
            or not requires_native_assignment(resolution.target_type, backend)
        )
        for resolution in resolutions
    ):
        message = "Pending native assignment requires a nonlocal Greenplum destination type."
        raise ValueError(message)
    profiles = [
        ColumnProfile(
            native_type=canonical_native_type(resolution.target_type, backend),
            source_backend=backend,
            destination_timezone=resolution.destination_timezone or destination_timezone,
            temporal_type=canonical_native_type(resolution.target_type, backend),
        )
        for resolution in resolutions
    ]
    for row in rows:
        raise_if_cancelled()
        if len(row) != len(resolutions):
            message = "Converted input row does not match the resolved schema."
            raise ValueError(message)
        for profile, resolution, value in zip(profiles, resolutions, row):
            converted = resolution.convert(value)
            validate_transport_value(converted, resolution.target_type, backend)
            profile.add(converted)
    for profile, resolution in zip(profiles, resolutions):
        if resolution.native_assignment_required and requires_native_assignment(
            resolution.target_type, backend
        ):
            continue
        validate_profile(
            profile,
            resolution.target_type,
            backend,
            allow_timestamp_precision_loss=False,
            source_type=resolution.target_type,
            source_backend=backend,
            destination_timezone=resolution.destination_timezone or destination_timezone,
        )
    return profiles
