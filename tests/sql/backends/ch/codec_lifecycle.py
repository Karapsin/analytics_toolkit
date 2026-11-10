from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.exact_temporal import timestamp_from_ticks, timestamp_ticks
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient, ExactHTTPTransform
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.native_codec import install_exact_native_codec

from tests.sql.backends.ch._support.exact_transport import (
    BinaryNativeSource,
    http_source,
    native_context,
)


@pytest.mark.parametrize("scale", [-1, 10])
def test_temporal_codec_rejects_unsupported_scale(scale: int) -> None:
    with pytest.raises(ValueError, match="scale"):
        timestamp_from_ticks(0, scale)


def test_temporal_encoder_keeps_aware_instants_and_negative_fraction() -> None:
    aware = pd.Timestamp("1969-12-31T23:59:59.999999999+00:00").tz_convert("Europe/Moscow")
    assert timestamp_ticks(aware, 9) == -1
    assert timestamp_ticks(pd.Timestamp("1970-01-01T03:00:00"), 9, "Europe/Moscow") == 0
    assert timestamp_ticks(42, 9) == 42
    assert timestamp_from_ticks(0, 9, "Europe/Moscow", offset_naive=True) == pd.Timestamp(
        "1970-01-01T03:00:00"
    )


@pytest.mark.parametrize(
    "connection", [SimpleNamespace(), SimpleNamespace(get_block_in_stream=lambda: None)]
)
def test_native_incompatible_factory_fails_instead_of_decoding_lossily(connection: Any) -> None:
    client = SimpleNamespace(connection=connection, client_settings={})
    with pytest.raises(TypeError, match="Incompatible clickhouse-driver"):
        install_exact_native_codec(client)


def test_native_driver_without_connection_interface_is_rejected() -> None:
    raw_type = type("MissingConnection", (), {"__module__": "clickhouse_driver.fake"})
    with pytest.raises(TypeError, match="Incompatible clickhouse-driver"):
        install_exact_native_codec(raw_type())


def test_native_existing_streams_are_replaced_locally() -> None:
    from clickhouse_driver import Client  # noqa: PLC0415

    raw = Client("127.0.0.1")
    raw.connection.context.server_info = native_context().server_info
    raw.connection.fin = BinaryNativeSource(b"\x00\x00")
    raw.connection.fout = BytesIO()
    raw.connection.block_in = object()
    raw.connection.block_out = object()
    install_exact_native_codec(raw)
    assert raw.connection.block_in.read().columns_with_types == []
    assert callable(raw.connection.block_out.write)


@pytest.mark.parametrize(("reported", "count"), [(False, 0), (True, 0), (True, 42)])
def test_native_command_counts_require_real_progress_evidence(reported: bool, count: int) -> None:
    from clickhouse_driver import Client, defines  # noqa: PLC0415
    from clickhouse_driver.progress import Progress  # noqa: PLC0415

    raw = Client("127.0.0.1")
    client = NativeClickHouseClient(raw)
    raw.connection.server_info = SimpleNamespace(
        used_revision=defines.DBMS_MIN_REVISION_WITH_CLIENT_WRITE_INFO,
    )
    raw.connection.context.server_info = raw.connection.server_info

    def execute(_sql: str) -> Any:
        progress = Progress()
        if reported:
            raw.connection.fin = BinaryNativeSource(bytes([0, 0, 0, count, 0]))
            progress = raw.connection.receive_progress()
        raw.last_query = SimpleNamespace(progress=progress)
        return []

    raw.execute = execute
    assert client.command("INSERT INTO target SELECT value FROM source") == (
        {"written_rows": count} if reported else []
    )


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([42], "column metadata"),
        ([[("x", "Int64")], (1, 2)], "row block"),
    ],
)
def test_native_invalid_stream_preserves_primary_failure_when_disconnect_fails(
    items: Any, message: str
) -> None:
    class Raw:
        def execute_iter(self, _sql: str, **_kwargs: Any) -> Any:
            return iter(items)

        def disconnect_connection(self) -> None:
            message = "close failed"
            raise RuntimeError(message)

    def read_invalid() -> None:
        with NativeClickHouseClient(Raw()).query_df_stream("SELECT x") as frames:
            list(frames)

    with pytest.raises((TypeError, ValueError), match=message):
        read_invalid()


