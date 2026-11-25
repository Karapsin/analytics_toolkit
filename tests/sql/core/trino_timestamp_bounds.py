"""Exact SPI slot limits, not Python's calendar, constrain native timestamps."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil
from analytics_toolkit.sql.backends.trino.timestamp_bounds import validate_timestamp_profile
from analytics_toolkit.sql.backends.trino.timestamp_capabilities import (
    require_timestamp_capabilities,
)
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile
from analytics_toolkit.sql.core.exact_values import ExactTemporal


def _profile(value):
    profile = ColumnProfile()
    profile.add(value)
    return profile


@pytest.mark.parametrize("aware", [False, True])
def test_trino_long_timestamp_slots_keep_the_last_picosecond_and_reject_adjacent_overflow(aware):
    bits, per_unit = (52, 10**9) if aware else (64, 10**6)
    native = "TIMESTAMP(12)" + (" WITH TIME ZONE" if aware else "")
    zone = "UTC" if aware else None
    low = -(2 ** (bits - 1)) * per_unit
    high = 2 ** (bits - 1) * per_unit - 1
    for ticks in (low, high):
        value = ExactTemporal(ticks, 12, zone, wall_time=not aware)
        validate_timestamp_profile(_profile(value), native)
        assert value.ticks == ticks
    for ticks in (low - 1, high + 1):
        with pytest.raises(ValueError, match="native Trino timestamp coordinate"):
            validate_timestamp_profile(
                _profile(ExactTemporal(ticks, 12, zone, wall_time=not aware)), native
            )


@pytest.mark.parametrize("precision", [0, 3, 6, 9, 12])
@pytest.mark.parametrize("year", [0, 10000])
def test_qualified_expanded_calendars_remain_native_with_and_without_timezone(precision, year):
    ticks = days_from_civil(year, 1, 1) * 86400 * 10**precision
    validate_timestamp_profile(
        _profile(ExactTemporal(ticks, precision, wall_time=True)), f"TIMESTAMP({precision})"
    )
    validate_timestamp_profile(
        _profile(ExactTemporal(ticks, precision, "UTC")), f"TIMESTAMP({precision}) WITH TIME ZONE"
    )


def test_zoned_long_storage_retains_its_packed_millisecond_limit():
    ticks = days_from_civil(100000, 1, 1) * 86400 * 10**12
    validate_timestamp_profile(_profile(ExactTemporal(ticks, 12, wall_time=True)), "TIMESTAMP(12)")
    with pytest.raises(ValueError, match="native Trino timestamp coordinate"):
        validate_timestamp_profile(
            _profile(ExactTemporal(ticks, 12, "UTC")), "TIMESTAMP(12) WITH TIME ZONE"
        )


def test_unresolved_wall_bounds_are_conservative_without_inventing_a_timezone():
    value = ExactTemporal((2**51 - 1) * 10**6, 9, wall_time=True)
    with pytest.raises(ValueError, match="Resolve destination timezone"):
        validate_timestamp_profile(_profile(value), "TIMESTAMP(9) WITH TIME ZONE")


def test_capability_planning_and_ordinary_values_never_open_a_connection():
    class NoConnection:
        def cursor(self):
            message = "Planning or ordinary timestamps must not probe server metadata."
            raise AssertionError(message)

    cache = {}
    ordinary = _profile(ExactTemporal(1, 9))
    assert (
        require_timestamp_capabilities(
            NoConnection(),
            "catalog.schema.target",
            {"value": "TIMESTAMP(9)"},
            {"value": ordinary},
            cache,
        )
        is None
    )
    wide = _profile(ExactTemporal(days_from_civil(10000, 1, 1) * 86400 * 10**9, 9))
    pending = require_timestamp_capabilities(
        NoConnection(),
        "catalog.schema.target",
        {"value": "TIMESTAMP(9)"},
        {"value": wide},
        cache,
        dry_run=True,
    )
    assert pending["pending"]
    assert pending["columns"] == ["value"]
    assert cache["pending_timestamp_capabilities"] == pending
