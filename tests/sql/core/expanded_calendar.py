"""Wide native timestamp coordinates remain exact through shared checkpoints."""

from __future__ import annotations

import datetime as dt

import pytest
from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal, typed_text
from analytics_toolkit.sql.core.replay_spool import ReplaySpool


@pytest.mark.parametrize("year", [-1, 0, 10000])
@pytest.mark.parametrize("scale", [0, 6, 9, 12])
def test_expanded_coordinates_replay_profile_and_native_conversion_without_bounded_calendar(
    year, scale
):
    ticks = days_from_civil(year, 1, 1) * 86400 * 10**scale + (1 if scale else 0)
    value = ExactTemporal(ticks, scale, wall_time=True)
    year_text = "-0001" if year == -1 else "0000" if year == 0 else "+10000"
    expected = year_text + "-01-01T00:00:00" + ("." + "0" * (scale - 1) + "1" if scale else "")

    assert value.isoformat() == expected
    assert typed_text(value) == expected
    with ReplaySpool(["stamp"]) as spool:
        spool.write_rows([(value,), (None,)])
        spool.certify()
        assert list(spool.iter_rows()) == [(value,), (None,)]
    profile = ColumnProfile()
    profile.add(value)
    assert profile.temporal_scale == scale
    assert profile.minimum_timestamp_ns == value.ticks_floor(9)
    assert profile.maximum_timestamp_ns == value.ticks_floor(9)
    assert profile.timestamp_calendar_outside_python
    assert profile.minimum_date is None
    validate_profile(profile, f"TIMESTAMP({scale})", "trino", allow_timestamp_precision_loss=False)
    resolved = ResolvedColumn(f"TIMESTAMP({scale})", target_backend="trino")
    assert resolved.convert(value) == value
    with pytest.raises(ValueError, match="timestamp range"):
        validate_profile(profile, "DateTime64(6)", "ch")


def test_wide_utc_normalization_keeps_fraction_across_the_calendar_boundary():
    ticks = days_from_civil(1, 1, 1) * 86400 * 10**9 - 14 * 3600 * 10**9 + 1
    value = ExactTemporal(ticks, 9, "+14:00")
    assert value.isoformat() == "0001-01-01T00:00:00.000000001+14:00"
    assert value.isoformat(utc=True) == "0000-12-31T10:00:00.000000001+00:00"
    assert typed_text(value) == value.isoformat(utc=True)


def test_expanded_calendar_does_not_invent_named_timezone_rules():
    value = ExactTemporal(days_from_civil(10000, 1, 1) * 86400, 0, "Europe/Paris")
    with pytest.raises(ValueError, match="project UTC"):
        value.isoformat()
    assert value.isoformat(utc=True) == "+10000-01-01T00:00:00+00:00"


def test_nanosecond_nat_bit_pattern_remains_an_exact_native_timestamp():
    value = ExactTemporal(-(2**63), 9)
    assert value.timestamp() is value
    assert value.isoformat() == "1677-09-21T00:12:43.145224192"


def test_profile_merge_retains_outside_calendar_evidence():
    profile = ColumnProfile()
    profile.add(dt.datetime.fromisoformat("2025-01-01T00:00:00"))
    expanded = ColumnProfile()
    expanded.add(ExactTemporal(days_from_civil(0, 1, 1) * 86400, 0))
    profile.merge(expanded)
    assert profile.count == 2
    assert profile.timestamp_calendar_outside_python
    with pytest.raises(ValueError, match="timestamp range"):
        validate_profile(profile, "DateTime64(0)", "ch")
