"""Actual native-format NaN, SQL NULL and signed-zero source columns."""

from __future__ import annotations

import math
from io import BytesIO
from struct import pack
from types import SimpleNamespace
from typing import Any

from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.native_codec import exact_block_stream

from tests.sql.backends.ch._support.exact_transport import (
    BinaryNativeSource,
    http_source,
    native_context,
)


def assert_native_float_values(values: Any) -> None:
    assert isinstance(values[0], float)
    assert math.isnan(values[0])
    assert values[1] is None
    assert values[2] == 0.0
    assert math.copysign(1.0, values[2]) == -1.0


def float_binary(*, nested: bool) -> tuple[str, bytes]:
    from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

    spec = "Array(Nullable(Float64))" if nested else "Nullable(Float64)"
    output = BytesIO(bytes([1, 1 if nested else 3]))
    output.seek(2)
    write_binary_str("id", output)
    write_binary_str(spec, output)
    if nested:
        output.write(pack("<Q", 3))
    output.write(b"\x00\x01\x00" + pack("<3d", float("nan"), 0.0, -0.0))
    return spec, output.getvalue()


class FloatHTTPSource:
    def __init__(self, binary: bytes) -> None:
        from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

        self._transform = NativeTransform()
        self.binary = binary
        self.query_count = 0
        self.close_calls = 0

    def query(self, _statement: str, **kwargs: Any) -> Any:
        from clickhouse_connect.driver.query import QueryContext  # noqa: PLC0415

        self.query_count += 1
        context = QueryContext(use_none=True, column_oriented=kwargs.get("column_oriented", False))
        return self._transform.parse_response(http_source(self.binary), context)

    def query_column_block_stream(self, statement: str, **_kwargs: Any) -> Any:
        return self.query(statement, column_oriented=True).column_block_stream

    def close(self) -> None:
        self.close_calls += 1


def float_client(transport: str, *, nested: bool) -> tuple[Any, Any, str]:
    spec, binary = float_binary(nested=nested)
    if transport == "http":
        raw = FloatHTTPSource(binary)
        return ExactHTTPClient(raw), raw, spec

    from clickhouse_driver import Client  # noqa: PLC0415

    raw = Client("127.0.0.1")
    raw.connection.context.server_info = native_context().server_info
    state = SimpleNamespace(query_count=0, close_calls=0)
    client = NativeClickHouseClient(raw)

    def block() -> Any:
        state.query_count += 1
        return exact_block_stream(BinaryNativeSource(binary), raw.connection.context).read()

    def execute(_statement: str, **kwargs: Any) -> Any:
        result = block()
        data = result.get_columns() if kwargs.get("columnar") else result.get_rows()
        return data, result.columns_with_types

    def execute_iter(_statement: str, **_kwargs: Any) -> Any:
        raw.packet_generator = lambda: iter([SimpleNamespace(block=block())])
        return raw.iter_receive_result(with_column_types=True)

    def close() -> None:
        state.close_calls += 1

    raw.execute = execute
    raw.execute_iter = execute_iter
    raw.disconnect_connection = close
    return client, state, spec
