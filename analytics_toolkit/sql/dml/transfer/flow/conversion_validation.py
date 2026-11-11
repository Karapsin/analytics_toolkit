"""Validate the final cast without changing frozen staging payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

if TYPE_CHECKING:
    from collections.abc import Sequence

    from analytics_toolkit.sql.core.replay_spool import ReplaySpool
    from analytics_toolkit.sql.core.type_resolution import ResolvedColumn


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
