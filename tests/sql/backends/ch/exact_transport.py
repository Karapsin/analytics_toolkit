from __future__ import annotations

import struct
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.http_codec import (
    ExactHTTPClient,
    ExactHTTPTransform,
    exact_http_type,
)
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.native_codec import exact_block_stream, read_column
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

from tests.sql.backends.ch._support.exact_transport import (
    BinaryNativeSource,
    http_query_context,
    http_source,
    native_context,
)


@pytest.mark.parametrize("scale", range(10))
@pytest.mark.parametrize("zone", [None, "UTC", "Europe/Moscow"])
def test_binary_temporal_ticks_remain_exact_for_both_transports(
    scale: int, zone: str | None
) -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    suffix = f", '{zone}'" if zone else ""
    spec = f"DateTime64({scale}{suffix})"
    ticks = [-1, 0, 1, 1_700_000_000 * 10**scale + (10**scale - 1)]
    payload = struct.pack("<4q", *ticks)
    native = read_column(native_context(), spec, len(ticks), BytesIO(payload))
    datatype = exact_http_type(get_from_name(spec))
    http = datatype.read_column(
        http_source(payload), len(ticks), http_query_context(use_numpy=False)
    )
    for actual in (native, http):
        for tick, value in zip(ticks, actual):
            assert isinstance(value, pd.Timestamp)
            assert value.value == tick * 10 ** (9 - scale)
            assert (value.tz is not None) == (zone is not None)


@pytest.mark.parametrize(
    ("spec", "payload", "expected"),
    [
        (
            "Nullable(DateTime64(9))",
            b"\x00\x01\x00" + struct.pack("<3q", -1, 0, 1),
            [pd.Timestamp(-1), None, pd.Timestamp(1)],
        ),
        (
            "Array(DateTime64(9))",
            struct.pack("<2Q3q", 2, 3, -1, 0, 1),
            [[pd.Timestamp(-1), pd.Timestamp(0)], [pd.Timestamp(1)]],
        ),
        ("Date32", struct.pack("<i", -25567), [date(1900, 1, 1)]),
        (
            "Tuple(DateTime64(9), Array(Nullable(DateTime64(9))))",
            struct.pack("<qQ", -1, 2) + b"\x00\x01" + struct.pack("<2q", 2, 0),
            [(pd.Timestamp(-1), [pd.Timestamp(2), None])],
        ),
    ],
)
def test_binary_nested_temporal_and_signed_date32(spec: str, payload: bytes, expected: Any) -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    count = 3 if spec.startswith("Nullable") else (2 if spec.startswith("Array") else 1)
    assert list(read_column(native_context(), spec, count, BytesIO(payload))) == expected
    datatype = exact_http_type(get_from_name(spec))
    assert (
        list(datatype.read_column(http_source(payload), count, http_query_context(use_none=True)))
        == expected
    )


def test_binary_map_keeps_duplicate_keys_without_changing_unique_maps() -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    payload = struct.pack("<2Q3q3q", 2, 3, 1, 1, 2, -1, 1, 2)
    spec = "Map(Int64, DateTime64(9))"
    native = read_column(native_context(), spec, 2, BytesIO(payload))
    datatype = exact_http_type(get_from_name(spec))
    http = datatype.read_column(http_source(payload), 2, http_query_context())
    for values in (native, http):
        assert values[0] == OrderedMapPairs(((1, pd.Timestamp(-1)), (1, pd.Timestamp(1))))
        assert values[1] == {2: pd.Timestamp(2)}


def test_native_stream_header_is_consumed_once_and_tuple_cells_are_rows() -> None:
    class Raw:
        disconnected = 0

        def execute_iter(self, _sql: str, **kwargs: Any) -> Any:
            assert kwargs == {"with_column_types": True, "chunk_size": 1}
            yield [("same", "Tuple(String, String)"), ("same", "Nullable(UInt64)")]
            yield (("metadata", "String"), None)
            yield (("other", "Int64"), 2**64 - 1)

        def disconnect_connection(self) -> None:
            self.disconnected += 1

    raw = Raw()
    with NativeClickHouseClient(raw).query_df_stream("SELECT tuple, value") as frames:
        frame = next(frames)
        assert list(frame.columns) == ["same", "same"]
        assert frame.iloc[0, 0] == ("metadata", "String")
        assert frame.iloc[1, 1] == 2**64 - 1
        assert list(frames) == []
    assert raw.disconnected == 0


def test_real_driver_iter_query_result_header_contract() -> None:
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415

    blocks = [ColumnOrientedBlock([("value", "Int64")], [[1, 2]])]
    client = Client("127.0.0.1")
    client.packet_generator = lambda: (SimpleNamespace(block=block) for block in blocks)
    assert list(client.iter_receive_result(with_column_types=True)) == [
        [("value", "Int64")],
        (1,),
        (2,),
    ]


def test_native_connection_factory_survives_reconnect_without_global_patch() -> None:
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.connection import Connection  # noqa: PLC0415

    raw = Client("127.0.0.1")
    ordinary = Connection.get_block_in_stream
    NativeClickHouseClient(raw)
    assert Connection.get_block_in_stream is ordinary
    raw.connection.fin = BinaryNativeSource(b"\x00\x00")
    for _attempt in range(2):
        raw.connection.context.server_info = SimpleNamespace(used_revision=0)
        raw.connection.fin.seek(0)
        stream = raw.connection.get_block_in_stream()
        assert stream.read().columns_with_types == []


