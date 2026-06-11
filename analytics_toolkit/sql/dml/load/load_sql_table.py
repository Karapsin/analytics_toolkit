from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterator, Optional, Sequence
from decimal import Decimal
from itertools import islice

import pandas as pd

from ...backend_adapters import UNSUPPORTED_BACKEND_MESSAGE, get_backend_adapter
from ...connection.config import (
    TrinoConfig,
    get_connection_config,
    resolve_connection_backend,
)
from ...connection.errors import SqlConfigError, UnsupportedConnectionTypeError
from analytics_toolkit.general import time_print


class AmbiguousTableLoadError(Exception):
    pass


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


DEFAULT_TRINO_INSERT_CHUNK_SIZE = 1000
DEFAULT_GP_INSERT_CHUNK_SIZE = 10_000
BatchInsertBackend = Callable[
    [
        Any,
        str,
        pd.DataFrame,
        Optional[Dict[str, str]],
        Optional[int],
        Optional[int],
        str,
        Optional[str],
        Optional[Callable[[int], None]],
    ],
    None,
]
RowInsertBackend = Callable[
    [
        Any,
        str,
        Sequence[str],
        Sequence[Sequence[Any]],
        Optional[Dict[str, str]],
        Optional[int],
        Optional[int],
        str,
        Optional[str],
        Optional[Callable[[int], None]],
        Optional[Callable[[], int]],
        Optional[Callable[[float, int], None]],
    ],
    None,
]


