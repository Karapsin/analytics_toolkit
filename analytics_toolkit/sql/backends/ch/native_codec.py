"""Connection-local exact native decoding; the driver's global factories stay intact."""

from __future__ import annotations

from types import MethodType
from typing import Any

from .exact_temporal import datetime_from_seconds, timestamp_from_ticks, timestamp_ticks
from .temporal_context import native_context_timezone


def _temporal_timezone(column: Any) -> Any:
    if column.timezone is not None or not column.offset_naive:
        return column.timezone
    # The driver omits an explicit tzinfo when local and server zones agree.
    # That still denotes local/server wallclock fields, rather than UTC fields.
    return native_context_timezone(column.context)


def _datetime_items(column: Any, items: Any, nulls_map: Any = None) -> tuple[Any, ...]:
    if getattr(column.context, "_atk_exact_source_values", False):
        return _raw_temporal_items(items, column.scale, nulls_map)
    timezone = _temporal_timezone(column)
    return tuple(
        None
        if nulls_map is not None and nulls_map[index]
        else timestamp_from_ticks(value, column.scale, timezone, offset_naive=column.offset_naive)
        for index, value in enumerate(items)
    )


def _datetime_seconds_items(column: Any, items: Any, nulls_map: Any = None) -> tuple[Any, ...]:
    if getattr(column.context, "_atk_exact_source_values", False):
        return _raw_temporal_items(items, 0, nulls_map)
    timezone = _temporal_timezone(column)
    return tuple(
        None
        if nulls_map is not None and nulls_map[index]
        else datetime_from_seconds(
            value,
            timezone,
            offset_naive=column.offset_naive,
        )
        for index, value in enumerate(items)
    )


def _raw_temporal_items(items: Any, scale: int, nulls_map: Any = None) -> tuple[Any, ...]:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        ExactTemporal,
    )

    return tuple(
        None
        if nulls_map is not None and nulls_map[index]
        else ExactTemporal(int(value), scale, "UTC")
        for index, value in enumerate(items)
    )


def _map_items(column: Any, n_items: int, buf: Any) -> list[Any]:
    from analytics_toolkit.sql.core.exact_values import OrderedMapPairs  # noqa: PLC0415

    if not n_items:
        return []
    offsets = column.offset_column.read_items(n_items, buf)
    keys = column.key_column.read_data(offsets[-1], buf)
    values = column.value_column.read_data(offsets[-1], buf)
    result = []
    begin = 0
    for end in offsets:
        pairs = tuple(zip(keys[begin:end], values[begin:end]))
        unique = dict(pairs)
        result.append(unique if len(unique) == len(pairs) else OrderedMapPairs(pairs))
        begin = end
    return result


def _datetime_write(column: Any, items: Any, nulls_map: Any = None) -> None:
    timezone = _temporal_timezone(column)
    scale = getattr(column, "scale", 0)
    for index, value in enumerate(items):
        items[index] = (
            column.null_value
            if nulls_map is not None and nulls_map[index]
            else timestamp_ticks(value, scale, timezone)
        )


def _prepare_column(column: Any) -> Any:
    from clickhouse_driver.columns.datetimecolumn import (  # noqa: PLC0415
        DateTime64Column,
        DateTimeColumn,
    )
    from clickhouse_driver.columns.mapcolumn import MapColumn  # noqa: PLC0415

    if isinstance(column, DateTime64Column):
        column.after_read_items = MethodType(_datetime_items, column)
    elif isinstance(column, DateTimeColumn):
        column.after_read_items = MethodType(_datetime_seconds_items, column)
    if isinstance(column, DateTimeColumn):
        column.before_write_items = MethodType(_datetime_write, column)
    if isinstance(column, MapColumn):
        column.read_items = MethodType(_map_items, column)
    for attribute in ("nested_column", "key_column", "value_column"):
        child = getattr(column, attribute, None)
        if child is not None:
            _prepare_column(child)
    for child in getattr(column, "nested_columns", ()):
        _prepare_column(child)
    return column


def read_column(  # noqa: PLR0913 -- matches the native driver's column reader contract
    context: Any,
    column_spec: str,
    n_items: int,
    buf: Any,
    *,
    use_numpy: Any = None,
    has_custom_serialization: bool = False,
) -> Any:
    from clickhouse_driver.columns.service import get_column_by_spec  # noqa: PLC0415

    del use_numpy
    column = _prepare_column(
        get_column_by_spec(
            column_spec,
            {"context": context, "has_custom_serialization": has_custom_serialization},
            use_numpy=False,
        )
    )
    column.read_state_prefix(buf)
    return column.read_data(n_items, buf)


