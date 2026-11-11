"""Exact, non-NumPy HTTP native-format reads with connection-local codecs."""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from copy import copy
from struct import pack
from types import MethodType
from typing import Any

from analytics_toolkit.sql.dml.io.dataframes import dataframe_from_columns

from .exact_temporal import datetime_from_seconds, timestamp_from_ticks, timestamp_ticks
from .temporal_context import timezone_name

_LOGGER = logging.getLogger(__name__)
_DATETIME_MAX_TICKS = 2**32 - 1
_DATETIME64_MIN_TICKS = -(2**63)
_DATETIME64_MAX_TICKS = 2**63 - 1


class _SafeHTTPSource:
    """Byte-source cleanup cannot change the established query outcome."""

    def __init__(self, source: Any) -> None:
        self._source = source
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._source.close()
        except Exception as close_error:  # noqa: BLE001 -- cleanup preserves the query outcome
            _LOGGER.warning("HTTP result source close failed: %s", type(close_error).__name__)


def _datetime_column(
    datatype: Any, source: Any, count: int, context: Any, _state: Any = None
) -> Any:
    ticks = source.read_array("q", count)
    if getattr(datatype, "_atk_exact_source_values", False):
        return _raw_temporal_values(ticks, datatype.scale)
    active = context.active_tz
    timezone = datatype.tzinfo or (active(datatype.tzinfo) if callable(active) else active)
    return [timestamp_from_ticks(tick, datatype.scale, timezone) for tick in ticks]


def _datetime_seconds_column(
    datatype: Any,
    source: Any,
    count: int,
    context: Any,
    _state: Any = None,
) -> Any:
    ticks = source.read_array("I", count)
    if getattr(datatype, "_atk_exact_source_values", False):
        return _raw_temporal_values(ticks, 0)
    if datatype.read_format(context) == "int":
        return ticks
    active = context.active_tz
    timezone = active(datatype.tzinfo) if callable(active) else (active or datatype.tzinfo)
    return [datetime_from_seconds(value, timezone) for value in ticks]


def _raw_temporal_values(ticks: Any, scale: int) -> list[Any]:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        ExactTemporal,
    )

    return [ExactTemporal(int(value), scale, "UTC") for value in ticks]


def _raw_column_finalizer(_datatype: Any, column: Any, _context: Any) -> Any:
    return column


def _timestamp_write_values(datatype: Any, values: Any, scale: int) -> list[int]:
    timezone = datatype.tzinfo or getattr(datatype, "_atk_default_timezone", None)
    if not datatype.nullable and any(value is None for value in values):
        message = "SQL NULL is not writable to a non-nullable ClickHouse timestamp."
        raise ValueError(message)
    return [0 if value is None else timestamp_ticks(value, scale, timezone) for value in values]


def _datetime_write(datatype: Any, values: Any, dest: Any, context: Any) -> None:
    del context
    ticks = _timestamp_write_values(datatype, values, datatype.scale)
    if any(value < _DATETIME64_MIN_TICKS or value > _DATETIME64_MAX_TICKS for value in ticks):
        message = "Value is outside ClickHouse DateTime64's Int64 timestamp range."
        raise ValueError(message)
    dest.extend(pack(f"<{len(ticks)}q", *ticks))


def _datetime_seconds_write(datatype: Any, values: Any, dest: Any, context: Any) -> None:
    del context
    ticks = _timestamp_write_values(datatype, values, 0)
    if any(value < 0 or value > _DATETIME_MAX_TICKS for value in ticks):
        message = "Value is outside ClickHouse DateTime's UInt32 timestamp range."
        raise ValueError(message)
    dest.extend(pack(f"<{len(ticks)}I", *ticks))


