from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd


DEFAULT_GP_INSERT_CHUNK_SIZE = 10_000


def execute_values(
    cursor: Any,
    sql: str,
    rows: Sequence[Sequence[Any]],
    page_size: int,
) -> Any:
    try:
        from psycopg2.extras import execute_values as psycopg2_execute_values
    except ImportError as exc:
        raise ImportError(
            "The 'psycopg2' package is required for Greenplum batch inserts."
        ) from exc
    return psycopg2_execute_values(cursor, sql, rows, page_size=page_size)


def normalize_insert_batch(adapter: Any, batch: pd.DataFrame) -> pd.DataFrame:
    del adapter
    normalized = batch.copy()
    for column_name in normalized.columns:
        series = normalized[column_name]
        normalized[column_name] = series.astype(object).where(series.notna(), None)
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
) -> None:
    row_tuples = [tuple(row) for row in rows]
    if not row_tuples:
        return

    sql = adapter.build_dataframe_batch_insert_sql(
        table_name,
        columns,
        row_count=1,
        query_label=query_label,
    )

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
            row_chunk = row_tuples[next_index:next_index + page_size]
            started_at = time.perf_counter()
            execute_values(cursor, sql, row_chunk, page_size=page_size)
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
        cursor.close()


def get_insert_chunk_size(explicit_value: int | None) -> int:
    if explicit_value is not None:
        if explicit_value <= 0:
            raise ValueError("gp_insert_chunk_size must be a positive integer.")
        return explicit_value
    return DEFAULT_GP_INSERT_CHUNK_SIZE


def _normalize_nullable_scalar(value: Any) -> Any:
    if _is_null_like(value):
        return None
    return value


def _is_null_like(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
