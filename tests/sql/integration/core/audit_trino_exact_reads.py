"""Exact Trino source values from engine expressions, without table creation."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.exact_values import ExactClock, ExactTemporal

from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from typing import Any

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_SCALE = 12
_NANOSECOND_TICKS = 1000


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _timestamp_ticks(value: Any, *, aware: bool = False) -> int:
    if isinstance(value, ExactTemporal):
        assert 0 <= value.scale <= _SCALE
        assert not value.offset_naive
        if aware:
            assert value.timezone is not None
            assert value.calendar_datetime(utc=True).utcoffset() == dt.timedelta(0)
        else:
            assert value.timezone is None
        return value.ticks * 10 ** (_SCALE - value.scale)
    assert isinstance(value, pd.Timestamp)
    if aware:
        assert value.utcoffset() == dt.timedelta(0)
    else:
        assert value.tzinfo is None
    return int(value.value) * _NANOSECOND_TICKS


def _assert_nanosecond_map(value: Any) -> None:
    assert isinstance(value, dict)
    assert len(value) == 2
    actual = {_timestamp_ticks(key): item for key, item in value.items()}
    assert actual == {
        123456788 * _NANOSECOND_TICKS: "earlier",
        123456789 * _NANOSECOND_TICKS: "later",
    }


def _assert_picosecond_reads(alias: str) -> None:
    earlier = "TIMESTAMP '1970-01-01 00:00:00.123456789011'"
    later = "TIMESTAMP '1970-01-01 00:00:00.123456789012'"
    result = sql.read(
        alias,
        f"SELECT {later} AS stamp, "
        "TIMESTAMP '1970-01-01 00:00:00.123456789012 UTC' AS zoned_stamp, "
        "TIMESTAMP '1969-12-31 23:59:59.123456789012' AS negative_stamp, "
        f"MAP(ARRAY[{earlier}, {later}], ARRAY['earlier', 'later']) AS stamp_map",
        retry_cnt=1,
        query_label=_label("trino-exact-picoseconds12"),
    )
    assert len(result) == 1
    row = result.iloc[0]
    for name, expected, aware in (
        ("stamp", 123456789012, False),
        ("zoned_stamp", 123456789012, True),
        ("negative_stamp", -876543210988, False),
    ):
        value = row[name]
        assert isinstance(value, ExactTemporal)
        assert value.scale == 12
        assert _timestamp_ticks(value, aware=aware) == expected
    precise_map = row["stamp_map"]
    assert isinstance(precise_map, dict)
    assert len(precise_map) == 2
    for key in precise_map:
        assert isinstance(key, ExactTemporal)
        assert key.scale == 12
    assert {_timestamp_ticks(key): item for key, item in precise_map.items()} == {
        123456789011: "earlier",
        123456789012: "later",
    }


def _assert_clock_reads(alias: str) -> None:
    result = sql.read(
        alias,
        "SELECT TIME '12:30:45.123456789' AS clock, "
        "TIME '12:30:45.123456789 +03:00' AS zoned_clock, "
        "TIME '23:59:59.999999999999' AS last_picosecond, "
        "TIME '12:30:45.123456000' AS aligned_clock",
        retry_cnt=1,
        query_label=_label("trino-exact-date-free-clocks"),
    )
    assert len(result) == 1
    row = result.iloc[0]
    for name, ticks, scale, offset in (
        ("clock", 45045 * 10**9 + 123456789, 9, None),
        ("zoned_clock", 45045 * 10**9 + 123456789, 9, 180),
        ("last_picosecond", 86400 * 10**12 - 1, 12, None),
    ):
        value = row[name]
        assert isinstance(value, ExactClock)
        assert value.ticks == ticks
        assert value.scale == scale
        assert value.offset_minutes == offset
    assert row["clock"].isoformat() == "12:30:45.123456789"
    assert row["zoned_clock"].isoformat() == "12:30:45.123456789+03:00"
    assert row["last_picosecond"].isoformat() == "23:59:59.999999999999"
    assert isinstance(row["aligned_clock"], dt.time)
    assert row["aligned_clock"] == dt.time(12, 30, 45, 123456)


@pytest.mark.sql_scenario("audit.read.trino_exact_temporal")
def test_trino_engine_reads_preserve_exact_temporals_and_nanosecond_map_keys() -> None:
    alias = "trino_source_values"
    earlier = "TIMESTAMP '1970-01-01 00:00:00.123456788'"
    later = "TIMESTAMP '1970-01-01 00:00:00.123456789'"
    map_sql = f"MAP(ARRAY[{earlier}, {later}], ARRAY['earlier', 'later'])"
    scalar = sql.read(
        alias,
        f"SELECT {later}",
        output_type="scalar",
        retry_cnt=1,
        query_label=_label("trino-exact-scalar9"),
    )
    assert _timestamp_ticks(scalar) == 123456789 * _NANOSECOND_TICKS

    # These remain engine expressions: Iceberg's table format is not changed
    # and values reach the client as temporal types, rather than pre-cast text.
    result = sql.read(
        alias,
        f"SELECT {later} AS stamp, "
        "TIMESTAMP '1970-01-01 00:00:00.123456789 UTC' AS zoned_stamp, "
        "TIMESTAMP '1969-12-31 23:59:59.123456789' AS negative_stamp, "
        f"{map_sql} AS stamp_map, ARRAY[{later}, NULL] AS stamp_array, "
        f"CAST(ROW(DATE '1900-01-01', {later}, {map_sql}) "
        "AS ROW(day DATE, stamp TIMESTAMP(9), stamp_map MAP(TIMESTAMP(9), VARCHAR))) AS bundle",
        retry_cnt=1,
        query_label=_label("trino-exact-nested9"),
    )
    assert len(result) == 1
    row = result.iloc[0]
    assert _timestamp_ticks(row["stamp"]) == 123456789 * _NANOSECOND_TICKS
    assert _timestamp_ticks(row["zoned_stamp"], aware=True) == 123456789 * _NANOSECOND_TICKS
    assert _timestamp_ticks(row["negative_stamp"]) == -876543211 * _NANOSECOND_TICKS
    _assert_nanosecond_map(row["stamp_map"])
    array = row["stamp_array"]
    assert len(array) == 2
    assert _timestamp_ticks(array[0]) == 123456789 * _NANOSECOND_TICKS
    assert array[1] is None
    bundle = row["bundle"]
    assert len(bundle) == 3
    assert bundle[0] == dt.date(1900, 1, 1)
    assert _timestamp_ticks(bundle[1]) == 123456789 * _NANOSECOND_TICKS
    _assert_nanosecond_map(bundle[2])
    _assert_picosecond_reads(alias)
    _assert_clock_reads(alias)
