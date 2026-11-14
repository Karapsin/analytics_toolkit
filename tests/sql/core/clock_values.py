"""Clock and duration values retain exact identity in certified replay frames."""

from __future__ import annotations

import datetime as dt
import json
import struct

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import (
    ENCODING_VERSION,
    MAX_EXACT_SCALE,
    MAX_TEMPORAL_SCALE,
    ExactClock,
    OrderedMapPairs,
    dataframe_from_rows,
    decode_value,
    encode_value,
    typed_text,
)
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from analytics_toolkit.sql.core.temporal_zones import canonical_named_zone
from analytics_toolkit.sql.core.type_resolution import (
    ResolvedColumn,
    resolve_profile,
    validate_profile,
)


@pytest.mark.parametrize(
    "value",
    [
        dt.time.min,
        dt.time.max,
        dt.time(1, 2, 3, 456789, fold=1),
        dt.time(1, 2, 3, 456789, tzinfo=dt.timezone.utc),
        dt.time(1, 2, 3, 456789, tzinfo=dt.timezone(dt.timedelta(hours=-7))),
        dt.time(
            1,
            2,
            3,
            456789,
            tzinfo=dt.timezone(dt.timedelta(seconds=1, microseconds=2), "precise offset"),
            fold=1,
        ),
        dt.time(1, 30, tzinfo=canonical_named_zone("America/New_York"), fold=1),
    ],
)
def test_clock_roundtrips_real_spool_without_inventing_a_date(value):
    with ReplaySpool(["clock"]) as spool:
        spool.write_rows([(value,), (None,)])
        spool.certify()
        rows = list(spool.iter_rows())
        replayed = rows[0][0]
        assert isinstance(replayed, dt.time)
        assert replayed.isoformat() == value.isoformat()
        assert replayed.fold == value.fold
        assert replayed.utcoffset() == value.utcoffset()
        assert replayed.tzname() == value.tzname()
        assert getattr(replayed.tzinfo, "key", None) == getattr(value.tzinfo, "key", None)
        assert (replayed.tzinfo is None) == (value.tzinfo is None)
        assert rows[1] == (None,)
        assert list(spool.iter_rows()) == rows


@pytest.mark.parametrize(
    "value",
    [
        dt.timedelta(),
        dt.timedelta(microseconds=1),
        dt.timedelta(microseconds=-1),
        dt.timedelta(days=-2, seconds=3, microseconds=4),
        dt.timedelta.max,
        dt.timedelta.min,
    ],
)
def test_python_duration_roundtrips_extremes_as_exact_integer_components(value):
    with ReplaySpool(["elapsed"]) as spool:
        spool.write_rows([(value,)])
        spool.certify()
        replayed = next(spool.iter_rows())[0]
        assert type(replayed) is dt.timedelta
        assert replayed == value
        assert (replayed.days, replayed.seconds, replayed.microseconds) == (
            value.days,
            value.seconds,
            value.microseconds,
        )


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
@pytest.mark.parametrize("ticks", [-123456789, 1, 123456789])
def test_pandas_duration_retains_asm8_ticks_and_unit_through_real_spool(unit, ticks):
    value = pd.Timedelta(np.timedelta64(ticks, unit))
    with ReplaySpool(["elapsed"]) as spool:
        spool.write_rows([(value,), (pd.NaT,)])
        spool.certify()
        rows = list(spool.iter_rows())
        replayed = rows[0][0]
        assert isinstance(replayed, pd.Timedelta)
        assert replayed.asm8.dtype == value.asm8.dtype
        assert int(replayed.asm8.view(np.int64)) == int(value.asm8.view(np.int64))
        assert rows[1] == (None,)


def test_version_one_binary_frames_keep_nested_clock_duration_values_and_ordered_maps():
    clock = dt.time(23, 59, 59, 999999, tzinfo=dt.timezone.utc, fold=1)
    precise = pd.Timedelta(1, unit="ns")
    elapsed = dt.timedelta(days=-1, microseconds=1)
    mapping = OrderedMapPairs(((clock, precise), (clock, elapsed)))
    row = ([clock, None, precise], {"elapsed": elapsed}, mapping, None)
    with ReplaySpool(["array", "map", "ordered", "null"]) as spool:
        spool.write_rows([row])
        spool.certify()
        raw = spool.path.read_bytes()
        magic = b"ATK-ROWS\x01\n"
        assert ENCODING_VERSION == 1
        assert raw.startswith(magic)
        header_end = len(magic) + struct.calcsize(">Q")
        size = struct.unpack(">Q", raw[len(magic) : header_end])[0]
        assert size == len(raw) - header_end
        tagged = json.loads(raw[header_end:])
        assert tagged[0] == "tuple"
        assert tagged[1][0][1][0][0] == "time"
        assert tagged[1][0][1][2][0] == "pandas_timedelta"
        replayed = next(spool.iter_rows())
        assert replayed == row
        assert replayed[0][0].fold == 1
        assert replayed[0][2].asm8 == precise.asm8
        assert isinstance(replayed[2], OrderedMapPairs)
        assert len(replayed[2]) == len(mapping)


