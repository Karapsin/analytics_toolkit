from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pandas as pd

from ....backend_adapters import get_backend_adapter
from ....connection.errors import sql_preview
from ....execution.labels import apply_query_label
from analytics_toolkit.general import time_print
from ..runtime.models import RowBatch
from ..runtime.retry import replace_connection, run_with_retry


class TransferSourceStreamReadError(RuntimeError):
    def __init__(
        self,
        *,
        connection_key: str,
        backend: str,
        query: str,
        original_exception: Exception,
    ) -> None:
        self.connection_key = connection_key
        self.backend = backend
        self.query = query
        self.original_exception = original_exception
        self.target_table: str | None = None
        self.retry_batch_size: int | None = None
        self.full_retry_attempt: int | None = None
        super().__init__(self._build_message())

    def with_retry_context(
        self,
        *,
        target_table: str,
        retry_batch_size: int,
        full_retry_attempt: int,
    ) -> TransferSourceStreamReadError:
        self.target_table = target_table
        self.retry_batch_size = retry_batch_size
        self.full_retry_attempt = full_retry_attempt
        self.args = (self._build_message(),)
        return self

    def _build_message(self) -> str:
        parts = [
            "ClickHouse source stream read failed after the stream started",
            f"source_db={self.connection_key}",
            f"backend={self.backend}",
        ]
        if self.target_table is not None:
            parts.append(f"target_table={self.target_table}")
        if self.full_retry_attempt is not None:
            parts.append(f"full_retry_attempt={self.full_retry_attempt}")
        if self.retry_batch_size is not None:
            parts.append(f"retry_batch_size={self.retry_batch_size}")
        parts.extend(
            [
                f"source_sql={sql_preview(self.query, max_chars=500)}",
                f"original_error={self.original_exception!r}",
            ]
        )
        return "; ".join(parts)


def iter_source_batches(
    connection_key: str,
    connection_backend: str,
    connection_ref: dict[str, Any],
    query: str,
    batch_size: int,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None = None,
    get_batch_size: Callable[[], int] | None = None,
    disable_ch_query_limit: bool = False,
) -> Iterator[RowBatch]:
    labeled_query = apply_query_label(query, query_label)
    batch_size_getter = get_batch_size or (lambda: batch_size)
    yield from get_backend_adapter(connection_backend).iter_source_batches(
        connection_key=connection_key,
        connection_ref=connection_ref,
        query=labeled_query,
        get_batch_size=batch_size_getter,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        disable_query_limit=disable_ch_query_limit,
    )


def _iter_dbapi_batches(
    connection_key: str,
    connection_backend: str,
    connection_ref: dict[str, Any],
    query: str,
    get_batch_size: Callable[[], int],
    retry_cnt: int,
    timeout_increment: int | float,
) -> Iterator[RowBatch]:
    cursor, columns = _start_dbapi_query_with_retry(
        connection_key,
        connection_backend,
        connection_ref,
        query,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
    )
    try:
        while True:
            rows = cursor.fetchmany(get_batch_size())
            if not rows:
                break
            yield RowBatch(columns=columns, rows=_rows_as_tuples(rows))
    except Exception:
        time_print(
            f"Failed SQL while reading transfer source:\n{query}",
            connection=connection_key,
            backend=connection_backend,
        )
        raise
    finally:
        cursor.close()


def _iter_clickhouse_batches(
    connection_key: str,
    connection_ref: dict[str, Any],
    query: str,
    get_batch_size: Callable[[], int],
    retry_cnt: int,
    timeout_increment: int | float,
    disable_query_limit: bool,
) -> Iterator[RowBatch]:
    context_manager: Any | None = None
    stream_iterator: Iterator[pd.DataFrame] | None = None
    first_block: pd.DataFrame | None = None

    try:
        context_manager, stream_iterator, first_block = _start_clickhouse_stream_with_retry(
            connection_key,
            connection_ref,
            query,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            disable_query_limit=disable_query_limit,
        )

        columns: list[str] | None = None
        pending_rows: list[tuple[Any, ...]] = []

        if first_block is not None and not first_block.empty:
            columns = list(first_block.columns)
            pending_rows.extend(_dataframe_rows_as_tuples(first_block))
            yield from _drain_full_row_batches(
                columns,
                pending_rows,
                get_batch_size,
            )

        if stream_iterator is not None:
            for block in stream_iterator:
                if block.empty:
                    continue

                if columns is None:
                    columns = list(block.columns)
                pending_rows.extend(_dataframe_rows_as_tuples(block))
                yield from _drain_full_row_batches(
                    columns,
                    pending_rows,
                    get_batch_size,
                )

        if pending_rows and columns is not None:
            yield RowBatch(columns=columns, rows=pending_rows)
    except Exception as exc:
        time_print(
            f"Failed SQL while reading transfer source:\n{query}",
            connection=connection_key,
            backend="ch",
        )
        if _is_clickhouse_stream_transport_error(exc):
            raise TransferSourceStreamReadError(
                connection_key=connection_key,
                backend="ch",
                query=query,
                original_exception=exc,
            ) from exc
        raise
    finally:
        if context_manager is not None:
            context_manager.__exit__(None, None, None)


