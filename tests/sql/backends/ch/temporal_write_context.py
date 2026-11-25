from __future__ import annotations

import struct
from datetime import date, datetime, timedelta, timezone, tzinfo
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.exact_temporal import timestamp_ticks
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient, exact_http_type
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.native_codec import exact_block_stream
from analytics_toolkit.sql.backends.ch.temporal_context import (
    canonical_timezone,
    effective_read_timezone,
    effective_write_timezone,
    native_context_timezone,
    timezone_name,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from zoneinfo import ZoneInfo

from tests.sql.backends.ch._support.exact_transport import (
    BinaryNativeSource,
    http_query_context,
    http_source,
    native_context,
)


def http_insert(spec: str, value: Any, zone: Any) -> tuple[bytes, Any]:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.insert import InsertContext  # noqa: PLC0415
    from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

    raw = SimpleNamespace(_transform=NativeTransform(), server_tz=zone)
    ExactHTTPClient(raw)
    context = InsertContext("db.target", ["x"], [get_from_name(spec)], data=[[value]])
    return b"".join(raw._transform.build_insert(context)), context.insert_exception


class NoHostTimestamp(datetime):
    def timestamp(self) -> float:
        message = "Host-local timestamp conversion is forbidden."
        raise AssertionError(message)


@pytest.mark.parametrize("python_buffer", [False, True])
@pytest.mark.parametrize(
    ("spec", "value", "zone", "ticks"),
    [
        ("DateTime", NoHostTimestamp(2024, 1, 1), "Europe/Moscow", 1_704_056_400),
        ("DateTime('UTC')", NoHostTimestamp(1970, 1, 1), "Europe/Moscow", 0),
        ("DateTime", datetime(1970, 1, 1, tzinfo=timezone.utc), "Europe/Moscow", 0),
        ("DateTime", 2**32 - 1, "America/Los_Angeles", 2**32 - 1),
        ("Nullable(DateTime)", None, "Europe/Moscow", 0),
        ("Array(DateTime)", [NoHostTimestamp(2024, 1, 1)], "Europe/Moscow", 1_704_056_400),
    ],
)
def test_http_datetime_wire_uses_declared_or_client_zone_and_real_buffers(
    spec: str,
    value: Any,
    zone: str,
    ticks: int,
    python_buffer: bool,
) -> None:
    binary, error = http_insert(spec, value, zone)
    assert error is None
    source = http_source(binary[-4:], python_buffer=python_buffer)
    try:
        assert source.read_array("I", 1)[0] == ticks
    finally:
        source.close()


@pytest.mark.parametrize(
    ("spec", "value", "zone", "range_name"),
    [
        ("DateTime", NoHostTimestamp(1970, 1, 1), "Europe/Moscow", "UInt32"),
        ("DateTime", NoHostTimestamp(2106, 2, 7), "America/Los_Angeles", "UInt32"),
        ("DateTime", -1, "UTC", "UInt32"),
        ("DateTime", 2**32, "UTC", "UInt32"),
        ("DateTime64(9)", NoHostTimestamp(2262, 4, 11, 23, 30), "America/Los_Angeles", "Int64"),
        ("DateTime64(9)", -(2**63) - 1, "UTC", "Int64"),
    ],
)
def test_http_timestamp_range_is_checked_after_timezone_conversion(
    spec: str,
    value: Any,
    zone: str,
    range_name: str,
) -> None:
    binary, error = http_insert(spec, value, zone)
    assert isinstance(error, ValueError)
    assert range_name in str(error)
    assert binary == b"INTERNAL EXCEPTION WHILE SERIALIZING"


@pytest.mark.parametrize("spec", ["DateTime", "DateTime64(9)"])
def test_nonnullable_http_timestamp_null_is_not_silently_an_epoch(spec: str) -> None:
    _binary, error = http_insert(spec, None, "UTC")
    assert isinstance(error, ValueError)
    assert "non-nullable" in str(error)


@pytest.mark.parametrize("spec", ["DateTime", "DateTime('UTC')", "DateTime('Europe/Moscow')"])
def test_datetime_read_keeps_ordinary_driver_scalar_and_timezone_policy(spec: str) -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    ordinary = get_from_name(spec)
    exact = exact_http_type(ordinary)
    binary = struct.pack("<I", 1_704_056_400)
    expected = ordinary.read_column(http_source(binary), 1, http_query_context())[0]
    actual = exact.read_column(http_source(binary), 1, http_query_context())[0]
    assert isinstance(actual, datetime)
    assert type(actual) is type(expected)
    assert actual == expected
    assert (actual.tzinfo is None) == (expected.tzinfo is None)
    assert actual.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Europe/Moscow", "Europe/Moscow"),
        (ZoneInfo("America/Los_Angeles"), "America/Los_Angeles"),
        (timezone.utc, "UTC"),
        (timezone(timedelta(hours=5, minutes=30)), "+05:30"),
        (timezone(timedelta(hours=-3, seconds=-1)), "-03:00:01"),
        (None, None),
        ("", None),
        (object(), None),
        (timezone(timedelta(microseconds=1)), None),
        (tzinfo(), None),
    ],
)
def test_effective_timezone_metadata_has_stable_identity(zone: Any, expected: str | None) -> None:
    assert timezone_name(zone) == expected


