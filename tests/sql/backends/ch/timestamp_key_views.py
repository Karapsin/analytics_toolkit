"""Timestamp key views retain unresolved walls and every aware key column."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.timestamp_keys import (
    dataframe_key_identity,
    source_key_validation_frame,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from zoneinfo import ZoneInfo


@pytest.mark.parametrize(
    "wall",
    [
        dt.datetime(2024, 11, 3, 1, 30, fold=1),  # noqa: DTZ001 -- unresolved wall coordinates.
        pd.Timestamp("2024-11-03T01:30:00.000000123"),
        ExactTemporal(1_700_000_000_000_000_123, 9, wall_time=True),
    ],
    ids=["naive-fold", "naive-nanoseconds", "exact-wall-ticks"],
)
def test_timestamp_key_without_destination_zone_keeps_unresolved_wall_identity(wall):
    identity = dataframe_key_identity(wall, "DateTime64(9)")

    assert identity is wall
    if isinstance(wall, ExactTemporal):
        assert identity.wall_time is True
        assert identity.timezone is None
        assert identity.ticks == 1_700_000_000_000_000_123
        assert identity.scale == 9
    elif isinstance(wall, pd.Timestamp):
        assert identity.tzinfo is None
        assert identity.nanosecond == 123
    else:
        assert identity.tzinfo is None
        assert identity.fold == 1


def test_two_aware_key_columns_share_a_validation_copy_without_changing_caller_cells():
    zone = ZoneInfo("America/New_York")
    folds = [
        dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=0),
        dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=1),
    ]
    expected_precise = [
        pd.Timestamp("2024-11-03T05:30:00.000000123Z"),
        pd.Timestamp("2024-11-03T06:30:00.000000456Z"),
    ]
    precise = [value.tz_convert("Europe/Moscow") for value in expected_precise]
    payloads = [{"row": [7]}, {"row": [3]}]
    index = pd.Index([7, 3], name="selected_row")
    frame = pd.DataFrame(
        {
            "event_time": pd.Series(folds, index=index, dtype=object),
            "processed_time": pd.Series(precise, index=index, dtype=object),
            "payload": pd.Series(payloads, index=index, dtype=object),
        }
    )
    original = frame.copy(deep=True)
    assert folds[0] == folds[1]

    validation = source_key_validation_frame(frame, ["event_time", "processed_time"])

    assert validation is not frame
    pd.testing.assert_index_equal(validation.index, frame.index)
    pd.testing.assert_index_equal(validation.columns, frame.columns)
    expected_folds = [
        dt.datetime(2024, 11, 3, 5, 30, tzinfo=dt.timezone.utc),
        dt.datetime(2024, 11, 3, 6, 30, tzinfo=dt.timezone.utc),
    ]
    assert validation.event_time.tolist() == expected_folds
    assert not validation.event_time.duplicated().any()
    assert validation.processed_time.tolist() == expected_precise
    assert [value.nanosecond for value in validation.processed_time] == [123, 456]
    assert all(value.utcoffset() == dt.timedelta(0) for value in validation.event_time)
    assert all(value.utcoffset() == dt.timedelta(0) for value in validation.processed_time)
    for position in range(len(frame)):
        assert frame.event_time.iloc[position] is folds[position]
        assert frame.event_time.iloc[position].fold == position
        assert frame.processed_time.iloc[position] is precise[position]
        assert frame.payload.iloc[position] is payloads[position]
        assert validation.payload.iloc[position] is payloads[position]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("native", ["DateTime32('UTC')", "TIMESTAMP('UTC')", "timestamp(0, 'UTC')"])
def test_timestamp_alias_key_identity_matches_physical_second_assignment(native):
    instant = dt.datetime(1970, 1, 1, 0, 0, 1, tzinfo=dt.timezone.utc)

    assert dataframe_key_identity(1, native) == dataframe_key_identity(instant, native)
    assert dataframe_key_identity(0, native) != dataframe_key_identity(instant, native)
