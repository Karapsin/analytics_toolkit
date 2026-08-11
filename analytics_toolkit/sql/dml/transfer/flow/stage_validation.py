from __future__ import annotations

# ruff: noqa: EM102, TC001, TID252, TRY003
from typing import TYPE_CHECKING, Any

from ...table._basic_ops import count_table_rows
from ..runtime.models import TransferOptions
from .stage_identity import TransferInternalColumns

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def validate_transfer_stage_identity(
    *,
    options: TransferOptions,
    connection: Any,
    stage_tables: Sequence[str],
    internal_columns: TransferInternalColumns | None,
    expected_slice_counts: Mapping[int, int],
) -> None:
    """Validate user-payload rows across the attempt's target stages.

    The historical function name is retained for internal compatibility. Transfer
    identity now lives in RAM and reserved resource names rather than row columns.
    """
    del internal_columns
    expected_count = sum(expected_slice_counts.values())
    actual_count = sum(
        count_table_rows(
            options.to_db_backend,
            connection,
            stage_table,
            query_label=options.query_label,
        )
        for stage_table in stage_tables
    )
    if actual_count != expected_count:
        message = (
            "Transfer stage payload count failure: "
            f"expected {expected_count} row(s), got {actual_count}."
        )
        raise RuntimeError(message)


def validate_transfer_stage_slice(  # noqa: PLR0913 -- compatibility surface
    *,
    options: TransferOptions,
    connection: Any,
    stage_table: str | Sequence[str] | None,
    internal_columns: TransferInternalColumns,
    slice_id: int,
    expected_count: int,
    streamed_count: int,
    log_prefix: str = "",
) -> None:
    """Validate one immutable keyed slice from its in-memory acknowledgements."""
    del options, connection, internal_columns, slice_id, log_prefix
    if streamed_count != expected_count:
        raise RuntimeError(
            f"Transfer slice streamed {streamed_count} row(s); expected {expected_count}."
        )
    if expected_count and not stage_table:
        message = "A non-empty transfer slice has no target stage."
        raise RuntimeError(message)