def test_http_incompatible_transform_is_actionable() -> None:
    with pytest.raises(RuntimeError, match="Incompatible clickhouse-connect"):
        ExactHTTPClient(SimpleNamespace(_transform=SimpleNamespace(parse_response=None)))
    raw_type = type("MissingTransform", (), {"__module__": "clickhouse_connect.driver.fake"})
    with pytest.raises(RuntimeError, match="Incompatible clickhouse-connect"):
        ExactHTTPClient(raw_type())


def test_http_exact_transform_rejects_numpy_before_reading() -> None:
    with pytest.raises(ValueError, match="non-NumPy"):
        ExactHTTPTransform(None).parse_response(None, SimpleNamespace(use_numpy=True))


def test_http_transport_installs_only_a_client_local_transform() -> None:
    from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

    original = NativeTransform()
    raw = SimpleNamespace(_transform=original)
    ExactHTTPClient(raw)
    assert isinstance(raw._transform, ExactHTTPTransform)
    assert not isinstance(original, ExactHTTPTransform)


@pytest.mark.parametrize("invalid", [False, True])
def test_http_binary_result_close_failure_cannot_change_success_or_decode_error(
    invalid: bool,
) -> None:
    from clickhouse_connect.driver.exceptions import InternalError  # noqa: PLC0415
    from clickhouse_connect.driver.query import QueryContext  # noqa: PLC0415

    closed = []

    def fail_close() -> None:
        if not closed:
            closed.append(True)
            message = "close failed"
            raise RuntimeError(message)

    if invalid:
        source = http_source(b"\x01\x01\x01x\x07Unknown", fail_close)
        with pytest.raises(InternalError, match=r"Unknown|unrecognized"):
            ExactHTTPTransform(None).parse_response(source, QueryContext())
    else:
        source = http_source(b"\x01\x01\x01x\x05Int64" + bytes(8), fail_close)
        result = ExactHTTPTransform(None).parse_response(source, QueryContext())
        owned_source = result.source
        assert result.result_rows == [(0,)]
        owned_source.close()
        result.close()
    assert closed == [True]


def test_http_empty_and_metadata_only_binary_results() -> None:
    from clickhouse_connect.driver.query import QueryContext  # noqa: PLC0415

    empty = ExactHTTPTransform(None).parse_response(http_source(b""), QueryContext())
    assert empty.result_rows == []
    payload = b"\x01\x00\x01x\x05Int64"
    result = ExactHTTPTransform(None).parse_response(http_source(payload), QueryContext())
    assert result.column_names == ("x",)
    assert result.result_rows == []


def test_http_query_and_dataframe_insert_use_exact_non_numpy_paths() -> None:
    class Raw:
        sentinel = "delegated"

        def query(self, sql: str, **kwargs: Any) -> Any:
            assert sql == "SELECT x"
            assert kwargs == {"column_oriented": True, "use_numpy": False}
            return SimpleNamespace(column_names=("x",), result_columns=[[2**64 - 1]], row_count=1)

        def insert(self, table: str, data: Any, **kwargs: Any) -> Any:
            assert table == "db.target"
            assert kwargs == {"column_names": ["x"]}
            assert data == [(pd.Timestamp(1),)]
            return 1

    client = ExactHTTPClient(Raw())
    assert client.sentinel == "delegated"
    assert client.query_df("SELECT x").iloc[0, 0] == 2**64 - 1
    frame = pd.DataFrame({"x": [pd.Timestamp(1)]})
    assert client.insert_df("db.target", frame) == 1
    assert client.insert_df("db.target", frame, column_names=["x"]) == 1


@pytest.mark.parametrize("primary_failure", [False, True])
def test_http_stream_close_failure_cannot_replace_outcome(primary_failure: bool) -> None:
    class Stream:
        source = SimpleNamespace(column_names=("x",))

        def __enter__(self) -> Any:
            return iter([[], [[1]]])

        def __exit__(self, *_args: object) -> None:
            message = "close failed"
            raise RuntimeError(message)

    raw = SimpleNamespace(query_column_block_stream=lambda _sql: Stream())
    client = ExactHTTPClient(raw)
    if primary_failure:

        def read_and_fail() -> None:
            with client.query_df_stream("SELECT x") as frames:
                assert next(frames).iloc[0, 0] == 1
                message = "primary"
                raise ValueError(message)

        with pytest.raises(ValueError, match="primary"):
            read_and_fail()
    else:
        with client.query_df_stream("SELECT x") as frames:
            assert [frame.iloc[0, 0] for frame in frames] == [1]
