"""DATE inputs need exact midnight ticks before timestamp validation and writes."""

import datetime as dt

import pytest
from analytics_toolkit.sql.backends.temporal_values import timestamp_value, validate_timestamp_range
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)


@pytest.mark.parametrize(
    ("value", "native", "valid"),
    [
        (dt.date(1800, 1, 1), "DateTime64(6)", False),
        (dt.date(1900, 1, 1), "DateTime64(6)", True),
        (dt.date(2299, 12, 31), "DateTime64(6)", True),
        (dt.date(2300, 1, 1), "DateTime64(6)", False),
        (dt.date(2262, 4, 11), "DateTime64(9)", True),
        (dt.date(2262, 4, 12), "DateTime64(9)", False),
        (dt.date(1900, 1, 1), "DateTime('UTC')", False),
        (dt.date(1970, 1, 1), "DateTime('UTC')", True),
        (dt.date(2106, 2, 7), "DateTime('UTC')", True),
        (dt.date(2106, 2, 8), "DateTime('UTC')", False),
    ],
)
def test_authoritative_date_to_timestamp_range_and_midnight_payload(value, native, valid):
    profile = ColumnProfile()
    profile.add(None)
    profile.add(value)
    if not valid:
        with pytest.raises(ValueError, match="timestamp range"):
            validate_profile(profile, f"Nullable({native})", "ch", destination_timezone="UTC")
        return
    validate_profile(profile, f"Nullable({native})", "ch", destination_timezone="UTC")
    converted = ResolvedColumn(native, target_backend="ch", destination_timezone="UTC").convert(
        value
    )
    assert isinstance(converted, dt.datetime)
    assert converted.date() == value
    assert converted.hour == converted.minute == converted.second == converted.microsecond == 0
    assert converted.utcoffset() == dt.timedelta(0)


def test_date_only_extrema_merge_without_changing_date_family():
    first = ColumnProfile()
    first.add(dt.date(2024, 1, 1))
    first.add(dt.date(2025, 1, 1))
    second = ColumnProfile()
    second.add(dt.date(1800, 1, 1))
    second.add(dt.date(1801, 1, 1))
    first.merge(second)
    assert first.minimum_date_only == dt.date(1800, 1, 1)
    assert first.maximum_date_only == dt.date(2025, 1, 1)
    assert first.families == {"date"}
    validate_profile(first, "DATE", "gp")
    with pytest.raises(ValueError, match="timestamp range"):
        validate_profile(first, "DateTime64(6)", "ch")


def test_timestamp_dates_and_real_timestamp_bounds_are_combined_exactly():
    profile = ColumnProfile()
    profile.add(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc))
    profile.add(dt.date(2262, 4, 12))
    with pytest.raises(ValueError, match="timestamp range"):
        validate_profile(profile, "DateTime64(9, 'UTC')", "ch")
    value = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    assert timestamp_value(value, "DateTime64(9)") is value
    assert timestamp_value(1, "DateTime64(9)") == 1
    assert timestamp_value(
        dt.date(2024, 1, 1), "DateTime64(6, 'Europe/Moscow')"
    ).utcoffset() == dt.timedelta(hours=3)
    for native in ["DateTime64(6, unknown)", "DateTime64(6, 3)", "DateTime64(6, 'Unknown/Zone')"]:
        with pytest.raises(ValueError, match="timezone"):
            timestamp_value(dt.date(2024, 1, 1), native)
    validate_timestamp_range(ColumnProfile(), "DateTime64(9)")
    with pytest.raises(ValueError, match="scale"):
        validate_timestamp_range(ColumnProfile(), "DateTime64(10)")
