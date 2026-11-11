from __future__ import annotations

import struct
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.native_codec import exact_block_stream
from analytics_toolkit.sql.backends.ch.temporal_context import effective_read_timezone
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from zoneinfo import ZoneInfo

from tests.sql.backends.ch._support.exact_transport import (
    BinaryNativeSource,
    http_query_context,
    http_source,
    native_context,
)

_FIRST = 1_730_622_600
_SECOND = 1_730_626_200
_SPECS = [
    ("dt", "DateTime"),
    ("precise", "DateTime64(9)"),
    ("nested", "Array(Tuple(DateTime64(9), Nullable(DateTime)))"),
]


def binary_source(*, empty: bool = False) -> bytes:
    from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

    output = BytesIO(bytes([len(_SPECS), 0 if empty else 2]))
    output.seek(2)
    data = [
        struct.pack("<2I", _FIRST, _SECOND),
        struct.pack("<2q", _FIRST * 10**9 + 123, _SECOND * 10**9 + 456),
        struct.pack("<2Q2q", 1, 2, _FIRST * 10**9 + 123, _SECOND * 10**9 + 456)
        + b"\x00\x01"
        + struct.pack("<2I", _FIRST, 0),
    ]
    for (name, spec), values in zip(_SPECS, data):
        write_binary_str(name, output)
        write_binary_str(spec, output)
        if not empty:
            output.write(values)
    return output.getvalue()


class NativeBinary:
    def __init__(self, binary: bytes) -> None:
        from clickhouse_driver import Client  # noqa: PLC0415

        self.raw = Client("127.0.0.1", alt_hosts="127.0.0.2:9001", round_robin=True)
        self.binary = binary
        for connection in [self.raw.connection, *self.raw.connections]:
            connection.context.server_info = native_context().server_info
            connection.context.server_info.get_timezone = lambda: "America/Los_Angeles"
        self.client = NativeClickHouseClient(self.raw)
        self.raw.execute = self.execute
        self.raw.execute_iter = self.execute_iter

    def block(self) -> Any:
        return exact_block_stream(
            BinaryNativeSource(self.binary), self.raw.connection.context
        ).read()

    def execute(self, _query: str, **kwargs: Any) -> Any:
        block = self.block()
        data = block.get_columns() if kwargs.get("columnar") else block.get_rows()
        return data, block.columns_with_types

    def execute_iter(self, _query: str, **kwargs: Any) -> Any:
        assert kwargs == {"with_column_types": True, "chunk_size": 1}
        self.raw.packet_generator = lambda: iter([SimpleNamespace(block=self.block())])
        return self.raw.iter_receive_result(with_column_types=True)


class HTTPBinary:
    server_tz = ZoneInfo("America/Los_Angeles")

    def __init__(self, binary: bytes, *, python_buffer: bool) -> None:
        from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

        self._transform = NativeTransform()
        self.binary = binary
        self.python_buffer = python_buffer

    def query(self, _query: str, **kwargs: Any) -> Any:
        context = http_query_context(column_oriented=kwargs.get("column_oriented", False))
        return self._transform.parse_response(
            http_source(self.binary, python_buffer=self.python_buffer),
            context,
        )

    def query_column_block_stream(self, query: str, **_kwargs: Any) -> Any:
        return self.query(query, column_oriented=True).column_block_stream


def assert_raw_frame(frame: Any) -> None:
    first, second = ExactTemporal(_FIRST, 0, "UTC"), ExactTemporal(_SECOND, 0, "UTC")
    assert list(frame.columns) == ["dt", "precise", "nested"]
    assert frame.iloc[0, 0] == first
    assert frame.iloc[1, 0] == second
    assert frame.iloc[0, 1] == ExactTemporal(_FIRST * 10**9 + 123, 9, "UTC")
    assert frame.iloc[0, 2] == [(ExactTemporal(_FIRST * 10**9 + 123, 9, "UTC"), first)]
    assert frame.iloc[1, 2] == [(ExactTemporal(_SECOND * 10**9 + 456, 9, "UTC"), None)]
    assert first.calendar_datetime().astimezone(ZoneInfo("America/Los_Angeles")).hour == 1
    assert second.calendar_datetime().astimezone(ZoneInfo("America/Los_Angeles")).hour == 1


def test_native_exact_source_eager_stream_nested_fold_and_reconnect() -> None:
    binary = NativeBinary(binary_source())
    public = binary.client.query_df("SELECT values")
    assert not isinstance(public.iloc[0, 0], ExactTemporal)
    assert public.iloc[0, 0] == public.iloc[1, 0]  # same wall clock, distinct raw instants
    binary.raw.connections.append(SimpleNamespace(context=None))
    binary.client.enable_exact_source_values()
    binary.raw.connections.pop()
    binary.client.enable_exact_source_values()
    assert effective_read_timezone(binary.client) == "UTC"
    assert effective_read_timezone(binary.raw) == "UTC"
    assert_raw_frame(binary.client.query_df("SELECT values"))
    with binary.client.query_df_stream("SELECT values") as frames:
        assert_raw_frame(next(frames))
        assert list(frames) == []
    previous_context = binary.raw.connection.context
    binary.raw.disconnect_connection()
    assert binary.raw.connection.context is previous_context
    binary.raw.connection.context.server_info = native_context().server_info
    binary.raw.connection.fin = BinaryNativeSource(binary.binary)
    assert isinstance(
        binary.raw.connection.get_block_in_stream().read().get_rows()[0][0], ExactTemporal
    )
    binary.raw.connection = binary.raw.get_connection()
    assert_raw_frame(binary.client.query_df("SELECT values"))
    fresh = NativeBinary(binary_source())
    assert not isinstance(fresh.client.query_df("SELECT values").iloc[0, 0], ExactTemporal)
    assert effective_read_timezone(fresh.raw) == "America/Los_Angeles"
    assert effective_read_timezone(object()) is None


@pytest.mark.parametrize("python_buffer", [False, True])
def test_http_exact_source_eager_stream_nested_fold_and_client_isolation(
    python_buffer: bool,
) -> None:
    client = ExactHTTPClient(HTTPBinary(binary_source(), python_buffer=python_buffer))
    assert effective_read_timezone(client) is None
    assert not isinstance(client.query_df("SELECT values").iloc[0, 0], ExactTemporal)
    assert effective_read_timezone(client) == "UTC"
    client._read_transform.read_timezone = None
    assert effective_read_timezone(client) is None
    client.enable_exact_source_values()
    assert effective_read_timezone(client) == "UTC"
    assert_raw_frame(client.query_df("SELECT values"))
    with client.query_df_stream("SELECT values") as frames:
        assert_raw_frame(next(frames))
        assert list(frames) == []
    fresh = ExactHTTPClient(HTTPBinary(binary_source(), python_buffer=python_buffer))
    assert not isinstance(fresh.query_df("SELECT values").iloc[0, 0], ExactTemporal)


@pytest.mark.parametrize("transport", ["native", "http"])
def test_exact_source_metadata_only_results_keep_header_and_empty_stream(transport: str) -> None:
    client = (
        NativeBinary(binary_source(empty=True)).client
        if transport == "native"
        else ExactHTTPClient(HTTPBinary(binary_source(empty=True), python_buffer=False))
    )
    client.enable_exact_source_values()
    frame = client.query_df("SELECT empty")
    assert list(frame.columns) == ["dt", "precise", "nested"]
    assert frame.empty
    with client.query_df_stream("SELECT empty") as frames:
        assert list(frames) == []