def _map_column(datatype: Any, source: Any, count: int, context: Any, state: Any = None) -> Any:
    from analytics_toolkit.sql.core.exact_values import OrderedMapPairs  # noqa: PLC0415

    offsets = source.read_array("Q", count)
    total = offsets[-1] if offsets else 0
    if state is None:
        # clickhouse-connect before stateful nested prefixes (supported 0.5.x).
        keys = datatype.key_type.read_column_data(source, total, context)
        values = datatype.value_type.read_column_data(source, total, context)
    else:
        keys = datatype.key_type.read_column_data(source, total, context, state[0])
        values = datatype.value_type.read_column_data(source, total, context, state[1])
    result, begin = [], 0
    for end in offsets:
        pairs = tuple(zip(keys[begin:end], values[begin:end]))
        unique = dict(pairs)
        result.append(unique if len(unique) == len(pairs) else OrderedMapPairs(pairs))
        begin = end
    return result


def exact_http_type(
    datatype: Any,
    *,
    default_timezone: Any = None,
    exact_source_values: bool = False,
) -> Any:
    """Clone registry types before customizing so unrelated clients are untouched."""
    from clickhouse_connect.datatypes.container import Map  # noqa: PLC0415
    from clickhouse_connect.datatypes.temporal import DateTime, DateTime64  # noqa: PLC0415

    datatype = copy(datatype)
    if isinstance(datatype, (DateTime, DateTime64)):
        datatype._atk_default_timezone = default_timezone  # noqa: SLF001 -- per-client implicit zone
        datatype._atk_exact_source_values = exact_source_values  # noqa: SLF001 -- connection-local source policy
        if exact_source_values:
            datatype._finalize_column = MethodType(_raw_column_finalizer, datatype)  # noqa: SLF001
    if isinstance(datatype, DateTime):
        datatype._read_column_binary = MethodType(_datetime_seconds_column, datatype)  # noqa: SLF001
        datatype._write_column_binary = MethodType(_datetime_seconds_write, datatype)  # noqa: SLF001
    if isinstance(datatype, DateTime64):
        datatype._read_column_binary = MethodType(_datetime_column, datatype)  # noqa: SLF001
        datatype._write_column_binary = MethodType(_datetime_write, datatype)  # noqa: SLF001
    if isinstance(datatype, Map):
        datatype.read_column_data = MethodType(_map_column, datatype)
    for attribute in ("element_type", "key_type", "value_type"):
        child = getattr(datatype, attribute, None)
        if child is not None:
            setattr(
                datatype,
                attribute,
                exact_http_type(
                    child,
                    default_timezone=default_timezone,
                    exact_source_values=exact_source_values,
                ),
            )
    children = getattr(datatype, "element_types", None)
    if children is not None:
        datatype.element_types = [
            exact_http_type(
                child, default_timezone=default_timezone, exact_source_values=exact_source_values
            )
            for child in children
        ]
    return datatype


class ExactHTTPTransform:
    def __init__(self, original: Any, *, default_timezone: Any = None) -> None:
        self._original = original
        self._default_timezone = default_timezone
        self.read_timezone: str | None = None
        self.exact_source_values = False

    def build_insert(self, context: Any) -> Any:
        context.column_types = [
            exact_http_type(datatype, default_timezone=self._default_timezone)
            for datatype in context.column_types
        ]
        return self._original.build_insert(context)

    def parse_response(self, source: Any, context: Any) -> Any:  # noqa: C901 -- native block framing owns decode errors and cleanup
        from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
        from clickhouse_connect.driver import transform  # noqa: PLC0415
        from clickhouse_connect.driver.exceptions import (  # noqa: PLC0415
            StreamCompleteException,
            StreamFailureError,
        )
        from clickhouse_connect.driver.query import QueryResult  # noqa: PLC0415

        if context.use_numpy:
            message = "Toolkit exact ClickHouse reads require non-NumPy column blocks."
            raise ValueError(message)
        active = context.active_tz
        read_zone = active(None) if callable(active) else active
        self.read_timezone = (
            "UTC"
            if self.exact_source_values
            else (timezone_name(read_zone) if read_zone is not None else "UTC")
        )
        names, types = [], []
        first = True
        owned_source = _SafeHTTPSource(source)

        def block() -> Any:
            nonlocal first
            try:
                try:
                    if context.block_info:
                        source.read_bytes(8)
                    count = source.read_leb128()
                except StreamCompleteException:
                    tag = getattr(source, "exception_tag", None)
                    extract = getattr(transform, "extract_exception_with_tag", None)
                    if tag and extract:
                        message = extract(source.last_message, tag)
                        if message:
                            raise StreamFailureError(message) from None
                    return None
                rows = source.read_leb128()
                columns = []
                for index in range(count):
                    name, spec = source.read_leb128_str(), source.read_leb128_str()
                    if first:
                        renamer = getattr(context, "column_renamer", None)
                        names.append(renamer(name) if renamer else name)
                        types.append(
                            exact_http_type(
                                get_from_name(spec),
                                exact_source_values=self.exact_source_values,
                            )
                        )
                    context.start_column(name)
                    columns.append(types[index].read_column(source, rows, context) if rows else ())
                first = False
            except BaseException:
                owned_source.close()
                raise
            else:
                return columns

        initial = block()
        if initial is None:
            owned_source.close()
            return QueryResult([])

        def blocks() -> Any:
            yield initial
            while True:
                current = block()
                if current is None:
                    return
                yield current

        return QueryResult(
            None, blocks(), tuple(names), tuple(types), context.column_oriented, owned_source
        )