@pytest.mark.parametrize(
    "value",
    [dt.time(1, 2, 3, 456789), dt.timedelta(microseconds=-1), pd.Timedelta(1, unit="ns")],
)
def test_clock_and_duration_text_fallback_is_versioned_typed_json(value):
    text = typed_text(value, source_backend="gp", source_type="source clock")
    encoded = json.loads(text)
    assert encoded["v"] == 1
    assert encoded["backend"] == "gp"
    assert encoded["type"] == "source clock"
    assert decode_value(encoded["value"]) == value
    assert typed_text(None) is None
    assert typed_text(pd.NaT) is None


class _UnboundClockZone(dt.tzinfo):
    def utcoffset(self, value):
        return None

    def tzname(self, value):
        return None


def test_unbound_clock_timezone_rejects_instead_of_guessing_a_date():
    with pytest.raises(TypeError, match="without inventing a date"):
        encode_value(dt.time(1, 2, tzinfo=_UnboundClockZone()))
    with pytest.raises(ValueError, match="Unknown exact clock timezone"):
        decode_value(["time", "01:02:00", ["future-zone"], 0])


def test_fixed_clock_offset_without_a_label_remains_aware_and_exact():
    replayed = decode_value(["time", "01:02:00", ["fixed", -1, 86399, 999999, None], 1])
    assert replayed.utcoffset() == dt.timedelta(microseconds=-1)
    assert replayed.replace(tzinfo=None) == dt.time(1, 2, fold=1)
    assert replayed.fold == 1


def test_new_tags_keep_existing_version_one_scalar_decoding_compatible():
    assert decode_value(["date", "1900-01-01"]) == dt.date(1900, 1, 1)
    assert decode_value(["int", "9007199254740993"]) == 2**53 + 1
    assert decode_value(["null"]) is None


class _DeclaredClockZone(dt.tzinfo):
    def __init__(self, *, key, offset, name):
        self.key = key
        self.offset = offset
        self.name = name

    def utcoffset(self, _value):
        return self.offset

    def tzname(self, _value):
        return self.name


class _ChangingClockZone(dt.tzinfo):
    def __init__(self, key):
        self.key = key
        self.offset_calls = 0
        self.name_calls = 0

    def utcoffset(self, _value):
        self.offset_calls += 1
        return dt.timedelta(hours=3 if self.offset_calls == 1 else 7)

    def tzname(self, _value):
        self.name_calls += 1
        return "captured clock zone" if self.name_calls == 1 else "changed clock zone"


def _roundtrip_clock(value, route):
    if route == "spool":
        with ReplaySpool(["clock"]) as spool:
            spool.write_rows([(value,)])
            spool.certify()
            replayed = next(spool.iter_rows())[0]
            assert next(spool.iter_rows())[0] == replayed
            return replayed
    if route == "text":
        document = json.loads(typed_text(value, source_backend="trino", source_type="TIME(6)"))
        assert document["v"] == 1
        assert document["backend"] == "trino"
        assert document["type"] == "TIME(6)"
        return decode_value(document["value"])
    return decode_value(encode_value(value))


@pytest.mark.parametrize("route", ["tagged", "text", "spool"])
@pytest.mark.parametrize(
    ("offset", "name"),
    [
        (dt.timedelta(hours=3), "UTC"),
        (dt.timedelta(), "custom zero offset"),
    ],
    ids=["offset-conflicts-with-key", "name-conflicts-with-key"],
)
def test_clock_named_key_conflicts_preserve_captured_offset_and_label(route, offset, name):
    zone = _DeclaredClockZone(key="UTC", offset=offset, name=name)
    value = dt.time(12, 34, 56, 123456, tzinfo=zone, fold=1)

    replayed = _roundtrip_clock(value, route)

    assert isinstance(replayed, dt.time)
    assert replayed.replace(tzinfo=None) == dt.time(12, 34, 56, 123456, fold=1)
    assert replayed.utcoffset() == offset
    assert replayed.tzname() == name
    assert replayed.fold == 1


