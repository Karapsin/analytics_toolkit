"""Decode explicitly identified control fields without touching user String values."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError


def metadata_text(value: Any) -> str:
    """Return an exact UTF-8 metadata identity, rejecting unverifiable values."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            message = "ClickHouse control metadata contains invalid UTF-8 text."
            raise ClickHouseClusterTopologyError(message) from exc
    if not isinstance(value, str):
        message = "ClickHouse control metadata did not return a text identity."
        raise ClickHouseClusterTopologyError(message)
    return value


def decode_metadata_rows(rows: list[Any], text_columns: tuple[int, ...]) -> list[Any]:
    """Opt in by field position; the default retains original rows and cells."""
    if not text_columns:
        return rows
    return [
        tuple(
            metadata_text(value) if position in text_columns and isinstance(value, bytes) else value
            for position, value in enumerate(row)
        )
        for row in rows
    ]


def decode_metadata_frame(frame: Any, columns: tuple[str, ...]) -> Any:
    """Normalize known metadata text columns, retaining nulls and frame metadata."""
    import pandas as pd  # noqa: PLC0415

    frame = frame.copy()
    for name in columns:
        if name in frame.columns:
            frame[name] = [
                value if value is None or pd.isna(value) else metadata_text(value)
                for value in frame[name]
            ]
    return frame