def test_effective_timezone_unknown_snapshot_cannot_fall_back_to_later_raw_metadata() -> None:
    raw = SimpleNamespace(server_tz=None)
    client = ExactHTTPClient(raw)
    assert effective_write_timezone(client) is None
    raw.server_tz = "Europe/Moscow"
    assert effective_write_timezone(client) is None
    assert effective_write_timezone(raw) == "Europe/Moscow"
    assert effective_write_timezone(ExactHTTPClient(raw)) == "Europe/Moscow"


def test_native_effective_timezone_uses_same_server_client_and_reconnect_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.util import compat  # noqa: PLC0415

    raw = Client("127.0.0.1", compression="lz4")
    client = NativeClickHouseClient(raw)
    assert effective_write_timezone(client) is None
    raw.connection.context.server_info = SimpleNamespace(get_timezone=lambda: "Europe/Moscow")
    assert effective_write_timezone(client) == "Europe/Moscow"
    assert effective_write_timezone(raw) == "Europe/Moscow"
    raw.connection.context.server_info = SimpleNamespace(get_timezone=lambda: "America/Los_Angeles")
    assert effective_write_timezone(client) == "America/Los_Angeles"
    raw.connection.context.settings = {"use_client_time_zone": True}
    monkeypatch.setattr(compat, "get_localzone_name_compat", lambda: "Europe/Paris")
    assert effective_write_timezone(client) == "Europe/Paris"
    assert native_context_timezone(None) is None
    assert raw.connection.connected is False


def test_effective_timezone_is_visible_through_connection_delegation() -> None:
    class Proxy:
        def __init__(self, client: Any) -> None:
            self.client = client

        def __getattr__(self, name: str) -> Any:
            return getattr(self.client, name)

    client = ExactHTTPClient(SimpleNamespace(server_tz=ZoneInfo("Europe/Moscow")))
    assert effective_write_timezone(Proxy(client)) == "Europe/Moscow"
    assert effective_read_timezone(Proxy(client)) is None
    client.enable_exact_source_values()
    assert effective_read_timezone(client) is None


@pytest.mark.parametrize("compression", [False, "lz4", "zstd"])
def test_native_datetime_writes_raw_exact_instants_across_destination_zones(
    compression: Any,
) -> None:
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415

    raw = Client("127.0.0.1", compression=compression)
    client = NativeClickHouseClient(raw)
    context = native_context()
    context.server_info.get_timezone = lambda: "America/Los_Angeles"
    raw.connection.context.server_info = context.server_info
    raw.connection.fout = BytesIO()
    value = ExactTemporal(0, 0, "Europe/Moscow", offset_naive=True)
    raw.connection.get_block_out_stream().write(ColumnOrientedBlock([("x", "DateTime")], [[value]]))
    binary = raw.connection.fout.getvalue()
    block = exact_block_stream(
        BinaryNativeSource(binary), raw.connection.context, compressed=bool(compression)
    ).read()
    decoded = block.get_rows()[0][0]
    assert timestamp_ticks(decoded, 0, "America/Los_Angeles") == 0
    assert effective_write_timezone(client) == "America/Los_Angeles"


