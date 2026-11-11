"""Default adapter policy for backends without implicit client timestamp zones."""

from __future__ import annotations

from typing import Any


def destination_write_timezone(
    adapter: Any, connection: Any, *, required: bool = False
) -> str | None:
    del adapter, connection, required
    return None


def requires_destination_timezone(adapter: Any, schema: dict[str, str]) -> bool:
    del adapter, schema
    return False


def requires_dataframe_timezone(adapter: Any, df: Any) -> bool:
    del adapter, df
    return False


def normalize_transfer_source_batch(
    adapter: Any,
    batch: Any,
    source_column_types: dict[str, str | None],
) -> Any:
    del adapter, source_column_types
    return batch


def source_read_timezone(adapter: Any, connection: Any) -> str | None:
    del adapter, connection
    return None


def normalize_source_temporal_batch(
    adapter: Any,
    connection: Any,
    batch: Any,
    column_types: dict[str, str | None],
    cache: dict[str, Any],
) -> Any:
    del adapter, connection, column_types, cache
    return batch
