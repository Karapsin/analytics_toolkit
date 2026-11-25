"""Decoded native identity survives positional frames and certified replay."""

from __future__ import annotations

import struct
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.gp.numeric_text import normalize_authoritative_value
from analytics_toolkit.sql.backends.native_scalars import (
    ExactArray,
    ExactInterval,
    JsonDocument,
    NativeDecimalNaN,
    NativeFloatNaN,
)
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    declared_domain_fits,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import (
    dataframe_from_columns,
    decode_value,
    encode_value,
    is_null,
    typed_text,
)
from analytics_toolkit.sql.core.replay_spool import ReplaySpool


def test_native_float_nan_retains_ieee_payload_beside_sql_null_in_frame_and_replay():
    bits = bytes.fromhex("fff8000000000123")
    nan = NativeFloatNaN(struct.unpack(">d", bits)[0])
    frame = dataframe_from_columns([[nan, None, -0.0]], ["value"])
    assert frame.iloc[0, 0] is nan
    assert frame.iloc[1, 0] is None
    assert struct.pack(">d", frame.iloc[2, 0]) == bytes.fromhex("8000000000000000")
    assert is_null(float("nan"))
    assert is_null(pd.NA)
    assert not is_null(nan)
    with ReplaySpool(["value"]) as spool:
        spool.write_rows([(nan,), (None,), (-0.0,)])
        spool.certify()
        rows = list(spool.iter_rows())
    assert isinstance(rows[0][0], NativeFloatNaN)
    assert struct.pack(">d", rows[0][0]) == bits
    assert rows[1] == (None,)
    assert struct.pack(">d", rows[2][0]) == bytes.fromhex("8000000000000000")
    profile = ColumnProfile()
    profile.add(rows[0][0])
    profile.add(rows[1][0])
    assert profile.families == {"float"}
    assert profile.null_count == 1


def test_native_decimal_nan_retains_sign_payload_and_nonnull_profile_evidence():
    nan = NativeDecimalNaN(Decimal("-NaN123"))
    restored = decode_value(encode_value(nan))
    assert isinstance(restored, NativeDecimalNaN)
    assert restored.as_tuple() == nan.as_tuple()
    assert not is_null(restored)
    profile = ColumnProfile()
    profile.add(restored)
    profile.add(None)
    assert profile.null_count == 1
    assert profile.families == {"decimal", "nonfinite"}
    assert typed_text(restored) == "-NaN123"


@pytest.mark.parametrize("backend", ["ch", "trino"])
def test_gp_numeric_typmod_never_proves_a_foreign_finite_decimal_domain(backend):
    foreign = "Decimal(18,2)" if backend == "ch" else "DECIMAL(18,2)"
    assert not declared_domain_fits("NUMERIC(18,2)", foreign, backend, source_backend="gp")
    nested = "Array(Decimal(18,2))" if backend == "ch" else "ARRAY(DECIMAL(18,2))"
    assert not declared_domain_fits("NUMERIC(18,2)[]", nested, backend, source_backend="gp")
    assert declared_domain_fits("NUMERIC(18,2)", "NUMERIC(18,2)", "gp", source_backend="gp")


@pytest.mark.parametrize(
    "document", ["null", '"text"', "1.2345678901234567890123456789", '{"a":[null,1]}']
)
def test_native_json_document_tag_keeps_encoded_document_distinct_from_user_text(document):
    source = JsonDocument(document)
    frame = dataframe_from_columns([[source, None]], ["json"])
    assert frame.iloc[0, 0] is source
    assert frame.iloc[1, 0] is None
    with ReplaySpool(["json", "text", "sql_null"]) as spool:
        spool.write_rows([(source, document, None)])
        spool.certify()
        row = next(spool.iter_rows())
    assert type(row[0]) is JsonDocument
    assert row[0] == document
    assert type(row[1]) is str
    assert row[2] is None
    assert not is_null(row[0])


def test_native_calendar_interval_keeps_independent_components_and_merge_evidence():
    interval = ExactInterval(14, -2, 123456)
    restored = decode_value(encode_value(interval))
    assert restored == interval
    profile = ColumnProfile()
    profile.add(restored)
    other = ColumnProfile()
    other.add(ExactInterval(6, 0, -123456))
    profile.merge(other)
    assert profile.families == {"interval"}
    assert profile.interval_months_nonzero
    assert profile.interval_months_gcd == 2
    assert profile.interval_days_nonzero
    assert profile.interval_time_gcd == 123456000


def test_native_array_shape_bounds_and_flat_nulls_survive_exact_conversion():
    source = ExactArray((1, None, 3, 4), (2, 2), (-2, 5))
    restored = decode_value(encode_value(source))
    assert restored == source
    assert restored.nested_values() == [[1, None], [3, 4]]
    converted = ResolvedColumn("BIGINT[]", target_backend="gp").convert(restored)
    assert isinstance(converted, ExactArray)
    assert converted.dimensions == (2, 2)
    assert converted.lower_bounds == (-2, 5)
    assert converted == ExactArray((1, None, 3, 4), (2, 2), (-2, 5))
    profile = ColumnProfile(native_type="BIGINT[]", source_backend="gp")
    profile.add(restored)
    assert profile.native_array_nondefault_bounds
    assert profile.array_leaves.count == 4
    assert profile.array_leaves.null_count == 1
    with pytest.raises(ValueError, match="array bounds"):
        validate_profile(profile, "ARRAY(BIGINT)", "trino")


@pytest.mark.parametrize(
    ("native", "values", "expected"),
    [
        ("INTEGER[]", ("1", None, "-2"), (1, None, -2)),
        ("BOOLEAN[]", ("true", None, "false"), (True, None, False)),
        ("NUMERIC(5,2)[]", ("1.25", None, "-2.5"), (Decimal("1.25"), None, Decimal("-2.5"))),
    ],
)
def test_native_array_authoritative_scalar_views_retain_shape_bounds_and_nulls(
    native, values, expected
):
    source = ExactArray(values, (3,), (-2,))
    converted = normalize_authoritative_value(source, native)
    assert isinstance(converted, ExactArray)
    assert converted.dimensions == source.dimensions
    assert converted.lower_bounds == source.lower_bounds
    assert converted == ExactArray(expected, source.dimensions, source.lower_bounds)
    assert source == ExactArray(values, source.dimensions, source.lower_bounds)