def insert_table_batch(
    connection_type: str,
    connection_ref: dict[str, Any],
    table_name: str,
    batch: pd.DataFrame,
    retry_fn: Any,
    retry_cnt: int,
    timeout_increment: int | float,
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    gp_insert_chunk_size: int | None = None,
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    backend = resolve_connection_backend(connection_type)
    normalized_batch = normalize_batch(batch) if backend != "trino" else batch

    def operation(attempt: int) -> int:
        connection = connection_ref["connection"]
        try:
            _insert_batch_backend(
                backend,
                connection,
                table_name,
                normalized_batch,
                target_column_types=target_column_types,
                trino_insert_chunk_size=trino_insert_chunk_size,
                gp_insert_chunk_size=gp_insert_chunk_size,
                connection_type=connection_type,
                query_label=query_label,
                on_progress=on_progress,
            )
            return len(normalized_batch)
        except Exception as exc:
            if backend == "gp":
                if getattr(connection, "closed", 0):
                    raise
            else:
                time_print(
                    f"Stage insert failed for {table_name}; "
                    "the current stage table will be discarded and reloaded "
                    "from scratch.",
                    connection=connection_type,
                    backend=backend,
                )
                time_print(
                    f"Original insert error for {table_name}: "
                    f"{type(exc).__name__}: {exc!r}",
                    connection=connection_type,
                    backend=backend,
                )
                raise AmbiguousTableLoadError(
                    f"Ambiguous stage insert outcome on {connection_type} for {table_name}"
                ) from exc
            raise

    return retry_fn(
        operation_name=f"inserting batch into stage table {table_name} on {connection_type}",
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=operation,
    )


def insert_rows_batch(
    connection_type: str,
    connection_ref: dict[str, Any],
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    retry_fn: Any,
    retry_cnt: int,
    timeout_increment: int | float,
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    gp_insert_chunk_size: int | None = None,
    query_label: str | None = None,
    on_success: Callable[[float, int], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
    gp_insert_page_size_getter: Callable[[], int] | None = None,
    on_gp_insert_page_success: Callable[[float, int], None] | None = None,
) -> int:
    backend = resolve_connection_backend(connection_type)
    row_tuples = [tuple(row) for row in rows]
    if not row_tuples:
        return 0
    normalized_rows = (
        row_tuples if backend in {"trino", "ch"} else normalize_rows(row_tuples)
    )

    def operation(attempt: int) -> int:
        connection = connection_ref["connection"]
        try:
            started_at = time.perf_counter()
            _insert_rows_backend(
                backend,
                connection,
                table_name,
                columns,
                normalized_rows,
                target_column_types=target_column_types,
                trino_insert_chunk_size=trino_insert_chunk_size,
                gp_insert_chunk_size=gp_insert_chunk_size,
                connection_type=connection_type,
                query_label=query_label,
                on_progress=on_progress,
                gp_insert_page_size_getter=gp_insert_page_size_getter,
                on_gp_insert_page_success=on_gp_insert_page_success,
            )
            duration_seconds = time.perf_counter() - started_at
        except Exception as exc:
            if backend == "gp":
                if getattr(connection, "closed", 0):
                    raise
            else:
                time_print(
                    f"Stage insert failed for {table_name}; "
                    "the current stage table will be discarded and reloaded "
                    "from scratch.",
                    connection=connection_type,
                    backend=backend,
                )
                time_print(
                    f"Original insert error for {table_name}: "
                    f"{type(exc).__name__}: {exc!r}",
                    connection=connection_type,
                    backend=backend,
                )
                raise AmbiguousTableLoadError(
                    f"Ambiguous stage insert outcome on {connection_type} for {table_name}"
                ) from exc
            raise

        if on_success is not None:
            on_success(duration_seconds, len(normalized_rows))
        return len(normalized_rows)

    return retry_fn(
        operation_name=f"inserting batch into stage table {table_name} on {connection_type}",
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=operation,
    )


def normalize_batch(batch: pd.DataFrame) -> pd.DataFrame:
    normalized = batch.copy()
    for column_name in normalized.columns:
        series = normalized[column_name]
        normalized[column_name] = series.astype(object).where(series.notna(), None)
    return normalized


def normalize_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[Any, ...]]:
    return [tuple(_normalize_nullable_scalar(value) for value in row) for row in rows]


def _insert_gp_batch(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    gp_insert_chunk_size: int | None = None,
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    rows = list(batch.itertuples(index=False, name=None))
    _insert_gp_rows(
        connection,
        table_name,
        batch.columns,
        rows,
        gp_insert_chunk_size=gp_insert_chunk_size,
        query_label=query_label,
        on_progress=on_progress,
    )


def _insert_gp_rows(
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    gp_insert_chunk_size: int | None = None,
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
    page_size_getter: Callable[[], int] | None = None,
    on_page_success: Callable[[float, int], None] | None = None,
) -> None:
    row_tuples = [tuple(row) for row in rows]
    if not row_tuples:
        return

    sql = build_gp_batch_insert_sql(table_name, columns, query_label=query_label)

    cursor = connection.cursor()
    try:
        next_index = 0
        while next_index < len(row_tuples):
            remaining_rows = len(row_tuples) - next_index
            configured_page_size = (
                page_size_getter()
                if page_size_getter is not None
                else gp_insert_chunk_size
            )
            page_size = min(
                _get_gp_insert_chunk_size(configured_page_size),
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


def _insert_trino_batch(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    connection_type: str = "trino",
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    rows = list(batch.itertuples(index=False, name=None))
    _insert_trino_rows(
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


def _insert_trino_rows(
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    connection_type: str = "trino",
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    chunk_size = _get_trino_insert_chunk_size(
        trino_insert_chunk_size,
        connection_type,
    )
    cursor = connection.cursor()
    try:
        row_iterator = _iter_trino_row_values(columns, rows, target_column_types)
        for row_chunk in _chunk_rows(row_iterator, chunk_size):
            params = [value for row in row_chunk for value in row]
            sql = build_trino_batch_insert_sql(
                table_name,
                columns,
                row_count=len(row_chunk),
                query_label=query_label,
            )
            time_print(
                f"Writing {len(row_chunk)} row(s) to table {table_name}",
                backend="trino",
            )
            cursor.execute(sql, params)
            if on_progress is not None:
                on_progress(len(row_chunk))
    finally:
        cursor.close()


def build_gp_batch_insert_sql(
    table_name: str,
    columns: Sequence[str],
    query_label: str | None = None,
) -> str:
    return get_backend_adapter("gp").build_dataframe_batch_insert_sql(
        table_name,
        columns,
        row_count=1,
        query_label=query_label,
    )


def build_trino_batch_insert_sql(
    table_name: str,
    columns: Sequence[str],
    row_count: int,
    query_label: str | None = None,
) -> str:
    if row_count <= 0:
        raise ValueError("row_count must be a positive integer.")

    return get_backend_adapter("trino").build_dataframe_batch_insert_sql(
        table_name,
        columns,
        row_count=row_count,
        query_label=query_label,
    )


def _insert_ch_batch(
    client: Any,
    table_name: str,
    batch: pd.DataFrame,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    normalized_batch = normalize_ch_batch(batch)
    client.insert_df(
        table=table_name,
        df=normalized_batch,
        column_names=list(batch.columns),
    )
    if on_progress is not None:
        on_progress(len(batch))


def _insert_ch_rows(
    client: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    column_types: dict[str, str] | None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    client.insert(
        table=table_name,
        data=[_normalize_ch_row(row) for row in rows],
        column_names=list(columns),
        column_type_names=_column_type_names(columns, column_types),
    )
    if on_progress is not None:
        on_progress(len(rows))


def _insert_gp_batch_backend(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
) -> None:
    del target_column_types, trino_insert_chunk_size, connection_type
    _insert_gp_batch(
        connection,
        table_name,
        batch,
        gp_insert_chunk_size=gp_insert_chunk_size,
        query_label=query_label,
        on_progress=on_progress,
    )


def _insert_trino_batch_backend(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
) -> None:
    del gp_insert_chunk_size
    _insert_trino_batch(
        connection,
        table_name,
        batch,
        target_column_types=target_column_types,
        trino_insert_chunk_size=trino_insert_chunk_size,
        connection_type=connection_type,
        query_label=query_label,
        on_progress=on_progress,
    )


def _insert_ch_batch_backend(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
) -> None:
    del target_column_types, trino_insert_chunk_size, gp_insert_chunk_size
    del connection_type, query_label
    _insert_ch_batch(connection, table_name, batch, on_progress=on_progress)


def _insert_gp_rows_backend(
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
    gp_insert_page_size_getter: Callable[[], int] | None,
    on_gp_insert_page_success: Callable[[float, int], None] | None,
) -> None:
    del target_column_types, trino_insert_chunk_size, connection_type
    _insert_gp_rows(
        connection,
        table_name,
        columns,
        rows,
        gp_insert_chunk_size=gp_insert_chunk_size,
        query_label=query_label,
        on_progress=on_progress,
        page_size_getter=gp_insert_page_size_getter,
        on_page_success=on_gp_insert_page_success,
    )


def _insert_trino_rows_backend(
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
    gp_insert_page_size_getter: Callable[[], int] | None,
    on_gp_insert_page_success: Callable[[float, int], None] | None,
) -> None:
    del gp_insert_chunk_size, gp_insert_page_size_getter, on_gp_insert_page_success
    _insert_trino_rows(
        connection,
        table_name,
        columns,
        rows,
        target_column_types=target_column_types,
        trino_insert_chunk_size=trino_insert_chunk_size,
        connection_type=connection_type,
        query_label=query_label,
        on_progress=on_progress,
    )


def _insert_ch_rows_backend(
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
    gp_insert_page_size_getter: Callable[[], int] | None,
    on_gp_insert_page_success: Callable[[float, int], None] | None,
) -> None:
    del trino_insert_chunk_size, gp_insert_chunk_size, connection_type, query_label
    del gp_insert_page_size_getter, on_gp_insert_page_success
    _insert_ch_rows(
        connection,
        table_name,
        columns,
        rows,
        target_column_types,
        on_progress=on_progress,
    )


_BATCH_INSERT_BACKENDS: dict[str, BatchInsertBackend] = {
    "gp": _insert_gp_batch_backend,
    "trino": _insert_trino_batch_backend,
    "ch": _insert_ch_batch_backend,
}

_ROW_INSERT_BACKENDS: dict[str, RowInsertBackend] = {
    "gp": _insert_gp_rows_backend,
    "trino": _insert_trino_rows_backend,
    "ch": _insert_ch_rows_backend,
}


def _insert_batch_backend(
    backend: str,
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    *,
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
) -> None:
    insert_backend = _BATCH_INSERT_BACKENDS.get(backend)
    if insert_backend is None:
        raise UnsupportedConnectionTypeError(UNSUPPORTED_BACKEND_MESSAGE)
    insert_backend(
        connection,
        table_name,
        batch,
        target_column_types,
        trino_insert_chunk_size,
        gp_insert_chunk_size,
        connection_type,
        query_label,
        on_progress,
    )


def _insert_rows_backend(
    backend: str,
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    target_column_types: dict[str, str] | None,
    trino_insert_chunk_size: int | None,
    gp_insert_chunk_size: int | None,
    connection_type: str,
    query_label: str | None,
    on_progress: Callable[[int], None] | None,
    gp_insert_page_size_getter: Callable[[], int] | None = None,
    on_gp_insert_page_success: Callable[[float, int], None] | None = None,
) -> None:
    insert_backend = _ROW_INSERT_BACKENDS.get(backend)
    if insert_backend is None:
        raise UnsupportedConnectionTypeError(UNSUPPORTED_BACKEND_MESSAGE)
    insert_backend(
        connection,
        table_name,
        columns,
        rows,
        target_column_types,
        trino_insert_chunk_size,
        gp_insert_chunk_size,
        connection_type,
        query_label,
        on_progress,
        gp_insert_page_size_getter,
        on_gp_insert_page_success,
    )


def normalize_ch_batch(batch: pd.DataFrame) -> pd.DataFrame:
    map_values = getattr(batch, "map", None)
    if map_values is None:
        normalized = batch.applymap(_normalize_ch_scalar)
    else:
        normalized = map_values(_normalize_ch_scalar)
    for column_name in normalized.columns:
        series = normalized[column_name]
        normalized[column_name] = series.astype(object).where(series.notna(), None)
    return normalized


def _normalize_ch_row(row: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(
        _normalize_ch_scalar(_normalize_nullable_scalar(value)) for value in row
    )


def _normalize_ch_scalar(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_normalize_ch_scalar(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_ch_scalar(item) for item in value)
    if isinstance(value, dict):
        return {
            _normalize_ch_scalar(key): _normalize_ch_scalar(item)
            for key, item in value.items()
        }
    return value


def _iter_trino_rows(
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    for row in batch.itertuples(index=False, name=None):
        yield from _iter_trino_row_values(batch.columns, [row], target_column_types)


def _iter_trino_row_values(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    for row in rows:
        _validate_row_width(columns, row)
        normalized_values = []
        for column_name, value in zip(columns, row):
            target_type = (
                target_column_types.get(column_name)
                if target_column_types is not None
                else None
            )
            normalized_values.append(_normalize_trino_value(value, target_type))
        yield tuple(normalized_values)


def _normalize_trino_value(value: Any, target_type: str | None) -> Any:
    if _is_null_like(value):
        return None

    if value is None:
        return None

    normalized_target_type = (target_type or "").lower()
    if normalized_target_type.startswith(("varchar", "char", "string")):
        return str(value)
    if normalized_target_type == "bigint":
        return int(value)
    return value


def _build_trino_values_tuple(
    columns: Sequence[str],
    row: Sequence[Any],
    target_column_types: dict[str, str] | None,
) -> str:
    _validate_row_width(columns, row)
    values_sql = []
    for column_name, value in zip(columns, row):
        target_type = (
            target_column_types.get(column_name)
            if target_column_types is not None
            else None
        )
        values_sql.append(_trino_literal(value, target_type))
    return f"({', '.join(values_sql)})"


def _validate_row_width(columns: Sequence[str], row: Sequence[Any]) -> None:
    if len(columns) != len(row):
        raise ValueError("Column and row value counts must match.")


def _chunk_rows(
    rows: Iterator[tuple[Any, ...]],
    chunk_size: int,
) -> Iterator[list[tuple[Any, ...]]]:
    while True:
        chunk = list(islice(rows, chunk_size))
        if not chunk:
            return
        yield chunk


def _chunk_sequence(
    rows: Sequence[tuple[Any, ...]],
    chunk_size: int,
) -> Iterator[list[tuple[Any, ...]]]:
    for start in range(0, len(rows), chunk_size):
        yield list(rows[start : start + chunk_size])


def _trino_literal(value: Any, target_type: str | None) -> str:
    if value is None:
        return "NULL"

    normalized_target_type = (target_type or "").lower()

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"

    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return "NULL"
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


def _get_trino_insert_chunk_size(
    explicit_value: int | None,
    connection_type: str = "trino",
) -> int:
    if explicit_value is not None:
        if explicit_value <= 0:
            raise ValueError("trino_insert_chunk_size must be a positive integer.")
        return explicit_value

    try:
        config = get_connection_config(connection_type)
    except (SqlConfigError, UnsupportedConnectionTypeError):
        return DEFAULT_TRINO_INSERT_CHUNK_SIZE
    if isinstance(config, TrinoConfig) and config.insert_chunk_size is not None:
        return config.insert_chunk_size
    return DEFAULT_TRINO_INSERT_CHUNK_SIZE


def _get_gp_insert_chunk_size(explicit_value: int | None) -> int:
    if explicit_value is not None:
        if explicit_value <= 0:
            raise ValueError("gp_insert_chunk_size must be a positive integer.")
        return explicit_value
    return DEFAULT_GP_INSERT_CHUNK_SIZE


def _column_type_names(
    columns: Sequence[str],
    column_types: dict[str, str] | None,
) -> list[str] | None:
    if column_types is None:
        return None
    try:
        return [column_types[column_name] for column_name in columns]
    except KeyError as exc:
        raise ValueError(
            f"Missing explicit SQL type for column {exc.args[0]!r}."
        ) from exc


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
