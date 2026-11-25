"""Authoritative Trino assignments preserve supported native input families."""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.trino_dates import native_date_target
from tests.sql._support.trino_integer_text import native_integer_target
from tests.sql._support.trino_numeric_text import native_numeric_target
from tests.sql._support.trino_time_values import native_time_target


def _load(value, native, explicit):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)}),
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **({"table_schema": {"payload": native}} if explicit else {}),
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_trino_native_date_accepts_cast_whitespace(monkeypatch, explicit):
    database = native_date_target(monkeypatch)

    assert _load(" \t2024-02-29\n", "DATE", explicit) == 3
    assert database.rows == [None, dt.date(2024, 2, 29), None]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        ("TIME(6)", "12:30:45.123456", dt.time(12, 30, 45, 123456)),
        (
            "TIME(6) WITH TIME ZONE",
            "12:30:45.123456+03:00",
            dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
        ),
    ],
)
def test_trino_native_time_accepts_native_clock_text(
    monkeypatch, explicit, native, value, expected
):
    database = native_time_target(monkeypatch, native)

    assert _load(value, native, explicit) == 3
    assert database.rows == [None, expected, None]


def test_trino_native_naive_clock_keeps_typed_timezone_conversion(monkeypatch):
    database = native_time_target(monkeypatch, "TIME(6) WITH TIME ZONE")

    assert _load(dt.time(12, 30, 45, 123456), "TIME(6) WITH TIME ZONE", True) == 3
    parameters = database.statements[-1][2]
    assert parameters == ["NULL", "TIME '12:30:45.123456'", "NULL"]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("native", ["TINYINT", "SMALLINT", "INTEGER"])
def test_trino_native_integer_accepts_exact_integral_decimal(monkeypatch, explicit, native):
    database = native_integer_target(monkeypatch, native)

    assert _load(Decimal("42.000"), native, explicit) == 3
    assert database.rows == [None, 42, None]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("native", ["REAL", "DOUBLE"])
@pytest.mark.parametrize("text", ["Infinity", "-Infinity", "NaN"])
def test_trino_native_floating_special_text_is_not_sql_null(monkeypatch, explicit, native, text):
    database = native_numeric_target(monkeypatch, native)

    assert _load(text, native, explicit) == 3
    assert database.rows[0] is database.rows[2] is None
    assert database.rows[1] is not None
    if text == "NaN":
        assert math.isnan(database.rows[1])
    else:
        assert database.rows[1] == float(text)


def test_trino_native_spaced_timestamp_declaration_keeps_nanoseconds(monkeypatch):
    database = native_time_target(monkeypatch, "TIMESTAMP(9) WITH TIME ZONE")
    monkeypatch.setattr(database, "decode_cast", lambda wire: wire)
    value = pd.Timestamp("2024-01-02T03:04:05.123456789Z")

    assert _load(value, "TIMESTAMP( 9 ) WITH TIME ZONE", True) == 3

    assert database.rows == ["NULL", "'2024-01-02 03:04:05.123456789 UTC'", "NULL"]