class ExactHTTPClient:
    """Preserve the usual client surface while avoiding its lossy dataframe path."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._write_timezone = getattr(client, "server_tz", None)
        self._read_transform: ExactHTTPTransform | None = None
        transform = getattr(client, "_transform", None)
        if transform is not None:
            if not callable(getattr(transform, "parse_response", None)):
                message = "Incompatible clickhouse-connect exact-decoding interface."
                raise RuntimeError(message)
            self._read_transform = ExactHTTPTransform(
                transform, default_timezone=self._write_timezone
            )
            client._transform = self._read_transform  # noqa: SLF001
        elif type(client).__module__.startswith("clickhouse_connect"):
            message = "Incompatible clickhouse-connect exact-decoding interface."
            raise RuntimeError(message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    @property
    def effective_write_timezone(self) -> str | None:
        return timezone_name(self._write_timezone)

    @property
    def effective_read_timezone(self) -> str | None:
        if self._read_transform is not None and self._read_transform.exact_source_values:
            return "UTC"
        return self._read_transform.read_timezone if self._read_transform is not None else None

    def enable_exact_source_values(self) -> None:
        """Preserve source raw ticks in this client, for eager and streaming reads."""
        if self._read_transform is not None:
            self._read_transform.exact_source_values = True

    def query(self, sql: str, **kwargs: Any) -> Any:
        kwargs["use_numpy"] = False
        return self._client.query(sql, **kwargs)

    def query_df(self, sql: str, **kwargs: Any) -> Any:
        result = self.query(sql, column_oriented=True, **kwargs)
        return dataframe_from_columns(
            result.column_names, result.result_columns, row_count=result.row_count
        )

    def insert_df(self, table: str, df: Any, column_names: Any = None, **kwargs: Any) -> Any:
        # InsertContext's dataframe path asks DateTime64 for a NumPy dtype even
        # for object columns. Row values avoid that unsupported-scale path.
        return self._client.insert(
            table,
            list(df.itertuples(index=False, name=None)),
            column_names=list(df.columns) if column_names is None else column_names,
            **kwargs,
        )

    @contextmanager
    def query_df_stream(self, sql: str, **kwargs: Any) -> Any:
        context = self._client.query_column_block_stream(sql, **kwargs)
        blocks = context.__enter__()
        try:
            result = context.source

            def frames() -> Any:
                for columns in blocks:
                    rows = len(columns[0]) if columns else 0
                    if rows:
                        yield dataframe_from_columns(result.column_names, columns, row_count=rows)

            yield frames()
        except BaseException:
            self._close_stream(context, sys.exc_info())
            raise
        else:
            self._close_stream(context, (None, None, None))

    @staticmethod
    def _close_stream(context: Any, exception: Any) -> None:
        try:
            context.__exit__(*exception)
        except Exception as close_error:  # noqa: BLE001 -- cleanup preserves the query outcome
            _LOGGER.warning("HTTP stream close failed: %s", type(close_error).__name__)
