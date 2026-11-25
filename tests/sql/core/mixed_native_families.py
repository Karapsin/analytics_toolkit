"""Inference proofs and replay conversion keep the complete native value domain."""

from __future__ import annotations

import datetime as dt
import struct
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.dataframe_types import (
    inferred_dataframe_values,
    resolve_dataframe_column,
)
from analytics_toolkit.sql.backends.trino.time_values import convert_time_value
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import ExactClock
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from analytics_toolkit.sql.dml.load.type_validation import convert_dataframe_schema


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("gp", "NUMERIC(71, 55)"), ("ch", "Decimal(71, 55)"), ("trino", "VARCHAR")],
)
def test_inference_uses_the_exact_binary_fraction_and_all_integer_digits(backend, expected):
    values = pd.Series([2**53 + 1, 0.1], dtype=object)
    resolution = resolve_dataframe_column(values, backend)
    assert resolution.target_type == expected
    if backend == "trino":
        assert resolution.text_fallback
    else:
        assert resolution.convert(0.1) == Decimal.from_float(0.1)
        assert resolution.convert(0.1) != Decimal("0.1")
        assert resolution.convert(2**53 + 1) == Decimal(2**53 + 1)
    assert resolution.certified_input_count == 2
    assert resolution.metadata()["conversion_policy"] == "inferred_exact"


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_native_float_keeps_representable_large_integers_and_negative_zero(backend):
    resolution = resolve_dataframe_column(pd.Series([2**60, 1.5, -0.0], dtype=object), backend)
    assert (
        resolution.target_type
        == {"gp": "DOUBLE PRECISION", "trino": "DOUBLE", "ch": "Float64"}[backend]
    )
    assert int(resolution.convert(2**60)) == 2**60
    assert struct.pack(">d", resolution.convert(-0.0)) == bytes.fromhex("8000000000000000")
    incompatible = resolve_dataframe_column(pd.Series([2**60 + 1, -0.0], dtype=object), backend)
    assert incompatible.text_fallback


def test_profile_merge_combines_all_numeric_facts_without_changing_raw_replay():
    first, last = ColumnProfile(), ColumnProfile()
    first.add(2**53 + 1)
    last.add(0.1)
    first.merge(last)
    resolution = resolve_profile(first, "gp")
    assert resolution.target_type == "NUMERIC(71, 55)"
    with ReplaySpool(["value"]) as spool:
        spool.write_rows([(2**53 + 1,), (0.1,)])
        spool.certify()
        raw = list(spool.iter_rows())
    assert type(raw[0][0]) is int
    assert type(raw[1][0]) is float
    assert resolution.convert(raw[1][0]) == Decimal.from_float(0.1)


def test_nested_inferred_conversion_retains_float_bits_and_nulls_before_decimal_binding():
    payload = [2**53 + 1, 0.1, None]
    resolution = resolve_dataframe_column(pd.Series([payload], dtype=object), "gp")
    assert resolution.target_type == "NUMERIC(71, 55)[]"
    assert resolution.convert(payload) == [Decimal(2**53 + 1), Decimal.from_float(0.1), None]
    assert payload == [2**53 + 1, 0.1, None]


def test_inferred_dataframe_view_preserves_positions_labels_index_and_authority_policy():
    frame = pd.DataFrame(
        {
            0: pd.Series([2**53 + 1, 0.1], dtype=object),
            1: pd.Series(["first", "last"], dtype=object),
        }
    )
    frame.columns = ["same", "same"]
    frame.index = [7, 9]
    original = frame.copy(deep=True)
    resolutions = [resolve_dataframe_column(frame.iloc[:, position], "gp") for position in range(2)]
    converted = inferred_dataframe_values(frame, resolutions)
    assert list(converted.columns) == ["same", "same"]
    assert list(converted.index) == [7, 9]
    assert converted.iloc[1, 0] == Decimal.from_float(0.1)
    pd.testing.assert_frame_equal(frame, original)
    requested, _ = convert_dataframe_schema(
        pd.DataFrame({"value": [0.1]}), {"value": "NUMERIC(71,55)"}, "gp"
    )
    assert requested.iloc[0, 0] == Decimal("0.1")
    with pytest.raises(ValueError, match="conversion plan"):
        inferred_dataframe_values(frame, resolutions[:1])


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_inferred_date_midnight_does_not_invent_awareness(backend):
    date = dt.date(2025, 1, 1)
    naive = dt.datetime.fromisoformat("2025-01-02T03:04:05.123456")
    resolution = resolve_dataframe_column(pd.Series([date, naive, None], dtype=object), backend)
    assert not resolution.text_fallback
    assert resolution.convert(date) == dt.datetime.fromisoformat("2025-01-01T00:00:00")
    assert resolution.convert(naive) == naive
    aware = naive.replace(tzinfo=dt.timezone.utc)
    ambiguous = resolve_dataframe_column(pd.Series([date, aware], dtype=object), backend)
    assert ambiguous.text_fallback


def test_gp_day_end_clock_is_adaptive_text_or_rejected_while_valid_rounding_can_carry():
    day_end = ExactClock(86400 * 10**6, 6)
    profile = ColumnProfile(native_type="TIME(6)", source_backend="gp")
    profile.add(day_end)
    inferred = resolve_profile(profile, "trino", source_type="TIME(6)", source_backend="gp")
    assert inferred.text_fallback
    assert "clock_ticks" in inferred.convert(day_end)
    assert not declared_domain_fits("TIME(6)", "TIME(6)", "trino", source_backend="gp")
    assert not declared_domain_fits(
        "TIME(6) WITH TIME ZONE", "TIME(6) WITH TIME ZONE", "trino", source_backend="gp"
    )
    assert not declared_domain_fits("TIME(6)[]", "ARRAY(TIME(6))", "trino", source_backend="gp")
    with pytest.raises(ValueError, match="Trino TIME"):
        validate_profile(profile, "TIME(6)", "trino")
    with pytest.raises(ValueError, match="24:00"):
        convert_time_value(day_end, "TIME(0)")
    before_midnight = ExactClock(86400 * 10**9 - 1, 9)
    assert convert_time_value(before_midnight, "TIME(0)") == dt.time()
    assert before_midnight.ticks == 86400 * 10**9 - 1
