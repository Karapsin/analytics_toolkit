"""Resolved clock parameters reach native Trino casts through the real SDK."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.core.exact_values import ExactClock

from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from typing import Any

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label() -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        "trino-clock-parameter-cast",
    )


def _clock_parts(value: Any) -> tuple[int, int | None]:
    if isinstance(value, ExactClock):
        return value.ticks * 10 ** (12 - value.scale), value.offset_minutes
    assert isinstance(value, dt.time)
    seconds = value.hour * 3600 + value.minute * 60 + value.second
    offset = value.utcoffset()
    minutes = None if offset is None else (offset.days * 86400 + offset.seconds) // 60
    return seconds * 10**12 + value.microsecond * 10**6, minutes


def _cast(cursor: Any, value: Any, native: str, native_control: str) -> tuple[Any, Any]:
    # The control uses independent native TIME literals, never the normalized
    # parameter text. Both casts share one query's session-start timezone.
    cursor.execute(
        f"/* {_label()} */ SELECT CAST(? AS {native}), CAST({native_control} AS {native})",
        [normalize_value(value, native)],
    )
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert len(rows[0]) == 2
    return rows[0][0], rows[0][1]


def _scalar_casts(cursor: Any) -> None:
    east = dt.timezone(dt.timedelta(hours=3))
    cases = [
        ("TIME(0)", dt.time(12, 30, 45, tzinfo=east), "TIME '12:30:45 +03:00'", 0, None),
        (
            "TIME(6)",
            dt.time(12, 30, 45, 123456, tzinfo=east),
            "TIME '12:30:45.123456 +03:00'",
            123456000000,
            None,
        ),
        (
            "TIME(6) WITH TIME ZONE",
            dt.time(12, 30, 45, 123456, tzinfo=east),
            "TIME '12:30:45.123456 +03:00'",
            123456000000,
            180,
        ),
        (
            "TIME(12) WITH TIME ZONE",
            ExactClock(45045 * 10**12 + 123456789012, 12, 180),
            "TIME '12:30:45.123456789012 +03:00'",
            123456789012,
            180,
        ),
    ]
    for native, value, control, fraction, offset in cases:
        actual, expected = _cast(cursor, value, native, control)
        assert _clock_parts(actual) == _clock_parts(expected)
        assert _clock_parts(actual) == (45045 * 10**12 + fraction, offset)


def _mixed_array_casts(cursor: Any) -> None:
    values = [
        dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
        dt.time(13, 30, 45, 654321),
        ExactClock(52245 * 10**12 + 123456789012, 12, 180),
        ExactClock(55845 * 10**12 + 987654321012, 12),
        None,
    ]
    literals = [
        "TIME '12:30:45.123456 +03:00'",
        "TIME '13:30:45.654321'",
        "TIME '14:30:45.123456789012 +03:00'",
        "TIME '15:30:45.987654321012'",
        "NULL",
    ]
    ticks = [45045 * 10**12 + 123456000000, 48645 * 10**12 + 654321000000]
    ticks.extend([52245 * 10**12 + 123456789012, 55845 * 10**12 + 987654321012])
    for leaf in ("TIME(12)", "TIME(12) WITH TIME ZONE"):
        # Casting each independent control leaf first avoids a mixed-type ARRAY
        # before its outer cast, while preserving native per-leaf semantics.
        control = "ARRAY[" + ", ".join(f"CAST({literal} AS {leaf})" for literal in literals) + "]"
        actual, expected = _cast(cursor, values, f"ARRAY({leaf})", control)
        assert len(actual) == len(expected) == 5
        assert actual[-1] is None
        assert expected[-1] is None
        for index, expected_ticks in enumerate(ticks):
            actual_parts, expected_parts = (
                _clock_parts(actual[index]),
                _clock_parts(expected[index]),
            )
            assert actual_parts == expected_parts
            assert actual_parts[0] == expected_ticks
            if leaf == "TIME(12)":
                assert actual_parts[1] is None
            elif index in (0, 2):
                assert actual_parts[1] == 180
            else:
                assert actual_parts[1] is not None


@pytest.mark.sql_scenario("audit.load.trino_clock_parameters")
def test_trino_clock_parameters_cast_scalars_and_mixed_arrays_on_native_engine() -> None:
    with closing(get_sql_connection("trino_source_values")) as connection, closing(
        connection.cursor()
    ) as cursor:
        _scalar_casts(cursor)
        _mixed_array_casts(cursor)
