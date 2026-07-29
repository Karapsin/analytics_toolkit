from __future__ import annotations

import time
from itertools import islice
from typing import Any, Callable, Iterator, Sequence

import pandas as pd

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error

from ...backends import get_backend_adapter
from ...connection.config import resolve_connection_backend


class AmbiguousTableLoadError(Exception):
    pass


def execute_values(
    cursor: Any,
    sql: str,
    rows: Sequence[Sequence[Any]],
    page_size: int,
) -> Any:
    from ...backends.gp.insert import execute_values as gp_execute_values

    return gp_execute_values(cursor, sql, rows, page_size)


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
    connection_key: str | None = None,
    rollback_fn: Callable[[Any], None] | None = None,
    replace_connection_fn: Callable[[str, dict[str, Any]], None] | None = None,
) -> int:
    backend = resolve_connection_backend(connection_type)
    adapter = get_backend_adapter(backend)
    normalized_batch = adapter.normalize_insert_batch(batch)

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
            if adapter.should_wrap_insert_error_as_ambiguous(connection, exc):
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
            _replace_connection_before_next_insert_retry(
                adapter=adapter,
                connection_key=connection_key,
                connection_ref=connection_ref,
                rollback_fn=rollback_fn,
                replace_connection_fn=replace_connection_fn,
                attempt=attempt,
                retry_cnt=retry_cnt,
                error=exc,
            )
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
    connection_key: str | None = None,
    rollback_fn: Callable[[Any], None] | None = None,
    replace_connection_fn: Callable[[str, dict[str, Any]], None] | None = None,
) -> int:
    backend = resolve_connection_backend(connection_type)
    adapter = get_backend_adapter(backend)
    row_tuples = [tuple(row) for row in rows]
    if not row_tuples:
        return 0
    normalized_rows = adapter.normalize_insert_rows(row_tuples)

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
            if adapter.should_wrap_insert_error_as_ambiguous(connection, exc):
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
            _replace_connection_before_next_insert_retry(
                adapter=adapter,
                connection_key=connection_key,
                connection_ref=connection_ref,
                rollback_fn=rollback_fn,
                replace_connection_fn=replace_connection_fn,
                attempt=attempt,
                retry_cnt=retry_cnt,
                error=exc,
            )
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


def _replace_connection_before_next_insert_retry(
    *,
    adapter: Any,
    connection_key: str | None,
    connection_ref: dict[str, Any],
    rollback_fn: Callable[[Any], None] | None,
    replace_connection_fn: Callable[[str, dict[str, Any]], None] | None,
    attempt: int,
    retry_cnt: int,
    error: Exception | None = None,
) -> None:
    if (
        (error is not None and is_non_retryable_sql_error(error))
        or not adapter.should_refresh_connection_before_insert_retry()
        or attempt >= retry_cnt
        or connection_key is None
        or replace_connection_fn is None
    ):
        return
    if rollback_fn is not None:
        rollback_fn(connection_ref["connection"])
    replace_connection_fn(connection_key, connection_ref)


def normalize_batch(batch: pd.DataFrame) -> pd.DataFrame:
    return get_backend_adapter("gp").normalize_insert_batch(batch)


def normalize_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[Any, ...]]:
    return get_backend_adapter("gp").normalize_insert_rows(rows)


def _insert_gp_batch(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    gp_insert_chunk_size: int | None = None,
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    get_backend_adapter("gp")._insert_dataframe_batch(
        connection,
        table_name,
        batch,
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
    get_backend_adapter("gp")._insert_rows(
        connection,
        table_name,
        columns,
        rows,
        gp_insert_chunk_size=gp_insert_chunk_size,
        query_label=query_label,
        on_progress=on_progress,
        page_size_getter=page_size_getter,
        on_page_success=on_page_success,
    )


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
    get_backend_adapter("trino")._insert_dataframe_batch(
        connection,
        table_name,
        batch,
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
    get_backend_adapter("trino")._insert_rows(
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
    get_backend_adapter("ch")._insert_dataframe_batch(
        client,
        table_name,
        batch,
        on_progress=on_progress,
    )


def _insert_ch_rows(
    client: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    column_types: dict[str, str] | None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    get_backend_adapter("ch")._insert_rows(
        client,
        table_name,
        columns,
        rows,
        column_types,
        on_progress=on_progress,
    )


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
    get_backend_adapter(backend).insert_dataframe_batch(
        connection,
        table_name,
        batch,
        target_column_types=target_column_types,
        trino_insert_chunk_size=trino_insert_chunk_size,
        gp_insert_chunk_size=gp_insert_chunk_size,
        connection_type=connection_type,
        query_label=query_label,
        on_progress=on_progress,
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
    get_backend_adapter(backend).insert_rows_batch(
        connection,
        table_name,
        columns,
        rows,
        target_column_types=target_column_types,
        trino_insert_chunk_size=trino_insert_chunk_size,
        gp_insert_chunk_size=gp_insert_chunk_size,
        connection_type=connection_type,
        query_label=query_label,
        on_progress=on_progress,
        gp_insert_page_size_getter=gp_insert_page_size_getter,
        on_gp_insert_page_success=on_gp_insert_page_success,
    )


def normalize_ch_batch(batch: pd.DataFrame) -> pd.DataFrame:
    from ...backends.ch import insert as ch_insert

    return ch_insert.normalize_batch(batch)


def _normalize_ch_row(row: Sequence[Any]) -> tuple[Any, ...]:
    from ...backends.ch import insert as ch_insert

    return ch_insert.normalize_row(row)


def _normalize_ch_scalar(value: object) -> object:
    from ...backends.ch import insert as ch_insert

    return ch_insert.normalize_scalar(value)


def _iter_trino_rows(
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    from ...backends.trino import insert as trino_insert

    yield from trino_insert.iter_dataframe_rows(batch, target_column_types)


def _iter_trino_row_values(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    from ...backends.trino import insert as trino_insert

    yield from trino_insert.iter_row_values(columns, rows, target_column_types)


def _normalize_trino_value(value: Any, target_type: str | None) -> Any:
    from ...backends.trino import insert as trino_insert

    return trino_insert.normalize_value(value, target_type)


def _build_trino_values_tuple(
    columns: Sequence[str],
    row: Sequence[Any],
    target_column_types: dict[str, str] | None,
) -> str:
    from ...backends.trino import insert as trino_insert

    return trino_insert.build_values_tuple(columns, row, target_column_types)


def _validate_row_width(columns: Sequence[str], row: Sequence[Any]) -> None:
    from ...backends.trino import insert as trino_insert

    trino_insert.validate_row_width(columns, row)


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
    from ...backends.trino import insert as trino_insert

    return trino_insert.literal(value, target_type)


def _get_trino_insert_chunk_size(
    explicit_value: int | None,
    connection_type: str = "trino",
) -> int:
    from ...backends.trino import insert as trino_insert

    return trino_insert.get_insert_chunk_size(explicit_value, connection_type)


def _get_gp_insert_chunk_size(explicit_value: int | None) -> int:
    from ...backends.gp import insert as gp_insert

    return gp_insert.get_insert_chunk_size(explicit_value)


def _column_type_names(
    columns: Sequence[str],
    column_types: dict[str, str] | None,
) -> list[str] | None:
    from ...backends.ch import insert as ch_insert

    return ch_insert.column_type_names(columns, column_types)
