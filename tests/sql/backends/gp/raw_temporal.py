"""Raw instants and date-free clocks retain exact values through GP binding."""

import datetime as dt
import json

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.gp import insert
from analytics_toolkit.sql.backends.gp.temporal_values import (
    freeze_clock_interval_value,
    infer_clock_interval_type,
    validate_clock_interval_profile,
)
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import ExactClock, ExactTemporal, decode_value
from psycopg2.extensions import adapt

from tests.sql._support.gp_arrays import AdaptedConnection


@pytest.mark.parametrize(("scale", "offset"), [(9, None), (12, 0), (12, 180)])
def test_raw_exact_clock_aligned_wide_precision_remains_native_in_gp(scale, offset):
    raw = ExactClock((12 * 3600 + 30 * 60) * 10**scale + 123456 * 10 ** (scale - 6), scale, offset)
    native = "TIME(6)" + (" WITH TIME ZONE" if offset is not None else "")
    assert freeze_clock_interval_value(raw, native) is raw
    profile = ColumnProfile(native_type=native, source_backend="gp")
    profile.add(raw)
    assert profile.temporal_scale == 6
    assert validate_clock_interval_profile(profile, native, allow_timestamp_precision_loss=False)
    assert infer_clock_interval_type(profile) == native
    expected = "12:30:00.123456" + (
        "+00:00:00" if offset == 0 else "+03:00:00" if offset is not None else ""
    )
    assert ResolvedColumn(native, target_backend="gp").convert(raw) == expected
    assert (raw.scale, raw.offset_minutes) == (scale, offset)


@pytest.mark.parametrize("offset", [None, 0, 180])
def test_raw_exact_clock_picoseconds_require_explicit_precision_loss(offset):
    raw = ExactClock(1, 12, offset, "source-clock" if offset is not None else None)
    profile = ColumnProfile(source_backend="gp")
    profile.add(raw)
    assert profile.families == {"time"}
    assert profile.temporal_scale == 12
    assert profile.native_constraints_valid
    assert profile.aware == (offset is not None)
    assert profile.naive == (offset is None)
    assert infer_clock_interval_type(profile) is None
    with pytest.raises(ValueError, match="precision or fields"):
        validate_clock_interval_profile(profile, "TIME(6)", allow_timestamp_precision_loss=False)
    text = ResolvedColumn("TEXT", target_backend="gp", text_fallback=True).convert(raw)
    assert decode_value(json.loads(text)["value"]) == raw
    assert freeze_clock_interval_value(raw, "TIME(6)") is raw


@pytest.mark.parametrize(
    ("ticks", "scale", "precision", "expected"),
    [
        (1, 0, 6, "00:00:01.000000"),
        (15, 1, 0, "00:00:02.000000"),
        (123456789012, 12, 6, "00:00:00.123457"),
        (123456789012, 12, 3, "00:00:00.123000"),
        (999999500000, 12, 6, "00:00:01.000000"),
        (86400 * 10**12 - 1, 12, 0, "24:00:00.000000"),
    ],
)
def test_raw_exact_clock_explicit_gp_rounding_uses_integer_ticks(ticks, scale, precision, expected):
    raw = ExactClock(ticks, scale)
    assert ResolvedColumn(f"TIME({precision})", target_backend="gp").convert(raw) == expected
    assert (raw.ticks, raw.scale) == (ticks, scale)


@pytest.mark.parametrize("offset", [-959, 959])
def test_raw_exact_clock_gp_offset_domain_retains_existing_native_boundary(offset):
    raw = ExactClock(1, 0, offset)
    profile = ColumnProfile(source_backend="gp")
    profile.add(raw)
    assert validate_clock_interval_profile(
        profile, "TIMETZ(6)", allow_timestamp_precision_loss=False
    )
    assert (
        ResolvedColumn("TIMETZ(6)", target_backend="gp")
        .convert(raw)
        .endswith("-15:59:00" if offset < 0 else "+15:59:00")
    )


@pytest.mark.parametrize("offset", [-960, 960])
def test_raw_exact_clock_outside_gp_offset_domain_keeps_exact_text_fallback(offset):
    raw = ExactClock(1, 0, offset)
    assert freeze_clock_interval_value(raw, "TIMETZ(6)") is raw
    profile = ColumnProfile(source_backend="gp")
    profile.add(raw)
    assert infer_clock_interval_type(profile) is None
    with pytest.raises(ValueError, match="incompatible"):
        validate_clock_interval_profile(profile, "TIMETZ(6)")
    with pytest.raises(ValueError, match="offset"):
        ResolvedColumn("TIMETZ(6)", target_backend="gp").convert(raw)
    text = ResolvedColumn("TEXT", target_backend="gp", text_fallback=True).convert(raw)
    assert decode_value(json.loads(text)["value"]) == raw


