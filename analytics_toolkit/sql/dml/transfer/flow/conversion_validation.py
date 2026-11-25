"""Validate the final cast without changing frozen staging payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from analytics_toolkit.sql.backends.explicit_boolean_cast import explicit_text_boolean_validation
from analytics_toolkit.sql.backends.explicit_date_cast import explicit_text_date_validation
from analytics_toolkit.sql.backends.explicit_integer_cast import explicit_text_integer_validation
from analytics_toolkit.sql.backends.explicit_numeric_text_cast import (
    explicit_text_numeric_validation,
)
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.native_values import pending_native_assignment
from analytics_toolkit.sql.core.type_resolution import resolve_column_type
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
    from analytics_toolkit.sql.core.replay_spool import ReplaySpool
    from analytics_toolkit.sql.core.type_resolution import ResolvedColumn


def resolve_final_column_conversion(  # noqa: PLR0913 -- stage and target authorities remain distinct.
    column: SourceColumn,
    resolution: ResolvedColumn,
    profile: ColumnProfile,
    *,
    backend: str,
    target_type: str,
    destination_timezone: str | None,
    allow_pending_native_assignment: bool = False,
) -> ResolvedColumn:
    """Share the final native SQL-cast selector for ordinary and pending siblings."""
    date_validation = explicit_text_date_validation(resolution, target_type, backend)
    if date_validation is not None:
        # Deliberate text intermediates keep their canonical DATE contract;
        # native temporal syntax belongs to an authoritative temporal stage.
        return date_validation
    if allow_pending_native_assignment and pending_native_assignment(
        profile,
        target_type,
        backend,
        allow_timestamp_precision_loss=False,
        destination_timezone=destination_timezone,
    ):
        return resolve_column_type(
            SourceColumn(column.name, resolution.target_type),
            backend,
            profile,
            source_backend=backend,
            authoritative_type=target_type,
            allow_timestamp_precision_loss=False,
            destination_timezone=destination_timezone,
            allow_pending_native_assignment=True,
        )
    return (
        explicit_text_boolean_validation(resolution, target_type, backend)
        or explicit_text_integer_validation(resolution, target_type, backend)
        or explicit_text_numeric_validation(resolution, target_type, backend)
        or resolve_column_type(
            SourceColumn(column.name, resolution.target_type),
            backend,
            profile,
            source_backend=backend,
            authoritative_type=target_type,
            allow_timestamp_precision_loss=False,
            destination_timezone=destination_timezone,
            allow_pending_native_assignment=allow_pending_native_assignment,
        )
    )


def resolve_final_conversions(  # noqa: PLR0913 - staged and final schema authorities
    columns: Sequence[SourceColumn],
    resolutions: Sequence[ResolvedColumn],
    profiles: Sequence[ColumnProfile],
    *,
    backend: str,
    existing_types: Mapping[str, str] | None,
    destination_timezone: str | None,
) -> list[ResolvedColumn] | None:
    """Retain the existing final SQL-cast compatibility checks and diagnostics."""
    if existing_types is None:
        return None
    return [
        resolve_final_column_conversion(
            column,
            resolution,
            profile,
            backend=backend,
            target_type=existing_types[column.name],
            destination_timezone=destination_timezone,
        )
        for column, profile, resolution in zip(columns, profiles, resolutions)
    ]


def validate_final_conversions(
    spool: ReplaySpool,
    stage_conversions: Sequence[ResolvedColumn],
    target_conversions: Sequence[ResolvedColumn],
) -> None:
    """Check every selected row through both schemas before destination mutation."""
    try:
        for row in spool.iter_rows():
            raise_if_cancelled()
            for stage, target, value in zip(stage_conversions, target_conversions, row):
                target.convert(stage.convert(value))
    except (TypeError, ValueError, OverflowError) as error:
        message = "Selected input cannot be converted to the resolved destination schema."
        raise ValueError(message) from error
