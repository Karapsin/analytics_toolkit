"""Native validation does not require an unrelated fallback representation."""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    declared_domain_fits,
    integer_domain,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.dml.load.type_validation import (
    _schema_value,
    convert_dataframe_schema,
    validate_dataframe_schema,
)


@pytest.mark.parametrize(
    ("source", "target", "fits"),
    [
        ("FLOAT(24)", "REAL", True),
        ("FLOAT(25)", "REAL", False),
        ("FLOAT(53)", "DOUBLE PRECISION", True),
        ("FLOAT", "REAL", False),
        ("FLOAT", "DOUBLE PRECISION", True),
    ],
)
def test_backend_unspecified_float_domains_keep_exact_width_proofs(source, target, fits):
    assert declared_domain_fits(source, target, "gp") is fits


@pytest.mark.parametrize("native", ["int8", "BIGINT", '"pg_catalog"."int8"'])
def test_greenplum_int8_alias_retains_full_signed_64_bit_domain(native):
    assert integer_domain(native, backend="gp") == (-(2**63), 2**63 - 1)


@pytest.mark.parametrize("backend", ["", "gp"])
def test_unbounded_numeric_integer_conversion_preserves_digits_without_float(backend):
    value = 2**256 + 1
    converted = ResolvedColumn("NUMERIC", target_backend=backend).convert(value)
    assert type(converted) is Decimal
    assert converted == Decimal(value)
    assert converted.as_tuple().exponent == 0


@pytest.mark.parametrize(
    ("value", "native", "expected"),
    [(1.25, "NUMERIC(5,2)", Decimal("1.25")), (123.0, "INTEGER", 123), (123.5, "INTEGER", 123.5)],
)
def test_legacy_schema_value_without_backend_keeps_explicit_conversion_policy(
    value, native, expected
):
    converted = _schema_value(value, native)
    assert converted == expected
    assert type(converted) is type(expected)


class _OpaqueClockZone(dt.tzinfo):
    def utcoffset(self, _value):
        return None

    def tzname(self, _value):
        return "opaque"


def test_native_clock_can_validate_when_fallback_cannot_preserve_its_zone():
    profile = ColumnProfile()
    profile.add(dt.time(12, 30, tzinfo=_OpaqueClockZone()))
    assert profile.families == {"time"}
    assert profile.text_encoding_valid is False
    validate_profile(profile, "TIME", "gp")
    with pytest.raises(ValueError, match="encoded exactly"):
        validate_profile(profile, "TEXT", "gp")


def test_unknown_native_values_are_not_silently_stringified_or_whitelisted():
    profile = ColumnProfile()
    profile.add(object())
    assert profile.families == {"unsupported"}
    assert profile.text_encoding_valid is False
    with pytest.raises(ValueError, match="integer"):
        validate_profile(profile, "BIGINT", "gp")
    with pytest.raises(ValueError, match="no supported exact native"):
        resolve_profile(profile, "gp")


@pytest.mark.parametrize("value", [2**53 + 1, Decimal("0.0001"), "é😀"])
def test_supported_text_measurements_retain_exact_bounds(value):
    profile = ColumnProfile()
    profile.add(value)
    expected = len(format(value, "f")) if isinstance(value, Decimal) else len(str(value))
    assert profile.text_encoding_valid is True
    assert profile.maximum_text_length == expected
    with pytest.raises(ValueError, match="authoritative text length"):
        validate_profile(profile, f"VARCHAR({expected - 1})", "gp")


def test_text_codec_availability_is_merged_over_every_slice():
    first, last = ColumnProfile(), ColumnProfile()
    first.add("fits")
    last.add(object())
    first.merge(last)
    assert first.maximum_text_length == 4
    assert first.text_encoding_valid is False
    with pytest.raises(ValueError, match="encoded exactly"):
        validate_profile(first, "VARCHAR(20)", "gp")


def test_interval_alignment_facts_merge_without_nanosecond_loss():
    first, last = ColumnProfile(), ColumnProfile()
    first.add(dt.timedelta(days=2, seconds=1))
    last.add(pd.Timedelta(3, unit="ns"))
    first.merge(last)
    assert first.families == {"interval"}
    assert first.interval_days_nonzero
    assert first.interval_time_gcd == 1
    assert first.temporal_scale == 9
    with pytest.raises(ValueError, match="precision"):
        validate_profile(first, "INTERVAL(6)", "gp", allow_timestamp_precision_loss=False)


def test_clock_awareness_facts_merge_for_native_inference():
    first, last = ColumnProfile(), ColumnProfile()
    first.add(dt.time(1))
    last.add(dt.time(2, tzinfo=dt.timezone.utc))
    first.merge(last)
    assert first.aware
    assert first.naive
    assert resolve_profile(first, "gp").target_type == "TEXT"


@pytest.mark.parametrize("invalid_first", [False, True])
def test_trino_clock_constraints_remain_invalid_after_slice_merge(invalid_first):
    valid, invalid = ColumnProfile(), ColumnProfile()
    valid.add(dt.time(12, tzinfo=dt.timezone.utc))
    invalid.add(dt.time(13, tzinfo=dt.timezone(dt.timedelta(hours=15))))
    assert valid.trino_time_constraints_valid is True
    assert invalid.trino_time_constraints_valid is False
    merged, incoming = (invalid, valid) if invalid_first else (valid, invalid)
    merged.merge(incoming)
    assert merged.trino_time_constraints_valid is False
    with pytest.raises(ValueError, match="authoritative Trino TIME"):
        validate_profile(merged, "TIME(6) WITH TIME ZONE", "trino")


@pytest.mark.parametrize(
    "offset", [dt.timedelta(hours=18), dt.timedelta(seconds=1, microseconds=1)]
)
def test_clock_offsets_outside_gp_native_domain_use_exact_typed_text(offset):
    import json  # noqa: PLC0415 -- inspect the exact fallback artifact.

    from analytics_toolkit.sql.core.exact_values import decode_value  # noqa: PLC0415

    value = dt.time(12, 30, tzinfo=dt.timezone(offset), fold=1)
    profile = ColumnProfile()
    profile.add(value)
    resolved = resolve_profile(profile, "gp")
    assert resolved.target_type == "TEXT"
    restored = decode_value(json.loads(resolved.convert(value))["value"])
    assert restored == value
    assert restored.utcoffset() == offset
    assert restored.fold == 1


@pytest.mark.parametrize(
    ("backend", "native"),
    [("trino", "DECIMAL"), ("ch", "Decimal"), ("ch", "NUMERIC"), ("ch", "FIXED")],
)
def test_backend_default_decimal_shape_precedes_explicit_float_conversion(backend, native):
    frame = pd.DataFrame({"value": pd.Series([123.0], dtype=object)})
    validate_dataframe_schema(frame, {"value": native}, backend)
    converted, _metadata = convert_dataframe_schema(frame, {"value": native}, backend)
    assert converted.iloc[0, 0] == Decimal(123)
    assert converted.iloc[0, 0].as_tuple().exponent == 0
    assert type(frame.iloc[0, 0]) is float