def test_raw_exact_clock_scalar_and_native_arrays_bind_through_actual_psycopg():
    raw = ExactClock(123456000000, 12, 0)
    rounded = ExactClock(123456789012, 12, 0)
    native = "TIME(6) WITH TIME ZONE"
    scalar = ResolvedColumn(native, target_backend="gp").convert(raw)
    nested = ResolvedColumn(native + "[]", target_backend="gp").convert([raw, None, rounded])
    assert freeze_clock_interval_value([[raw]], native + "[]")[0][0] is raw
    connection = AdaptedConnection()
    get_backend("gp").insert_rows_batch(
        connection,
        "sandbox.clock_values",
        ["clock", "nested"],
        [[scalar, nested], [None, None]],
        target_column_types={"clock": native, "nested": native + "[]"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    statement = connection.target.statements[0]
    assert b"'00:00:00.123456+00:00:00'" in statement
    assert b"'00:00:00.123457+00:00:00'" in statement
    assert (
        b"CAST(ARRAY['00:00:00.123456+00:00:00',NULL,"
        b"'00:00:00.123457+00:00:00'] AS TIME(6) WITH TIME ZONE[])" in statement
    )
    assert connection.committed


def test_raw_instants_adapt_exactly_as_utc_scalar_and_nested_timestamp_arrays():
    raw = ExactTemporal(1704067200123456000, 9, "Europe/Moscow", offset_naive=True)
    expected = dt.datetime(2024, 1, 1, microsecond=123456, tzinfo=dt.timezone.utc)
    assert insert._normalize_uuid_scalar(raw) == expected
    assert (
        adapt(insert._normalize_uuid_scalar(raw)).getquoted()
        == b"'2024-01-01T00:00:00.123456+00:00'::timestamptz"
    )
    adapter = get_backend("gp")
    connection = AdaptedConnection()
    adapter.insert_dataframe_batch(
        connection,
        '"sandbox"."raw_times"',
        pd.DataFrame(
            {
                "value": pd.Series([raw], dtype=object),
                "nested": pd.Series([[[raw, None]]], dtype=object),
            }
        ),
        target_column_types={
            "value": "TIMESTAMP WITH TIME ZONE",
            "nested": "TIMESTAMP WITH TIME ZONE[][]",
        },
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    statement = connection.target.statements[0]
    assert statement.count(b"'2024-01-01T00:00:00.123456+00:00'::timestamptz") == 2
    assert b"TIMESTAMP WITH TIME ZONE[][]" in statement
    assert connection.committed
    assert insert._normalize_nested_scalar((raw, [raw])) == (expected, [expected])


def test_raw_submicroseconds_never_silently_truncate_in_gp_binding():
    raw = ExactTemporal(1704067200123456789, 9, "UTC")
    with pytest.raises(ValueError, match="microsecond precision"):
        insert._normalize_uuid_scalar(raw)
    narrowed = ResolvedColumn("TIMESTAMP(6) WITH TIME ZONE", target_backend="gp").convert(raw)
    assert narrowed == dt.datetime(2024, 1, 1, microsecond=123456, tzinfo=dt.timezone.utc)
    assert (
        adapt(insert._normalize_uuid_scalar(narrowed)).getquoted()
        == b"'2024-01-01T00:00:00.123456+00:00'::timestamptz"
    )


@pytest.mark.parametrize("wall_time", [False, True])
def test_raw_picosecond_input_aligned_to_microseconds_keeps_wall_or_instant_provenance(wall_time):
    raw = ExactTemporal(1_000_000, 12, wall_time=wall_time)
    expected = dt.datetime(1970, 1, 1, microsecond=1, tzinfo=None if wall_time else dt.timezone.utc)
    result = insert._normalize_uuid_scalar(raw)
    assert result == expected
    assert result.tzinfo is expected.tzinfo
    assert (b"::timestamp" if wall_time else b"::timestamptz") in adapt(result).getquoted()
    assert raw.wall_time is wall_time


@pytest.mark.parametrize("offset", [None, 0, 60])
def test_raw_exact_clock_binding_requires_exact_microsecond_alignment(offset):
    aligned = ExactClock(123456_000000, 12, offset)
    expected = dt.time(
        0,
        0,
        0,
        123456,
        tzinfo=None if offset is None else dt.timezone(dt.timedelta(minutes=offset)),
    )
    bound = insert._normalize_uuid_scalar(aligned)
    assert bound == expected
    assert bound.utcoffset() == expected.utcoffset()
    assert adapt(bound).getquoted()
    with pytest.raises(ValueError, match="Raw clock exceeds Greenplum microsecond precision"):
        insert._normalize_uuid_scalar(ExactClock(aligned.ticks + 1, 12, offset))


def test_raw_picosecond_timestamp_binding_rejects_remaining_precision_and_invalid_scale():
    for raw in (ExactTemporal(1, 12), ExactTemporal(0, 13)):
        with pytest.raises(
            ValueError, match="Raw timestamp exceeds Greenplum microsecond precision"
        ):
            insert._normalize_uuid_scalar(raw)