@pytest.mark.parametrize("route", ["tagged", "text", "spool"])
@pytest.mark.parametrize("key", [None, "UTC"], ids=["offset-only", "conflicting-named-key"])
def test_clock_callbacks_are_captured_once_before_encoding_and_native_replay(route, key):
    zone = _ChangingClockZone(key)
    value = dt.time(12, 34, 56, 123456, tzinfo=zone, fold=1)

    replayed = _roundtrip_clock(value, route)
    native = ResolvedColumn("TIME(6) WITH TIME ZONE", target_backend="trino").convert(replayed)

    assert isinstance(replayed, dt.time)
    assert replayed.utcoffset() == dt.timedelta(hours=3)
    assert replayed.tzname() == "captured clock zone"
    assert replayed.fold == 1
    assert isinstance(native, dt.time)
    assert native.isoformat() == "12:34:56.123456+03:00"
    assert native.fold == 1
    assert zone.offset_calls == 1
    assert zone.name_calls == 1


@pytest.mark.parametrize("name", ["UTC", "America/New_York"])
def test_genuine_named_clock_zone_keeps_its_version_one_named_descriptor(name):
    value = dt.time(1, 30, 0, 123456, tzinfo=canonical_named_zone(name), fold=1)

    encoded = encode_value(value)

    assert encoded[2] == ["named", name]
    replayed = decode_value(encoded)
    assert getattr(replayed.tzinfo, "key", None) == name
    assert replayed.utcoffset() == value.utcoffset()
    assert replayed.tzname() == value.tzname()
    assert replayed.fold == 1


def test_unlabelled_fixed_clock_offset_encodes_null_label_and_replays_exact_clock():
    offset = dt.timedelta(hours=3, minutes=30)
    value = dt.time(
        12,
        34,
        56,
        123456,
        tzinfo=_DeclaredClockZone(key=None, offset=offset, name=None),
        fold=1,
    )

    encoded = encode_value(value)
    replayed = _roundtrip_clock(value, "spool")

    assert encoded[2] == ["fixed", 0, 12600, 0, None]
    assert replayed.tzinfo is not None
    assert replayed.utcoffset() == offset
    assert replayed.replace(tzinfo=None) == dt.time(12, 34, 56, 123456, fold=1)
    assert replayed.fold == 1


@pytest.mark.parametrize("route", ["tagged", "text", "spool"])
def test_unknown_clock_zone_key_still_rejects_and_cleans_partial_spool(route):
    zone = _DeclaredClockZone(
        key="Invalid/Definitely_Not_A_Timezone", offset=dt.timedelta(hours=3), name="custom zone"
    )
    value = dt.time(12, 34, tzinfo=zone)
    if route == "spool":
        with ReplaySpool(["clock"]) as spool:
            path = spool.path
            with pytest.raises(ValueError, match="timezone"):
                spool.write_rows([(value,)])
            assert spool.closed
        assert not path.exists()
    else:
        with pytest.raises(ValueError, match="timezone"):
            _roundtrip_clock(value, route)


@pytest.mark.parametrize("scale", range(13))
def test_exact_clock_model_preserves_each_scale_without_an_invented_date(scale):
    ticks = 3723 * 10**scale + (1 if scale else 0)
    value = ExactClock(ticks, scale)

    expected = "01:02:03" + (f".{1:0{scale}d}" if scale else "")
    assert value.isoformat() == expected
    assert value.ticks_floor(12) == ticks * 10 ** (12 - scale)
    actual = value.time()
    if scale > 6:
        assert actual is value
    else:
        assert type(actual) is dt.time
        assert actual == dt.time(1, 2, 3, 10 ** (6 - scale) if scale else 0)
    assert value.offset_minutes is None


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"ticks": 0, "scale": -1}, "between zero and twelve"),
        ({"ticks": 0, "scale": 13}, "between zero and twelve"),
        ({"ticks": -1, "scale": 12}, "within one day"),
        ({"ticks": 86400 * 10**12, "scale": 12}, "within one day"),
        ({"ticks": 0, "scale": 12, "offset_minutes": -1440}, "within one day"),
        ({"ticks": 0, "scale": 12, "offset_minutes": 1440}, "within one day"),
        ({"ticks": 0, "scale": 12, "timezone_name": "unbound label"}, "fixed UTC offset"),
    ],
)
def test_exact_clock_constructor_rejects_unrepresentable_coordinates(arguments, message):
    with pytest.raises(ValueError, match=message):
        ExactClock(**arguments)