@pytest.mark.parametrize(
    ("spec", "value", "utc"),
    [
        ("DateTime", NoHostTimestamp(2100, 7, 1), "2100-07-01T07:00:00Z"),
        ("DateTime64(9)", NoHostTimestamp(2262, 4, 11), "2262-04-11T07:00:00Z"),
    ],
)
def test_future_la_native_http_wires_and_calendars_match_iana(
    spec: str, value: Any, utc: str
) -> None:
    import pandas as pd  # noqa: PLC0415
    import pytz  # noqa: PLC0415
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415

    scale = 9 if spec.startswith("DateTime64") else 0
    tick = pd.Timestamp(utc).value // 10 ** (9 - scale)
    wire_format = "<q" if scale else "<I"
    raw = Client("127.0.0.1")
    NativeClickHouseClient(raw)
    context = native_context()
    context.server_info.get_timezone = lambda: "America/Los_Angeles"
    raw.connection.context.server_info = context.server_info
    raw.connection.fout = BytesIO()
    raw.connection.get_block_out_stream().write(ColumnOrientedBlock([("x", spec)], [[value]]))
    native = raw.connection.fout.getvalue()
    assert native.endswith(struct.pack(wire_format, tick))
    for zone in (
        "America/Los_Angeles",
        ZoneInfo("America/Los_Angeles"),
        pytz.timezone("America/Los_Angeles"),
    ):
        binary, error = http_insert(spec, value, zone)
        assert error is None
        assert binary.endswith(struct.pack(wire_format, tick))
    decoded = (
        exact_block_stream(BinaryNativeSource(native), raw.connection.context)
        .read()
        .get_rows()[0][0]
    )
    assert decoded.hour == 0
    read_context = http_query_context()
    selected_zone = canonical_timezone("America/Los_Angeles")
    read_context.query_tz = selected_zone
    if not callable(read_context.active_tz):
        read_context.active_tz = selected_zone
    decoded = exact_http_type(get_from_name(spec)).read_column(
        http_source(struct.pack(wire_format, tick)),
        1,
        read_context,
    )[0]
    assert decoded.hour == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(1970, 1, 1), -1000),
        (datetime(1970, 1, 1), -1000),  # noqa: DTZ001 -- naive destination wall time
        (datetime(1970, 1, 1, tzinfo=timezone.utc), 0),
        (pd.Timestamp("1970-01-01 00:00:00.000000123"), -877),
    ],
)
def test_submicrosecond_fixed_offset_writes_keep_exact_ticks(value: Any, expected: int) -> None:
    # Such an offset has no whole-second timezone name; retain the actual tzinfo policy.
    zone = timezone(timedelta(microseconds=1))
    assert timezone_name(zone) is None
    assert timestamp_ticks(value, 9, zone) == expected
    binary, error = http_insert("DateTime64(9)", value, zone)
    assert error is None
    assert struct.unpack("<q", binary[-8:])[0] == expected


def test_timezone_without_fixed_offset_remains_unresolved() -> None:
    class CalendarDependentZone(tzinfo):
        def utcoffset(self, value: Any) -> None:
            return None

    assert timezone_name(CalendarDependentZone()) is None


def test_http_datetime_integer_read_format_returns_raw_seconds() -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    datatype = exact_http_type(get_from_name("DateTime"))
    context = http_query_context(query_formats={"DateTime": "int"})
    result = datatype.read_column(http_source(struct.pack("<2I", 0, 2**32 - 1)), 2, context)
    assert list(result) == [0, 2**32 - 1]