def exact_block_stream(fin: Any, context: Any, *, compressed: bool = False) -> Any:
    from clickhouse_driver import defines  # noqa: PLC0415
    from clickhouse_driver.block import BlockInfo, ColumnOrientedBlock  # noqa: PLC0415
    from clickhouse_driver.reader import read_binary_str, read_binary_uint8  # noqa: PLC0415
    from clickhouse_driver.streams.native import BlockInputStream  # noqa: PLC0415
    from clickhouse_driver.varint import read_varint  # noqa: PLC0415

    base = BlockInputStream
    if compressed:
        from clickhouse_driver.streams.compressed import CompressedBlockInputStream  # noqa: PLC0415

        base = CompressedBlockInputStream

    class ExactBlockInputStream(base):  # type: ignore[misc, valid-type]
        def read(self, use_numpy: Any = None) -> Any:
            del use_numpy
            info = BlockInfo()
            revision = self.context.server_info.used_revision
            if revision >= defines.DBMS_MIN_REVISION_WITH_BLOCK_INFO:
                info.read(self.fin)
            n_columns, n_rows = read_varint(self.fin), read_varint(self.fin)
            columns, data = [], []
            for _index in range(n_columns):
                name, spec = read_binary_str(self.fin), read_binary_str(self.fin)
                columns.append((name, spec))
                custom = False
                if revision >= defines.DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION:
                    custom = bool(read_binary_uint8(self.fin))
                if n_rows:
                    data.append(
                        read_column(
                            self.context,
                            spec,
                            n_rows,
                            self.fin,
                            has_custom_serialization=custom,
                        )
                    )
            return ColumnOrientedBlock(columns_with_types=columns, data=data, info=info)

    return ExactBlockInputStream(fin, context)


def _connection_stream(connection: Any) -> Any:
    return exact_block_stream(
        connection.fin, connection.context, compressed=bool(connection.compression)
    )


def _receive_progress(connection: Any) -> Any:
    from clickhouse_driver import defines  # noqa: PLC0415

    progress = connection._atk_original_receive_progress()  # noqa: SLF001
    if connection.server_info.used_revision >= defines.DBMS_MIN_REVISION_WITH_CLIENT_WRITE_INFO:
        connection._atk_written_rows_known = True  # noqa: SLF001
    return progress


def _connection_output_stream(connection: Any) -> Any:
    from clickhouse_driver import defines  # noqa: PLC0415
    from clickhouse_driver.columns.service import get_column_by_spec  # noqa: PLC0415
    from clickhouse_driver.streams.native import BlockOutputStream  # noqa: PLC0415
    from clickhouse_driver.varint import write_varint  # noqa: PLC0415
    from clickhouse_driver.writer import write_binary_str, write_binary_uint8  # noqa: PLC0415

    base = BlockOutputStream
    if connection.compression:
        from clickhouse_driver.streams.compressed import (  # noqa: PLC0415
            CompressedBlockOutputStream,
        )

        base = CompressedBlockOutputStream

    class ExactBlockOutputStream(base):  # type: ignore[misc, valid-type]
        def write(self, block: Any) -> None:
            revision = self.context.server_info.used_revision
            if revision >= defines.DBMS_MIN_REVISION_WITH_BLOCK_INFO:
                block.info.write(self.fout)
            write_varint(block.num_columns, self.fout)
            write_varint(block.num_rows, self.fout)
            for index, (name, spec) in enumerate(block.columns_with_types):
                write_binary_str(name, self.fout)
                write_binary_str(spec, self.fout)
                if revision >= defines.DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION:
                    write_binary_uint8(0, self.fout)
                values = block.get_column_by_index(index)
                column = _prepare_column(
                    get_column_by_spec(
                        spec,
                        {"context": self.context, "types_check": block.types_check},
                        use_numpy=False,
                    )
                )
                # State prefixes for ordinary columns are independent of their values.
                if getattr(column, "prefix_needs_items", False):
                    column.write_state_prefix(self.fout, values)
                else:
                    column.write_state_prefix(self.fout)
                column.write_data(values, self.fout)
            self.finalize()

    if connection.compression:
        return ExactBlockOutputStream(
            connection.compressor_cls,
            connection.compress_block_size,
            connection.fout,
            connection.context,
        )
    return ExactBlockOutputStream(connection.fout, connection.context)


def install_exact_native_codec(client: Any) -> None:
    """Install once per connection; reconnects call this same stream factory."""
    connection = getattr(client, "connection", None)
    if connection is None:
        if type(client).__module__.startswith("clickhouse_driver"):
            message = "Incompatible clickhouse-driver exact-decoding interface."
            raise TypeError(message)
        # Protocol fakes used by callers do not instantiate driver connections.
        return
    from clickhouse_driver.result import (  # noqa: PLC0415
        IterQueryResult,
        ProgressQueryResult,
        QueryResult,
    )

    client.client_settings["use_numpy"] = False
    client.query_result_cls = QueryResult
    client.iter_query_result_cls = IterQueryResult
    client.progress_query_result_cls = ProgressQueryResult
    connections = [connection, *getattr(client, "connections", ())]
    for current in connections:
        if not callable(getattr(current, "get_block_in_stream", None)):
            message = "Incompatible clickhouse-driver exact-decoding interface."
            raise TypeError(message)
        current.get_block_in_stream = MethodType(_connection_stream, current)
        if not callable(getattr(current, "get_block_out_stream", None)):
            message = "Incompatible clickhouse-driver exact-encoding interface."
            raise TypeError(message)
        current.get_block_out_stream = MethodType(_connection_output_stream, current)
        if not hasattr(current, "_atk_original_receive_progress"):
            current._atk_original_receive_progress = current.receive_progress  # noqa: SLF001
            current.receive_progress = MethodType(_receive_progress, current)
        current._atk_written_rows_known = False  # noqa: SLF001
        if getattr(current, "block_in", None) is not None:
            current.block_in = current.get_block_in_stream()
        if getattr(current, "block_out", None) is not None:
            current.block_out = current.get_block_out_stream()
