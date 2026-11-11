from __future__ import annotations

import struct
from io import BytesIO
from typing import Any

import pytest
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

from tests.sql.backends.ch.temporal_source_mode import HTTPBinary, NativeBinary


class NativeOutput(BytesIO):
    def write_strings(self, values: Any, encoding: str = "utf-8") -> None:
        from clickhouse_driver.writer import write_binary_bytes  # noqa: PLC0415

        for value in values:
            write_binary_bytes(value.encode(encoding), self)


def array_key_map_block() -> tuple[bytes, bytes]:
    from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

    # One map, two keys: [1] and [2, 3], with String values "a" and "b".
    payload = struct.pack("<3Q3q", 2, 1, 3, 1, 2, 3) + b"\x01a\x01b"
    output = BytesIO(b"\x01\x01")
    output.seek(2)
    write_binary_str("mapping", output)
    write_binary_str("Map(Array(Int64), String)", output)
    output.write(payload)
    return output.getvalue(), payload


@pytest.mark.parametrize("transport", ["native", "http_python", "http_compiled"])
@pytest.mark.parametrize("exact_source", [False, True])
def test_array_map_keys_survive_binary_eager_stream_and_write_roundtrip(
    transport: str, exact_source: bool
) -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.insert import InsertContext  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415

    binary, payload = array_key_map_block()
    native = NativeBinary(binary) if transport == "native" else None
    client: Any = (
        native.client
        if native is not None
        else ExactHTTPClient(HTTPBinary(binary, python_buffer=transport == "http_python"))
    )
    if exact_source:
        client.enable_exact_source_values()
    expected = OrderedMapPairs((([1], "a"), ([2, 3], "b")))
    value = client.query_df("SELECT mapping").iloc[0, 0]
    assert value == expected
    with client.query_df_stream("SELECT mapping") as frames:
        assert next(frames).iloc[0, 0] == expected
        assert list(frames) == []
    spec = "Map(Array(Int64), String)"
    if native is not None:
        native.raw.connection.fout = NativeOutput()
        native.raw.connection.get_block_out_stream().write(
            ColumnOrientedBlock([("mapping", spec)], [[value]])
        )
        result = native.raw.connection.fout.getvalue()
    else:
        context = InsertContext("db.target", ["mapping"], [get_from_name(spec)], data=[[value]])
        result = b"".join(client._client._transform.build_insert(context))
        assert context.insert_exception is None
    assert result.endswith(payload)
