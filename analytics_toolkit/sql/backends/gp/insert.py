from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import pandas as pd

from analytics_toolkit.sql.backends.execution_outcome import close_cursor

DEFAULT_GP_INSERT_CHUNK_SIZE = 10_000


def execute_values(
    cursor: Any,
    sql: str,
    rows: Sequence[Sequence[Any]],
    page_size: int,
    *,
    template: str | None = None,
) -> Any:
    try:
        from psycopg2.extras import execute_values as psycopg2_execute_values
    except ImportError as exc:
        raise ImportError(
            "The 'psycopg2' package is required for Greenplum batch inserts."
        ) from exc
    if template is not None:
        return psycopg2_execute_values(cursor, sql, rows, page_size=page_size, template=template)
    return psycopg2_execute_values(cursor, sql, rows, page_size=page_size)


def normalize_insert_batch(adapter: Any, batch: pd.DataFrame) -> pd.DataFrame:
    del adapter
    normalized = batch.copy()
    for column_name in normalized.columns:
        series = normalized[column_name]
        normalized_series = pd.Series(
            [_normalize_nested_scalar(value) for value in series], index=series.index, dtype=object
        )
        normalized[column_name] = normalized_series
    return normalized


def normalize_insert_rows(
    adapter: Any,
    rows: Sequence[Sequence[Any]],
) -> list[tuple[Any, ...]]:
    del adapter
    return [tuple(_normalize_nullable_scalar(value) for value in row) for row in rows]


def insert_dataframe_batch(
    adapter: Any,
    connection: Any,
    table_name: str,
    batch: Any,
    *,
    gp_insert_chunk_size: int | None = None,
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    rows = list(batch.itertuples(index=False, name=None))
    insert_rows(
        adapter,
        connection,
        table_name,
        batch.columns,
        rows,
        gp_insert_chunk_size=gp_insert_chunk_size,
        query_label=query_label,
        on_progress=on_progress,
    )


def insert_rows(
    adapter: Any,
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    gp_insert_chunk_size: int | None = None,
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
    page_size_getter: Callable[[], int] | None = None,
    on_page_success: Callable[[float, int], None] | None = None,
    target_column_types: dict[str, str] | None = None,
) -> None:
    row_tuples = normalize_insert_rows(adapter, rows)
    row_tuples = normalize_json_columns(columns, row_tuples, target_column_types)
    if not row_tuples:
        return

    sql = adapter.build_dataframe_batch_insert_sql(
        table_name,
        columns,
        row_count=1,
        query_label=query_label,
    )

    template = array_parameter_template(columns, target_column_types)
    cursor = connection.cursor()
    try:
        next_index = 0
        while next_index < len(row_tuples):
            remaining_rows = len(row_tuples) - next_index
            configured_page_size = (
                page_size_getter() if page_size_getter is not None else gp_insert_chunk_size
            )
            page_size = min(
                get_insert_chunk_size(configured_page_size),
                remaining_rows,
            )
            row_chunk = row_tuples[next_index : next_index + page_size]
            started_at = time.perf_counter()
            if template is None:
                execute_values(cursor, sql, row_chunk, page_size=page_size)
            else:
                execute_values(cursor, sql, row_chunk, page_size=page_size, template=template)
            duration_seconds = time.perf_counter() - started_at
            if on_progress is not None:
                on_progress(len(row_chunk))
            if on_page_success is not None:
                on_page_success(duration_seconds, len(row_chunk))
            next_index += page_size
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        close_cursor(cursor, backend="gp")


def get_insert_chunk_size(explicit_value: int | None) -> int:
    if explicit_value is not None:
        if explicit_value <= 0:
            raise ValueError("gp_insert_chunk_size must be a positive integer.")
        return explicit_value
    return DEFAULT_GP_INSERT_CHUNK_SIZE


def _normalize_nullable_scalar(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return _normalize_nested_scalar(value)
    if _is_null_like(value):
        return None
    if isinstance(value, dict):
        from psycopg2.extras import Json  # noqa: PLC0415

        return Json(_normalize_nested_scalar(value))
    return _normalize_uuid_scalar(value)


def normalize_json_columns(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
) -> list[tuple[Any, ...]]:
    if not target_column_types:
        return [tuple(row) for row in rows]
    json_indexes = {
        index
        for index, column in enumerate(columns)
        if target_column_types.get(column, "").strip().lower() in {"json", "jsonb"}
    }
    json_arrays = {}
    for index, column in enumerate(columns):
        native = target_column_types.get(column, "").strip().lower()
        rank = 0
        while native.endswith("[]"):
            native = native[:-2].rstrip()
            rank += 1
        if rank and native in {"json", "jsonb"}:
            json_arrays[index] = rank
    if not json_indexes and not json_arrays:
        return [tuple(row) for row in rows]
    from psycopg2.extras import Json  # noqa: PLC0415

    return [
        tuple(
            Json(value)
            if index in json_indexes and isinstance(value, (dict, list))
            else _normalize_json_array(value, json_arrays[index])
            if index in json_arrays
            else value
            for index, value in enumerate(row)
        )
        for row in rows
    ]


def _normalize_uuid_scalar(value: Any) -> Any:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - backend import cycle
        MAX_TEMPORAL_SCALE,
        ExactTemporal,
    )

    if isinstance(value, ExactTemporal):
        if not 0 <= value.scale <= MAX_TEMPORAL_SCALE or value.ticks % 10 ** max(
            0, value.scale - 6
        ):
            msg = (
                "Raw timestamp exceeds Greenplum microsecond precision; "
                "resolve the schema conversion first."
            )
            raise ValueError(msg)
        return ExactTemporal(value.ticks, value.scale, "UTC").calendar_datetime(utc=True)
    return str(value) if isinstance(value, UUID) else value


def _is_null_like(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict)):
        return False
    try:
        missing = pd.isna(value)
        return bool(missing) if getattr(missing, "ndim", 0) == 0 else False
    except (TypeError, ValueError):
        return False


def _normalize_nested_scalar(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_nested_scalar(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_nested_scalar(item) for item in value)
    if isinstance(value, dict):
        return {key: _normalize_nested_scalar(item) for key, item in value.items()}
    return None if _is_null_like(value) else _normalize_uuid_scalar(value)


def array_parameter_template(
    columns: Sequence[str], column_types: dict[str, str] | None
) -> str | None:
    if not column_types or not any(
        column_types.get(name, "").rstrip().endswith("[]") for name in columns
    ):
        return None
    # UUID string arrays need a destination type context. Preserve exact numeric
    # element typmods and multidimensional types without global driver adapters.
    placeholders = []
    for name in columns:
        native = column_types.get(name, "").rstrip()
        placeholders.append(
            f"CAST(%s AS {native.replace('%', '%%')})" if native.endswith("[]") else "%s"
        )
    return "(" + ", ".join(placeholders) + ")"


def _normalize_json_array(value: Any, rank: int) -> Any:
    from psycopg2.extras import Json  # noqa: PLC0415 -- optional backend driver.

    if _is_null_like(value):
        return None
    if not isinstance(value, (list, tuple)):
        message = "Greenplum JSON array values must match the destination array rank."
        raise TypeError(message)
    if rank > 1:
        return [_normalize_json_array(item, rank - 1) for item in value]
    return [
        None if _is_null_like(item) else item if isinstance(item, Json) else Json(item)
        for item in value
    ]
