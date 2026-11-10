from __future__ import annotations

import struct
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch import exact_temporal
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient, exact_http_type
from analytics_toolkit.sql.backends.ch.native_client import (
    NativeClickHouseClient,
    NativeQueryResult,
)
from analytics_toolkit.sql.backends.ch.native_codec import read_column
from analytics_toolkit.sql.core.exact_values import (
    ExactTemporal,
    decode_value,
    encode_value,
    typed_text,
)
from analytics_toolkit.sql.dml.io.dataframes import dataframe_from_columns

from tests.sql.backends.ch._support.exact_transport import (
    http_query_context,
    http_source,
    native_context,
)


def epoch_seconds(year: int) -> int:
    delta = datetime(year, 1, 1, tzinfo=timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86400 + delta.seconds


@pytest.mark.parametrize("scale", [7, 8])
def test_binary_temporal_values_beyond_pandas_range_keep_raw_ticks(scale: int) -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    ticks = epoch_seconds(2299) * 10**scale + 1
    spec = f"DateTime64({scale}, 'UTC')"
    binary = struct.pack("<q", ticks)
    native = read_column(native_context(), spec, 1, BytesIO(binary))[0]
    datatype = exact_http_type(get_from_name(spec))
    http = datatype.read_column(http_source(binary), 1, http_query_context())[0]
    for value in (native, http):
        assert value == ExactTemporal(ticks, scale, "UTC")
        assert exact_temporal.timestamp_ticks(value, scale) == ticks
        assert exact_temporal.timestamp_ticks(value, 9) == ticks * 10 ** (9 - scale)
        assert exact_temporal.timestamp_ticks(value, 6) == ticks // 10 ** (scale - 6)
        result = NativeQueryResult([(value,)], [("time", spec)], column_oriented=False)
        assert result.result_rows == [(value,)]
        frame = dataframe_from_columns(["time"], [[value]], row_count=1)
        assert frame.iloc[0, 0] is value


@pytest.mark.parametrize("scale", [0, 3, 6, 7, 8])
@pytest.mark.parametrize(
    ("zone", "naive"), [(None, False), ("UTC", False), ("Europe/Moscow", True)]
)
def test_microsecond_aligned_values_fall_back_to_datetime_when_pandas_is_narrow(
    monkeypatch: pytest.MonkeyPatch, scale: int, zone: str | None, naive: bool
) -> None:
    def narrow_timestamp(*_args: Any, **_kwargs: Any) -> Any:
        message = "pandas 1.x nanosecond range"
        raise pd.errors.OutOfBoundsDatetime(message)

    monkeypatch.setattr(exact_temporal, "pd", SimpleNamespace(Timestamp=narrow_timestamp))
    ticks = epoch_seconds(2299) * 10**scale
    value = exact_temporal.timestamp_from_ticks(ticks, scale, zone, offset_naive=naive)
    assert isinstance(value, datetime)
    assert value.year == 2299
    assert value.microsecond == 0
    assert (value.tzinfo is None) == (zone is None or naive)


def test_exact_temporal_fallback_text_keeps_full_fraction_and_utc_instant() -> None:
    value = ExactTemporal(epoch_seconds(2299) * 10**8 + 1, 8, "Europe/Moscow")
    assert typed_text(value) == "2299-01-01T00:00:00.00000001+00:00"


@pytest.mark.parametrize("year", [1970, 2299])
@pytest.mark.parametrize("client_timezone", [False, True])
def test_binary_naive_native_wallclock_preserves_raw_ticks_and_source_zone(
    monkeypatch: pytest.MonkeyPatch, year: int, client_timezone: bool
) -> None:
    from clickhouse_driver import Client  # noqa: PLC0415
    from clickhouse_driver.block import ColumnOrientedBlock  # noqa: PLC0415
    from clickhouse_driver.columns import datetimecolumn  # noqa: PLC0415
    from clickhouse_driver.util import compat  # noqa: PLC0415

    monkeypatch.setattr(datetimecolumn, "get_localzone_name_compat", lambda: "Europe/Moscow")
    monkeypatch.setattr(compat, "get_localzone_name_compat", lambda: "Europe/Moscow")
    context = native_context()
    context.settings = {"use_client_time_zone": client_timezone}
    context.server_info.get_timezone = lambda: "UTC" if client_timezone else "Europe/Moscow"
    ticks = epoch_seconds(year) * 10**7 + 1
    value = read_column(context, "DateTime64(7)", 1, BytesIO(struct.pack("<q", ticks)))[0]
    expected_text = f"{year}-01-01T03:00:00.0000001"
    if year == 2299:
        assert value == ExactTemporal(ticks, 7, "Europe/Moscow", offset_naive=True)
        assert value.calendar_datetime().tzinfo is None
        assert value.isoformat() == expected_text
        assert value.isoformat(utc=True) == expected_text
        assert decode_value(encode_value(value)) == value
    else:
        assert value == pd.Timestamp(expected_text)
        assert value.tzinfo is None
    assert typed_text(value, fractional_scale=7) == expected_text
    assert exact_temporal.timestamp_ticks(value, 7, "Europe/Moscow") == ticks

    raw = Client("127.0.0.1")
    NativeClickHouseClient(raw)
    raw.connection.context.server_info = context.server_info
    raw.connection.context.settings = context.settings
    raw.connection.fout = BytesIO()
    raw.connection.get_block_out_stream().write(
        ColumnOrientedBlock([("x", "DateTime64(7)")], [[value]]),
    )
    assert raw.connection.fout.getvalue().endswith(struct.pack("<q", ticks))


@pytest.mark.parametrize("spec", ["DateTime64(7)", "Array(DateTime64(7))", "DateTime64(7, 'UTC')"])
def test_http_implicit_timestamp_writes_use_server_zone_and_preserve_explicit_zone(
    spec: str,
) -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.insert import InsertContext  # noqa: PLC0415
    from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

    raw = SimpleNamespace(_transform=NativeTransform(), server_tz="Europe/Moscow")
    ExactHTTPClient(raw)
    value = pd.Timestamp("1970-01-01T03:00:00.000000100")
    values = [[[value]]] if spec.startswith("Array") else [[value]]
    declared_type = get_from_name(spec)
    context = InsertContext("db.target", ["x"], [declared_type], data=values)
    binary = b"".join(raw._transform.build_insert(context))
    expected_ticks = 108_000_000_001 if "UTC" in spec else 1
    assert context.insert_exception is None
    assert binary.endswith(struct.pack("<q", expected_ticks))
    assert not hasattr(declared_type, "_atk_default_timezone")
