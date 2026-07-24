from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from itertools import islice
from typing import Any

import pandas as pd


DEFAULT_TRINO_INSERT_CHUNK_SIZE = 1000


def insert_dataframe_batch(
    adapter: Any,
    connection: Any,
    table_name: str,
    batch: Any,
    *,
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    connection_type: str = "trino",
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
        target_column_types=target_column_types,
        trino_insert_chunk_size=trino_insert_chunk_size,
        connection_type=connection_type,
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
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    connection_type: str = "trino",
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    from analytics_toolkit.general import time_print

    chunk_size = get_insert_chunk_size(trino_insert_chunk_size, connection_type)
    cursor = connection.cursor()
    try:
        row_iterator = iter_row_values(columns, rows, target_column_types)
        for row_chunk in _chunk_rows(row_iterator, chunk_size):
            params = [value for row in row_chunk for value in row]
            sql = adapter.build_dataframe_batch_insert_sql(
                table_name,
                columns,
                row_count=len(row_chunk),
                query_label=query_label,
            )
            time_print(
                f"Writing {len(row_chunk)} row(s) to table {table_name}",
                backend=adapter.backend,
            )
            cursor.execute(sql, params)
            if on_progress is not None:
                on_progress(len(row_chunk))
    finally:
        cursor.close()


def get_insert_chunk_size(
    explicit_value: int | None,
    connection_type: str = "trino",
) -> int:
    if explicit_value is not None:
        if explicit_value <= 0:
            raise ValueError("trino_insert_chunk_size must be a positive integer.")
        return explicit_value

    from ...connection.config import TrinoConfig, get_connection_config
    from ...connection.errors import SqlConfigError, UnsupportedConnectionTypeError

    try:
        config = get_connection_config(connection_type)
    except (SqlConfigError, UnsupportedConnectionTypeError):
        return DEFAULT_TRINO_INSERT_CHUNK_SIZE
    if isinstance(config, TrinoConfig) and config.insert_chunk_size is not None:
        return config.insert_chunk_size
    return DEFAULT_TRINO_INSERT_CHUNK_SIZE


def iter_dataframe_rows(
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    for row in batch.itertuples(index=False, name=None):
        yield from iter_row_values(batch.columns, [row], target_column_types)


def iter_row_values(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    for row in rows:
        validate_row_width(columns, row)
        normalized_values = []
        for column_name, value in zip(columns, row):
            target_type = (
                target_column_types.get(column_name) if target_column_types is not None else None
            )
            normalized_values.append(normalize_value(value, target_type))
        yield tuple(normalized_values)


def normalize_value(value: Any, target_type: str | None) -> Any:
    if _is_null_like(value):
        return None

    normalized_target_type = (target_type or "").lower()
    if normalized_target_type.startswith(("varchar", "char", "string")):
        return str(value)
    if normalized_target_type == "bigint":
        return int(value)
    return value


def build_values_tuple(
    columns: Sequence[str],
    row: Sequence[Any],
    target_column_types: dict[str, str] | None,
) -> str:
    validate_row_width(columns, row)
    values_sql = []
    for column_name, value in zip(columns, row):
        target_type = (
            target_column_types.get(column_name) if target_column_types is not None else None
        )
        values_sql.append(literal(value, target_type))
    return f"({', '.join(values_sql)})"


def validate_row_width(columns: Sequence[str], row: Sequence[Any]) -> None:
    if len(columns) != len(row):
        raise ValueError("Column and row value counts must match.")


def literal(value: Any, target_type: str | None) -> str:
    if _is_null_like(value):
        return "NULL"

    normalized_target_type = (target_type or "").lower()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, pd.Timestamp):
        timestamp_value = value.to_pydatetime()
        if normalized_target_type == "date":
            return f"DATE '{timestamp_value.strftime('%Y-%m-%d')}'"
        return f"TIMESTAMP '{timestamp_value.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
    if hasattr(value, "isoformat") and normalized_target_type == "date":
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, (int, float)):
        return str(value)

    escaped = str(value).replace("'", "''")
    if normalized_target_type:
        return f"CAST('{escaped}' AS {target_type})"
    return f"'{escaped}'"


def _chunk_rows(
    rows: Iterator[tuple[Any, ...]],
    chunk_size: int,
) -> Iterator[list[tuple[Any, ...]]]:
    while True:
        chunk = list(islice(rows, chunk_size))
        if not chunk:
            return
        yield chunk


def _is_null_like(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
