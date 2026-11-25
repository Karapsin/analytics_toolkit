"""Exact eager source acquisition shared by keyed and ordinal snapshot readers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.source_codecs import exact_source_connection
from analytics_toolkit.sql.dml.transfer.io.source import normalize_source_batch
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions


def enable_exact_staged_source_values(connection: Any) -> None:
    """Select exact transport decoders before eager results can lose their context."""
    enable = getattr(connection, "enable_exact_source_values", None)
    if callable(enable):
        enable()


def exact_staged_source_connection(
    options: TransferOptions, connection: Any, source_types: dict[str, str | None]
) -> Any:
    """Opt only this internal read into exact cursor decoding and send-format values."""
    enable_exact_staged_source_values(connection)
    return exact_source_connection(
        options.from_db_backend,
        connection,
        source_columns=[SourceColumn(name, native) for name, native in source_types.items()],
    )


def normalize_staged_source_result(  # noqa: PLR0913 - exact result, source identity and scheduled bound.
    options: TransferOptions,
    connection: Any,
    result: Any,
    source_types: dict[str, str | None],
    *,
    row_limit: int,
    log_prefix: str,
) -> RowBatch:
    row_count = len(result.columns[0]) if result.columns else 0
    if any(len(column) != row_count for column in result.columns):
        message = f"{log_prefix}Source batch columns have unequal lengths."
        raise RuntimeError(message)
    if row_count > row_limit:
        message = (
            f"{log_prefix}Source batch returned {row_count} row(s); scheduled limit is {row_limit}."
        )
        raise RuntimeError(message)
    batch = RowBatch(list(result.column_names), list(zip(*result.columns)))
    normalized = normalize_source_batch(options, connection, batch, source_types)
    if normalized.row_count > row_limit:
        message = (
            f"{log_prefix}Normalized source batch returned {normalized.row_count} row(s); "
            f"scheduled limit is {row_limit}."
        )
        raise RuntimeError(message)
    return normalized