def test_full_binary_native_block_uses_exact_column_factory() -> None:
    from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

    output = BytesIO(b"\x01\x01")
    output.seek(2)
    write_binary_str("value", output)
    write_binary_str("DateTime64(9)", output)
    output.write(struct.pack("<q", 1_700_000_000_123_456_789))
    output.seek(0)
    block = exact_block_stream(BinaryNativeSource(output.getvalue()), native_context()).read()
    assert block.get_rows() == [(pd.Timestamp(1_700_000_000_123_456_789),)]


def test_http_stream_uses_column_blocks_and_preserves_duplicate_names() -> None:
    from clickhouse_connect.driver.query import QueryResult  # noqa: PLC0415

    class Raw:
        def query_column_block_stream(self, sql: str) -> Any:
            assert sql == "SELECT precise"
            result = QueryResult(None, iter([[[pd.Timestamp(1)], [2**64 - 1]]]), ("x", "x"))
            return result.column_block_stream

    with ExactHTTPClient(Raw()).query_df_stream("SELECT precise") as frames:
        frame = next(frames)
        assert list(frame.columns) == ["x", "x"]
        assert frame.iloc[0, 0].value == 1
        assert frame.iloc[0, 1] == 2**64 - 1


@pytest.mark.parametrize("scale", range(10))
def test_binary_temporal_writes_preserve_exact_ticks(scale: int) -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.insert import InsertContext  # noqa: PLC0415
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415

    ticks = [-1, 1, 1_700_000_000 * 10**scale + (10**scale - 1)]
    unit_precision, unit = next(
        (p, u) for p, u in ((0, "s"), (3, "ms"), (6, "us"), (9, "ns")) if p >= scale
    )
    values = [pd.Timestamp(tick * 10 ** (unit_precision - scale), unit=unit) for tick in ticks]
    spec = f"DateTime64({scale})"
    datatype = exact_http_type(get_from_name(spec))
    output = bytearray()
    context = InsertContext("target", ["value"], [datatype])
    datatype.write_column(values, output, context)
    assert bytes(output) == struct.pack("<3q", *ticks)

    raw = Client("127.0.0.1")
    NativeClickHouseClient(raw)
    raw.connection.context.server_info = native_context().server_info
    raw.connection.fout = BytesIO()
    raw.connection.get_block_out_stream().write(ColumnOrientedBlock([("value", spec)], [values]))
    assert raw.connection.fout.getvalue()[-24:] == struct.pack("<3q", *ticks)


@pytest.mark.parametrize("python_buffer", [False, True])
def test_http_native_binary_result_preserves_all_output_forms(python_buffer: bool) -> None:
    from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

    def binary_string(value: str) -> bytes:
        encoded = value.encode()
        return bytes([len(encoded)]) + encoded

    payload = (
        b"\x02\x01"
        + binary_string("x")
        + binary_string("DateTime64(9)")
        + struct.pack("<q", 1)
        + binary_string("x")
        + binary_string("UInt64")
        + struct.pack("<Q", 2**64 - 1)
    )
    for column_oriented in (True, False):
        context = http_query_context(column_oriented=column_oriented)
        result = ExactHTTPTransform(NativeTransform()).parse_response(
            http_source(payload, python_buffer=python_buffer),
            context,
        )
        assert result.column_names == ("x", "x")
        expected = (
            [[pd.Timestamp(1)], [2**64 - 1]] if column_oriented else [(pd.Timestamp(1), 2**64 - 1)]
        )
        assert result.result_set == expected


@pytest.mark.parametrize("compression", ["lz4", "zstd"])
def test_native_compressed_binary_blocks_keep_temporal_ticks(compression: str) -> None:
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415

    raw = Client("127.0.0.1", compression=compression)
    NativeClickHouseClient(raw)
    raw.connection.context.server_info = native_context().server_info
    raw.connection.fout = BytesIO()
    timestamp = pd.Timestamp(-1)
    raw.connection.get_block_out_stream().write(
        ColumnOrientedBlock([("x", "DateTime64(9)")], [[timestamp]])
    )
    binary = raw.connection.fout.getvalue()
    stream = exact_block_stream(BinaryNativeSource(binary), native_context(), compressed=True)
    assert stream.read().get_rows() == [(timestamp,)]


def test_native_stream_batches_driver_rows_locally() -> None:
    class Raw:
        def execute_iter(self, _sql: str, **kwargs: Any) -> Any:
            assert kwargs["chunk_size"] == 1
            yield [("x", "UInt64")]
            yield from ((value,) for value in range(65_537))

        def disconnect_connection(self) -> None:
            pytest.fail("A fully consumed stream must not disconnect.")

    with NativeClickHouseClient(Raw()).query_df_stream("SELECT x") as frames:
        batches = list(frames)
    assert [len(frame) for frame in batches] == [65_536, 1]
    assert batches[1].iloc[0, 0] == 65_536