@pytest.mark.parametrize("scale", range(7, 13))
def test_exact_clock_with_only_zero_submicrosecond_digits_uses_python_time(scale):
    ticks = 3723 * 10**scale + 123456 * 10 ** (scale - 6)
    value = ExactClock(ticks, scale, 0, "captured UTC")

    actual = value.time()

    assert type(actual) is dt.time
    assert actual.replace(tzinfo=None) == dt.time(1, 2, 3, 123456)
    assert actual.utcoffset() == dt.timedelta()
    assert actual.tzname() == "captured UTC"
    assert value.ticks == ticks
    assert value.scale == scale


@pytest.mark.parametrize(
    ("offset", "name", "suffix"),
    [(None, None, ""), (0, None, "+00:00"), (330, "captured +05:30", "+05:30")],
)
def test_exact_clock_tag_preserves_naive_versus_aware_and_captured_zone_name(offset, name, suffix):
    value = ExactClock(1, 12, offset, name)

    tagged = encode_value(value)
    replayed = decode_value(tagged)

    assert tagged == ["clock_ticks", "1", 12, offset, name]
    assert replayed == value
    assert replayed.offset_minutes == offset
    assert replayed.timezone_name == name
    assert replayed.isoformat() == f"00:00:00.000000000001{suffix}"


def test_version_one_real_spool_retains_nested_picosecond_clocks_and_sql_null():
    naive, aware = ExactClock(1, 12), ExactClock(2, 12, 0, "captured UTC")
    mapping = OrderedMapPairs(((naive, [aware, None]), (naive, [naive, None])))
    row = ([naive, None, aware], {"clock": aware}, mapping, None)

    with ReplaySpool(["array", "map", "ordered", "null"]) as spool:
        spool.write_rows([row, (None, None, None, pd.NaT)])
        spool.certify()
        raw = spool.path.read_bytes()
        magic = b"ATK-ROWS\x01\n"
        size_start = len(magic)
        frame_start = size_start + struct.calcsize(">Q")
        size = struct.unpack(">Q", raw[size_start:frame_start])[0]
        tagged = json.loads(raw[frame_start : frame_start + size])
        rows = list(spool.iter_rows())

        assert ENCODING_VERSION == 1
        assert raw.startswith(magic)
        assert tagged[1][0][1][0] == ["clock_ticks", "1", 12, None, None]
        assert tagged[1][0][1][2] == ["clock_ticks", "2", 12, 0, "captured UTC"]
        assert rows[0] == row
        assert isinstance(rows[0][2], OrderedMapPairs)
        assert len(rows[0][2]) == 2
        assert rows[1] == (None, None, None, None)
        assert list(spool.iter_rows()) == rows


def test_positional_dataframe_retains_picosecond_clocks_and_duplicate_labels():
    first, second = ExactClock(1, 12), ExactClock(2, 12, 0)
    rows = [(first, second), (None, None), (second, first)]

    frame = dataframe_from_rows(["clock", "clock"], rows)

    assert list(frame.columns) == ["clock", "clock"]
    assert frame.dtypes.tolist() == [object, object]
    assert list(frame.itertuples(index=False, name=None)) == rows
    assert frame.iloc[0, 0] is first
    assert frame.iloc[0, 1] is second


@pytest.mark.parametrize(
    ("ticks", "rounded"), [(1, "00:00:00.000000"), (500000, "00:00:00.000001")]
)
def test_clock_profiles_retain_precision_before_explicit_native_rounding(ticks, rounded):
    value = ExactClock(ticks, 12)
    profile = ColumnProfile()
    profile.add(None)
    profile.add(value)

    assert profile.families == {"time"}
    assert profile.temporal_scale > 6
    validate_profile(profile, "TIME(12)", "trino", allow_timestamp_precision_loss=False)
    with pytest.raises(ValueError, match="precision"):
        validate_profile(profile, "TIME(6)", "gp", allow_timestamp_precision_loss=False)
    validate_profile(profile, "TIME(6)", "gp")
    assert ResolvedColumn("TIME(6)", target_backend="gp").convert(value) == rounded


@pytest.mark.parametrize(
    ("backend", "native"), [("gp", "TEXT"), ("trino", "VARCHAR"), ("ch", "String")]
)
def test_unrepresentable_inferred_clock_uses_exact_typed_text_without_ch_precision_expansion(
    backend, native
):
    value = ExactClock(1, 12)
    profile = ColumnProfile()
    profile.add(value)

    resolved = resolve_profile(profile, backend)
    document = json.loads(resolved.convert(value))

    assert resolved.target_type == native
    assert resolved.text_fallback
    assert document["v"] == 1
    assert decode_value(document["value"]) == value
    assert resolved.convert(None) is None
    assert MAX_EXACT_SCALE == 12
    assert MAX_TEMPORAL_SCALE == 9
