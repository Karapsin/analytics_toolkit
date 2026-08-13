from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

import pandas as pd

from analytics_toolkit.sql.ddl.identifiers import _parse_table_name, quote_identifier

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from types import TracebackType

    _NativeStreamContext = AbstractContextManager[Iterator[pd.DataFrame]]
else:
    _NativeStreamContext = AbstractContextManager

_METADATA_PAIR_LENGTH = 2
_INVALID_STREAM_METADATA = "Native ClickHouse stream did not return column metadata."
_INVALID_STREAM_ROWS = "Native ClickHouse stream returned an invalid row block."


class NativeQueryResult:
    def __init__(
        self,
        data: Sequence[Sequence[Any]],
        columns_with_types: Sequence[tuple[str, str]],
        *,
        column_oriented: bool,
    ) -> None:
        self.column_names = tuple(name for name, _type_name in columns_with_types)
        self.column_types = tuple(type_name for _name, type_name in columns_with_types)
        self._data = data
        self._column_oriented = column_oriented
        self._result_rows: list[tuple[Any, ...]] | None = None
        self._result_columns: list[tuple[Any, ...]] | None = None

    @property
    def result_rows(self) -> list[tuple[Any, ...]]:
        if self._result_rows is None:
            if self._column_oriented:
                self._result_rows = list(zip(*self._data)) if self._data else []
            else:
                self._result_rows = [tuple(row) for row in self._data]
        return self._result_rows

    @property
    def result_columns(self) -> list[tuple[Any, ...]]:
        if self._result_columns is None:
            if self._column_oriented:
                self._result_columns = [tuple(column) for column in self._data]
            elif self._data:
                self._result_columns = list(zip(*self._data))
            else:
                self._result_columns = [() for _name in self.column_names]
        return self._result_columns

    @property
    def row_count(self) -> int:
        if self._column_oriented:
            return len(self._data[0]) if self._data else 0
        return len(self._data)


class _NativeDataFrameStream(_NativeStreamContext):
    def __init__(self, client: Any, sql: str) -> None:
        self._client = client
        self._sql = sql
        self._iterator: Iterator[Any] | None = None
        self._exhausted = False

    def __enter__(self) -> Iterator[pd.DataFrame]:
        self._iterator = iter(
            self._client.execute_iter(
                self._sql,
                with_column_types=True,
                chunk_size=65_536,
            )
        )
        return self._dataframes()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del traceback
        if exc_type is not None or not self._exhausted:
            self._client.disconnect_connection()

    def _dataframes(self) -> Iterator[pd.DataFrame]:
        if self._iterator is None:
            raise RuntimeError(_INVALID_STREAM_METADATA)
        try:
            first = next(self._iterator)
        except StopIteration:
            self._exhausted = True
            return

        rows, columns_with_types = _split_first_stream_block(first)
        columns = [name for name, _type_name in columns_with_types]
        if rows:
            yield pd.DataFrame.from_records(rows, columns=columns)
        for raw_rows in self._iterator:
            rows = _normalize_stream_rows(raw_rows)
            if rows:
                yield pd.DataFrame.from_records(rows, columns=columns)
        self._exhausted = True


class NativeClickHouseClient:
    is_native_transport = True

    def __init__(self, client: Any) -> None:
        self._client = client
        self._closed = False

    def command(
        self,
        sql: str,
        settings: Mapping[str, Any] | None = None,
    ) -> Any:
        result = (
            self._client.execute(sql)
            if settings is None
            else self._client.execute(sql, settings=dict(settings))
        )
        progress = getattr(getattr(self._client, "last_query", None), "progress", None)
        written_rows = getattr(progress, "written_rows", 0)
        if (
            isinstance(written_rows, int)
            and not isinstance(written_rows, bool)
            and written_rows > 0
        ):
            return {"written_rows": written_rows}
        return result

    def query(
        self,
        sql: str,
        *,
        column_oriented: bool = False,
        settings: Mapping[str, Any] | None = None,
    ) -> NativeQueryResult:
        kwargs: dict[str, Any] = {
            "with_column_types": True,
            "columnar": column_oriented,
        }
        if settings is not None:
            kwargs["settings"] = dict(settings)
        data, columns_with_types = self._client.execute(sql, **kwargs)
        return NativeQueryResult(
            data,
            columns_with_types,
            column_oriented=column_oriented,
        )

    def query_df(self, sql: str) -> pd.DataFrame:
        return self._client.query_dataframe(sql)

    def query_df_stream(self, sql: str) -> AbstractContextManager[Iterator[pd.DataFrame]]:
        return _NativeDataFrameStream(self._client, sql)

    def insert_df(
        self,
        table: str,
        df: pd.DataFrame,
        column_names: Sequence[str],
    ) -> Any:
        query = _insert_query(table, column_names)
        rows = list(df.itertuples(index=False, name=None))
        return self._client.execute(query, rows)

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        column_type_names: Sequence[str] | None = None,
    ) -> Any:
        if column_type_names is not None and len(column_type_names) != len(column_names):
            message = "column_type_names must match column_names."
            raise ValueError(message)
        if column_type_names is not None:
            from .insert import normalize_typed_row  # noqa: PLC0415

            types = dict(zip(column_names, column_type_names))
            data = [normalize_typed_row(column_names, row, types) for row in data]
        return self._client.execute(_insert_query(table, column_names), data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.disconnect_connection()


def _split_first_stream_block(
    first: Any,
) -> tuple[Sequence[Sequence[Any]], Sequence[tuple[str, str]]]:
    if not isinstance(first, (list, tuple)):
        raise TypeError(_INVALID_STREAM_METADATA)

    # Test doubles and older driver adapters commonly expose (rows, metadata).
    if len(first) == _METADATA_PAIR_LENGTH and _is_column_metadata(first[1]):
        return first[0], first[1]

    # clickhouse-driver wraps IterQueryResult in chunk_size batches.  Its first
    # inner block starts with metadata, followed by that block's rows.
    outer = list(first)
    if outer and isinstance(outer[0], (list, tuple)):
        first_block = list(outer[0])
        if first_block and _is_column_metadata(first_block[0]):
            rows = list(first_block[1:])
            for block in outer[1:]:
                rows.extend(_normalize_stream_rows(block))
            return rows, first_block[0]

    # A non-chunked IterQueryResult has metadata as its first item.
    if outer and _is_column_metadata(outer[0]):
        return outer[1:], outer[0]
    raise ValueError(_INVALID_STREAM_METADATA)


def _normalize_stream_rows(value: Any) -> list[Sequence[Any]]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(_INVALID_STREAM_ROWS)
    items = list(value)
    if not items:
        return []
    if isinstance(items[0], tuple):
        return items
    rows: list[Sequence[Any]] = []
    for block in items:
        if not isinstance(block, (list, tuple)):
            raise TypeError(_INVALID_STREAM_ROWS)
        rows.extend(block)
    return rows


def _is_column_metadata(value: Any) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    return all(
        isinstance(column, (list, tuple))
        and len(column) == _METADATA_PAIR_LENGTH
        and isinstance(column[0], str)
        and isinstance(column[1], str)
        for column in value
    )


def _insert_query(table: str, column_names: Sequence[str]) -> str:
    parsed = _parse_table_name(table, "clickhouse")
    parts = [part for part in (parsed.catalog, parsed.db, parsed.name) if part]
    quoted_table = ".".join(quote_identifier(part, "ch") for part in parts)
    quoted_columns = ", ".join(quote_identifier(column, "ch") for column in column_names)
    return f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES"