def _start_dbapi_query_with_retry(
    connection_key: str,
    connection_backend: str,
    connection_ref: dict[str, Any],
    query: str,
    retry_cnt: int,
    timeout_increment: int | float,
) -> tuple[Any, list[str]]:
    def operation(attempt: int) -> tuple[Any, list[str]]:
        cursor = connection_ref["connection"].cursor()
        try:
            cursor.execute(query)
            columns = [column[0] for column in cursor.description or []]
            return cursor, columns
        except Exception:
            cursor.close()
            get_backend_adapter(connection_backend).rollback_quietly(
                connection_ref["connection"]
            )
            replace_connection(connection_key, connection_ref)
            raise

    return run_with_retry(
        operation_name=f"starting source query on {connection_key} ({connection_backend})",
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=operation,
    )


def _start_clickhouse_stream_with_retry(
    connection_key: str,
    connection_ref: dict[str, Any],
    query: str,
    retry_cnt: int,
    timeout_increment: int | float,
    disable_query_limit: bool,
) -> tuple[Any, Iterator[pd.DataFrame], pd.DataFrame | None]:
    def operation(attempt: int) -> tuple[Any, Iterator[pd.DataFrame], pd.DataFrame | None]:
        connection = connection_ref["connection"]
        original_query_limit = getattr(connection, "query_limit", None)
        should_restore_query_limit = (
            disable_query_limit
            and hasattr(connection, "query_limit")
            and original_query_limit
        )
        if should_restore_query_limit:
            connection.query_limit = 0
        try:
            context_manager = connection.query_df_stream(query)
        finally:
            if should_restore_query_limit:
                connection.query_limit = original_query_limit
        try:
            stream = context_manager.__enter__()
            iterator = iter(stream)
            while True:
                try:
                    block = next(iterator)
                except StopIteration:
                    return context_manager, iterator, None
                if block.empty:
                    continue
                return context_manager, iterator, block
        except Exception:
            context_manager.__exit__(None, None, None)
            replace_connection(connection_key, connection_ref)
            raise

    return run_with_retry(
        operation_name=f"starting source query on {connection_key} (ch)",
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=operation,
    )


def _rows_as_tuples(rows: list[Any]) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in rows]


def _dataframe_rows_as_tuples(block: pd.DataFrame) -> list[tuple[Any, ...]]:
    return list(block.itertuples(index=False, name=None))


def _drain_full_row_batches(
    columns: list[str],
    pending_rows: list[tuple[Any, ...]],
    get_batch_size: Callable[[], int],
) -> Iterator[RowBatch]:
    while True:
        current_batch_size = get_batch_size()
        if len(pending_rows) < current_batch_size:
            break
        batch_rows = pending_rows[:current_batch_size]
        del pending_rows[:current_batch_size]
        yield RowBatch(columns=columns, rows=batch_rows)


def _is_clickhouse_stream_transport_error(exc: Exception) -> bool:
    for chained_exc in _exception_chain(exc):
        class_names = {cls.__name__ for cls in type(chained_exc).mro()}
        if class_names & {
            "ChunkedEncodingError",
            "IncompleteRead",
            "ProtocolError",
            "ReadTimeoutError",
        }:
            return True
        message = str(chained_exc).lower()
        if any(
            pattern in message
            for pattern in (
                "unexpected failure to read next chunk",
                "incompleteread",
                "connection broken",
                "response ended prematurely",
            )
        ):
            return True
    return False


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
        stack.extend(arg for arg in current.args if isinstance(arg, BaseException))
