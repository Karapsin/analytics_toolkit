"""Validate the final cast without changing frozen staging payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from analytics_toolkit.sql.backends.explicit_boolean_cast import explicit_text_boolean_validation
from analytics_toolkit.sql.backends.explicit_date_cast import explicit_text_date_validation
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.core.type_resolution import resolve_column_type
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
    from analytics_toolkit.sql.core.replay_spool import ReplaySpool
    from analytics_toolkit.sql.core.type_resolution import ResolvedColumn


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
        explicit_text_date_validation(resolution, existing_types[column.name], backend)
        or explicit_text_boolean_validation(resolution, existing_types[column.name], backend)
        or resolve_column_type(
            SourceColumn(column.name, resolution.target_type),
            backend,
            profile,
            source_backend=backend,
            authoritative_type=existing_types[column.name],
            allow_timestamp_precision_loss=False,
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
