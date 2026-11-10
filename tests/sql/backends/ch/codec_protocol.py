from __future__ import annotations

import struct
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.http_codec import (
    ExactHTTPTransform,
    _map_column,
    exact_http_type,
)
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.native_codec import (
    _map_items,
    exact_block_stream,
    read_column,
)

from tests.sql.backends.ch._support.exact_transport import (
    BinaryNativeSource,
    http_query_context,
    http_source,
    native_context,
)


def test_native_current_revision_roundtrip_and_metadata_only_block() -> None:
    from clickhouse_driver import Client, defines  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415

    raw = Client("127.0.0.1")
    NativeClickHouseClient(raw)
    raw.connection.context.server_info = SimpleNamespace(
        used_revision=defines.DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION,
        get_timezone=lambda: "UTC",
    )
    raw.connection.fout = BytesIO()
    raw.connection.get_block_out_stream().write(
        ColumnOrientedBlock([("x", "DateTime64(9)")], [[pd.Timestamp(-1)]]),
    )
    binary = raw.connection.fout.getvalue()
    block = exact_block_stream(BinaryNativeSource(binary), raw.connection.context).read()
    assert block.get_rows() == [(pd.Timestamp(-1),)]
    header = exact_block_stream(
        BinaryNativeSource(b"\x01\x00\x01x\x05Int64"), native_context()
    ).read()
    assert header.columns_with_types == [("x", "Int64")]
    assert read_column(native_context(), "Map(Int64, Int64)", 0, BytesIO()) == []
    assert _map_items(SimpleNamespace(), 0, BytesIO()) == []


def test_native_data_dependent_prefix_receives_the_same_block_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415
    from clickhouse_driver.columns import service  # noqa: PLC0415

    received = []

    class DataDependentColumn:
        prefix_needs_items = True

        def write_state_prefix(self, output: Any, values: Any) -> None:
            received.append(values)
            output.write(b"prefix")

        def write_data(self, values: Any, output: Any) -> None:
            output.write(struct.pack("<q", values[0]))

    monkeypatch.setattr(
        service, "get_column_by_spec", lambda *_args, **_kwargs: DataDependentColumn()
    )
    raw = Client("127.0.0.1")
    NativeClickHouseClient(raw)
    raw.connection.context.server_info = native_context().server_info
    raw.connection.fout = BytesIO()
    raw.connection.get_block_out_stream().write(ColumnOrientedBlock([("x", "Int64")], [[42]]))
    assert received == [[42]]
    assert raw.connection.fout.getvalue().endswith(b"prefix" + struct.pack("<q", 42))


def test_native_old_protocol_progress_does_not_certify_zero_count() -> None:
    from clickhouse_driver import Client, defines  # noqa: PLC0415

    raw = Client("127.0.0.1")
    client = NativeClickHouseClient(raw)
    NativeClickHouseClient(raw)
    raw.connection.server_info = SimpleNamespace(
        used_revision=defines.DBMS_MIN_REVISION_WITH_CLIENT_WRITE_INFO - 1,
    )

    def execute(_sql: str) -> Any:
        raw.connection.fin = BinaryNativeSource(bytes(3))
        raw.last_query = SimpleNamespace(progress=raw.connection.receive_progress())
        return []

    raw.execute = execute
    assert client.command("INSERT INTO target SELECT x FROM source") == []


def test_http_legacy_map_prefix_contract_still_uses_real_scalar_codecs() -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    class LegacyChild:
        def __init__(self, spec: str) -> None:
            self.codec = exact_http_type(get_from_name(spec))

        def read_column_data(self, source: Any, count: int, context: Any) -> Any:
            return self.codec.read_column(source, count, context)

    datatype = SimpleNamespace(
        key_type=LegacyChild("Int64"), value_type=LegacyChild("DateTime64(9)")
    )
    payload = struct.pack("<Q2q", 1, 42, 1)
    assert _map_column(datatype, http_source(payload), 1, http_query_context()) == [
        {42: pd.Timestamp(1)}
    ]


def test_http_transform_insert_uses_exact_codec_and_multiblock_result_metadata() -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.insert import InsertContext  # noqa: PLC0415
    from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

    transform = ExactHTTPTransform(NativeTransform())
    context = InsertContext(
        "db.target", ["x"], [get_from_name("DateTime64(9)")], data=[[pd.Timestamp(-1)]]
    )
    binary = b"".join(transform.build_insert(context))
    assert context.insert_exception is None
    assert binary.endswith(struct.pack("<q", -1))

    block = b"\x00" * 8 + b"\x01\x01\x01x\x05Int64" + struct.pack("<q", 42)
    query_context = http_query_context()
    query_context.block_info = True
    query_context.column_renamer = lambda name: name.upper()
    result = transform.parse_response(http_source(block + block), query_context)
    assert result.column_names == ("X",)
    assert result.result_rows == [(42,), (42,)]


@pytest.mark.parametrize(
    "message",
    [b"normal last block", b"__exception__tag\r\nCode: 1. failed\r\n16 tag\r\n__exception__"],
)
def test_http_tagged_exception_is_not_confused_with_a_normal_end_of_stream(message: bytes) -> None:
    from clickhouse_connect.driver import transform  # noqa: PLC0415
    from clickhouse_connect.driver.exceptions import (  # noqa: PLC0415
        StreamCompleteException,
        StreamFailureError,
    )

    def finished() -> Any:
        raise StreamCompleteException

    source = SimpleNamespace(
        read_leb128=finished,
        last_message=message,
        exception_tag="tag",
        close=lambda: None,
    )
    extractor = getattr(transform, "extract_exception_with_tag", None)
    if extractor and extractor(message, "tag"):
        with pytest.raises(StreamFailureError, match="Code: 1"):
            ExactHTTPTransform(None).parse_response(source, http_query_context())
    else:
        result = ExactHTTPTransform(None).parse_response(source, http_query_context())
        assert result.result_rows == []
