"""Native clock key equality and lossless Python payloads for Trino maps."""

from __future__ import annotations

import datetime as dt
from enum import Enum

import pytest
from analytics_toolkit.sql.backends.trino.time_values import (
    convert_time_value,
    time_key_identity,
    time_key_value_and_identity,
)

from tests.sql._support.trino_parameters import render_parameter


def clock(hour, minute=0, second=0, microsecond=0, *, offset=None):
    zone = dt.timezone(dt.timedelta(hours=offset)) if offset is not None else None
    return dt.time(hour, minute, second, microsecond, tzinfo=zone)


@pytest.mark.parametrize(
    ("native", "left", "right", "identities"),
    [
        ("TIME(6)", clock(12, offset=0), clock(12, offset=1), (43_200_000_000,) * 2),
        ("TIME(6)", clock(12, offset=1), clock(11, offset=0), (43_200_000_000, 39_600_000_000)),
        (
            "TIME(3)",
            clock(12, microsecond=123400, offset=0),
            clock(13, microsecond=123499, offset=1),
            (43_200_123_000, 46_800_123_000),
        ),
        (
            "TIME(6) WITH TIME ZONE",
            clock(0, 30, offset=1),
            clock(23, 30, offset=0),
            (84_600_000_000,) * 2,
        ),
        (
            "TIME(6) WITH TIME ZONE",
            clock(23, 30, offset=-1),
            clock(0, 30, offset=0),
            (1_800_000_000,) * 2,
        ),
        (
            "TIME(12) WITH TIME ZONE",
            clock(12, microsecond=123456, offset=0),
            clock(13, microsecond=123456, offset=1),
            (43_200_123_456_000_000,) * 2,
        ),
        ("TIME(3)", clock(23, 59, 59, 999500), clock(0, microsecond=400), (0, 0)),
        ("TIME", clock(23, 59, 59, 999500), clock(0, microsecond=400), (0, 0)),
        (
            "TIME(3)",
            clock(23, 59, 59, 999499),
            clock(0, microsecond=500),
            (86_399_999_000, 1_000),
        ),
        (
            "TIME(0) WITH TIME ZONE",
            clock(23, 59, 59, 500000, offset=-14),
            clock(14, offset=0),
            (50_400_000_000,) * 2,
        ),
        (
            "TIME(6) WITHOUT TIME ZONE",
            clock(12, offset=-14),
            clock(12, offset=14),
            (43_200_000_000,) * 2,
        ),
    ],
)
def test_clock_key_identity_matches_native_cast_precision_timezone_and_day_wrap(
    native, left, right, identities
):
    results = [time_key_value_and_identity(value, native) for value in (left, right)]
    assert [item[0] for item in results] == [True, True]
    assert tuple(item[2] for item in results) == identities
    assert [time_key_identity(value, native) for value in (left, right)] == [
        (True, identity) for identity in identities
    ]
    if "WITH TIME ZONE" in native:
        # Zoned key payloads keep their native post-conversion offsets and wall
        # clocks. Only equality normalizes them; the stored spelling is retained.
        for original, (_handled, payload, _identity) in zip((left, right), results):
            assert render_parameter(payload) == render_parameter(
                convert_time_value(original, native)
            )
            assert payload.utcoffset() == original.utcoffset()
    else:
        assert all(payload.tzinfo is None for _handled, payload, _identity in results)
        assert (results[0][1] == results[1][1]) == (identities[0] == identities[1])


@pytest.mark.parametrize("native", ["TIME(6)", "TIME(6) WITHOUT TIME ZONE"])
def test_native_distinct_plain_clock_keys_survive_python_dict_and_real_sdk(native):
    originals = (clock(12, offset=1), clock(11, offset=0))
    assert originals[0] == originals[1]
    keys = [time_key_value_and_identity(value, native)[1] for value in originals]
    payload = dict(zip(keys, ("noon", "eleven")))

    assert len(payload) == 2
    assert render_parameter(payload) == (
        "MAP(ARRAY[TIME '12:00:00.000000',TIME '11:00:00.000000'], ARRAY['noon','eleven'])"
    )
    assert render_parameter(convert_time_value(originals[0], native)) == (
        "TIME '12:00:00.000000 +01:00'"
    )


def test_explicit_precision_cannot_erase_native_distinct_clock_keys_during_dict_creation():
    originals = (clock(12, microsecond=123400, offset=0), clock(13, microsecond=123499, offset=1))
    assert originals[0] != originals[1]
    assert convert_time_value(originals[0], "TIME(3)") == convert_time_value(
        originals[1], "TIME(3)"
    )
    keys = [time_key_value_and_identity(value, "TIME(3)")[1] for value in originals]
    payload = dict(zip(keys, ("noon", "one")))

    assert len(payload) == 2
    assert render_parameter(payload) == (
        "MAP(ARRAY[TIME '12:00:00.123000',TIME '13:00:00.123000'], ARRAY['noon','one'])"
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (clock(0, 30, offset=1), clock(23, 30, offset=0)),
        (clock(23, 30, offset=-1), clock(0, 30, offset=0)),
    ],
)
def test_zoned_clock_identity_does_not_depend_on_python_day_wrapping_hash(left, right):
    assert left != right
    assert len({left, right}) == 2
    assert time_key_identity(left, "TIME(6) WITH TIME ZONE") == time_key_identity(
        right, "TIME(6) WITH TIME ZONE"
    )


@pytest.mark.parametrize("native", ["TIME WITH TIME ZONE", "TIME(6) WITH TIME ZONE"])
def test_naive_zoned_map_key_cast_is_rejected_without_inventing_session_context(native):
    value = clock(1)
    with pytest.raises(ValueError, match="fixed UTC offset"):
        time_key_value_and_identity(value, native)
    assert render_parameter(convert_time_value(value, native)) == "TIME '01:00:00.000000'"
    assert time_key_identity(clock(1, offset=0), native) == (True, 3_600_000_000)


def test_clock_key_identity_keeps_existing_null_and_type_boundaries():
    class Values(Enum):
        NULL = None

    for value in (None, Values.NULL):
        with pytest.raises(ValueError, match="keys cannot be NULL"):
            time_key_value_and_identity(value, "TIME(6)")
    assert time_key_value_and_identity(1, "BIGINT") == (False, None, None)
    assert time_key_identity(1, None) == (False, None)
    with pytest.raises(ValueError, match="precision"):
        time_key_value_and_identity(clock(1), "TIME(13)")
