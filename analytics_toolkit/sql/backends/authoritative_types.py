"""Resolve caller and existing schema authority without prematurely adapting rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile


def resolve_authoritative_profile(  # noqa: PLR0913 -- preserve separate source, target and temporal policies.
    profile: ColumnProfile,
    target_type: str,
    backend: str,
    *,
    source_type: str | None,
    source_backend: str,
    allow_timestamp_precision_loss: bool,
    destination_timezone: str | None,
    allow_pending_native_assignment: bool,
) -> ResolvedColumn:
    from analytics_toolkit.sql.backends.native_values import (  # noqa: PLC0415 -- authoritative and native policy initialization cycle.
        pending_native_assignment,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- public compatibility facade owns the resolution type.
        ResolvedColumn,
        validate_profile,
    )

    pending = allow_pending_native_assignment and pending_native_assignment(
        profile,
        target_type,
        backend,
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
    )
    if not pending:
        validate_profile(
            profile,
            target_type,
            backend,
            allow_timestamp_precision_loss=allow_timestamp_precision_loss,
            source_type=source_type,
            source_backend=source_backend,
            destination_timezone=destination_timezone,
        )
    return ResolvedColumn(
        target_type,
        source_type or "",
        source_backend,
        "native_assignment_pending" if pending else "authoritative",
        certified_input_count=0 if pending else profile.count,
        target_backend=backend,
        destination_timezone=destination_timezone,
        native_assignment_required=pending,
        native_assignment_precision_loss=pending and allow_timestamp_precision_loss,
    )
