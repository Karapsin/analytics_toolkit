"""Redundant decimal zeros do not require lossy rounding or a text fallback."""

from __future__ import annotations

from decimal import Decimal, Inexact, Rounded, localcontext

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.decimal_values import (
    convert_decimal_value,
    decimal_target_type,
    minimal_fractional_scale,
    required_fractional_scale,
    resolve_decimal_type,
    validate_decimal_type,
)
from analytics_toolkit.sql.core.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.dml.load.type_validation import validate_dataframe_schema


@pytest.mark.parametrize(
    ("backend", "native"),
    [("gp", "NUMERIC(5,0)"), ("trino", "DECIMAL(5,0)"), ("ch", "Decimal(5,0)")],
)
def test_authoritative_zero_scale_accepts_redundant_fractional_zeros(backend, native):
    value = Decimal("123.0")
    profile = ColumnProfile()
    profile.add(value)

    validate_profile(profile, native, backend)
    converted = ResolvedColumn(native, target_backend=backend).convert(value)

    assert isinstance(converted, Decimal)
    assert converted == value
    assert converted.as_tuple().exponent == 0


@pytest.mark.parametrize(
    ("value", "raw_scale", "exact_scale"),
    [
        ("0.0001", 4, 4),
        ("1E+5", 0, 0),
        ("1.2300", 4, 2),
        ("1000.00", 2, 0),
        ("0E-1000", 1000, 0),
        ("-0.00000", 5, 0),
    ],
)
def test_profile_keeps_raw_scale_and_separate_nonzero_fractional_requirement(
    value, raw_scale, exact_scale
):
    profile = ColumnProfile()
    profile.add(Decimal(value))

    assert profile.fractional_digits == raw_scale
    assert profile.minimal_nonzero_fractional_scale == exact_scale
    assert minimal_fractional_scale(Decimal(value)) == exact_scale


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_inference_keeps_raw_preferred_scale_when_native_type_supports_it(backend):
    profile = ColumnProfile()
    profile.add(Decimal("1.2000"))
    expected = {"gp": "NUMERIC(5, 4)", "trino": "DECIMAL(5, 4)", "ch": "Decimal(5, 4)"}

    assert resolve_decimal_type(profile, backend) == expected[backend]


@pytest.mark.parametrize(
    ("backend", "raw_scale", "expected"),
    [("gp", 16384, "NUMERIC(1, 0)"), ("trino", 40, "DECIMAL(1, 0)"), ("ch", 80, "Decimal(1, 0)")],
)
def test_native_inference_tries_minimal_exact_scale_after_raw_domain_fails(
    backend, raw_scale, expected
):
    profile = ColumnProfile()
    profile.add(Decimal("1." + "0" * raw_scale))

    assert profile.fractional_digits == raw_scale
    assert resolve_decimal_type(profile, backend) == expected
    resolution = resolve_profile(profile, backend)
    assert resolution.target_type == expected
    assert not resolution.text_fallback
    assert resolution.convert(Decimal("1." + "0" * raw_scale)) == Decimal(1)


def test_merged_key_slices_keep_the_largest_nonzero_scale_before_native_choice():
    profile = ColumnProfile()
    profile.add(Decimal("1." + "0" * 80))
    later_slice = ColumnProfile()
    later_slice.add(Decimal("0.0034000"))
    assert profile.merge(later_slice) is profile

    assert profile.fractional_digits == 80
    assert profile.minimal_nonzero_fractional_scale == 4
    assert resolve_decimal_type(profile, "trino") == "DECIMAL(5, 4)"
    assert resolve_decimal_type(profile, "ch") == "Decimal(5, 4)"


@pytest.mark.parametrize(
    ("backend", "value"),
    [("gp", "1E-16384"), ("trino", "1E-39"), ("ch", "1E-77")],
)
def test_inference_never_clamps_a_nonzero_fraction_to_backend_limits(backend, value):
    profile = ColumnProfile()
    profile.add(Decimal(value))

    assert resolve_decimal_type(profile, backend) is None
    assert resolve_profile(profile, backend).text_fallback


@pytest.mark.parametrize(
    ("value", "target", "expected"),
    [
        ("123.0000", "NUMERIC(5,0)", "123"),
        ("1.2300", "NUMERIC(5,3)", "1.230"),
        ("-0.00000", "NUMERIC(1,0)", "-0"),
        ("12345678901234567890.1234500", "NUMERIC(30,5)", "12345678901234567890.12345"),
    ],
)
def test_alignment_discards_only_excess_zero_digits_without_decimal_context_rounding(
    value, target, expected
):
    original = Decimal(value)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        handled, converted = convert_decimal_value(original, target, "gp")

    assert handled
    assert converted.as_tuple() == Decimal(expected).as_tuple()
    assert converted == original


def test_scale_alignment_does_not_pad_or_rewrite_already_fitting_decimal():
    original = Decimal("1.20")
    handled, converted = convert_decimal_value(original, "DECIMAL(10,4)", "trino")

    assert handled
    assert converted is original
    assert convert_decimal_value(2**64 - 1, "Decimal(20,0)", "ch") == (
        True,
        Decimal(18446744073709551615),
    )


@pytest.mark.parametrize(
    ("backend", "target", "value"),
    [
        ("gp", "NUMERIC(5,0)", "123.01"),
        ("gp", "NUMERIC(4,2)", "999.99"),
        ("trino", "DECIMAL(5,0)", "1E+5"),
        ("ch", "Decimal(4,3)", "0.0001"),
        ("gp", "NUMERIC(1001,0)", "1"),
        ("trino", "DECIMAL(39,0)", "1"),
        ("ch", "Decimal(77,0)", "1"),
        ("ch", "Decimal(4,5)", "0"),
    ],
)
def test_decimal_conversion_rejects_nonzero_loss_and_unsupported_domains(backend, target, value):
    with pytest.raises(ValueError, match="precision or scale"):
        convert_decimal_value(Decimal(value), target, backend)


def test_unbounded_greenplum_numeric_aligns_only_redundant_excess_scale():
    original = Decimal("1." + "0" * 16384)
    handled, converted = convert_decimal_value(original, "NUMERIC", "gp")

    assert handled
    assert converted == original
    assert converted.as_tuple().exponent == -16383
    for value in (Decimal("1E-16384"), Decimal("1E+131072")):
        with pytest.raises(ValueError, match="precision or scale"):
            convert_decimal_value(value, "NUMERIC", "gp")


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_nonfinite_decimals_do_not_acquire_a_fictitious_exact_scale(value):
    with pytest.raises(ValueError, match="Non-finite"):
        minimal_fractional_scale(value)


def test_decimal_helper_leaves_other_value_and_destination_families_to_their_codecs():
    assert convert_decimal_value("1.20", "NUMERIC(5,2)", "gp") == (False, "1.20")
    assert convert_decimal_value(Decimal("1.20"), "VARCHAR", "trino") == (
        False,
        Decimal("1.20"),
    )
    with pytest.raises(ValueError, match="decimal type is unsupported"):
        convert_decimal_value(Decimal(1), "NUMERIC", "trino")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DECIMAL", "DECIMAL(38,0)"),
        ("decimal(5)", "DECIMAL(5,0)"),
        ("DECIMAL(5,2)", "DECIMAL(5,2)"),
    ],
)
def test_trino_native_decimal_defaults_have_their_actual_zero_scale(raw, expected):
    assert decimal_target_type(raw, "trino") == expected
    assert convert_decimal_value(Decimal("123.0"), raw, "trino")[1] == Decimal(123)


@pytest.mark.parametrize("backend", ["gp", "ch", "trino"])
def test_decimal_default_normalization_never_reinterprets_qualified_or_other_types(backend):
    for native in ("pg_catalog.numeric", '"numeric"', "VARCHAR"):
        assert decimal_target_type(native, backend) == native
    if backend != "ch":
        assert decimal_target_type("NUMERIC", backend) == "NUMERIC"
        assert decimal_target_type("Nullable(Decimal)", backend) == "Nullable(Decimal)"
    if backend == "gp":
        assert decimal_target_type("DECIMAL", backend) == "DECIMAL"


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("Decimal", "Decimal(10,0)"),
        ("decimal(5)", "Decimal(5,0)"),
        ("NUMERIC", "Decimal(10,0)"),
        ("Numeric(5)", "Decimal(5,0)"),
        ("DEC(5,2)", "Decimal(5,2)"),
        ("Fixed", "Decimal(10,0)"),
        ("Nullable(NUMERIC(5))", "nullable(Decimal(5,0))"),
        ("LowCardinality(Nullable(Fixed))", "lowcardinality(nullable(Decimal(10,0)))"),
    ],
)
def test_clickhouse_factory_decimal_aliases_preserve_their_actual_default_domain(native, expected):
    profile = ColumnProfile()
    profile.add(Decimal("123.0"))
    assert decimal_target_type(native, "ch") == expected
    validate_profile(profile, native, "ch")
    assert convert_decimal_value(Decimal("123.0"), native, "ch")[1] == Decimal(123)


@pytest.mark.parametrize(
    ("backend", "native"),
    [
        ("gp", "NUMERIC"),
        ("gp", "NUMERIC(5)"),
        ("gp", "DECIMAL(5,0)"),
        ("trino", "DECIMAL"),
        ("trino", "DECIMAL(5)"),
        ("ch", "Nullable(Decimal)"),
        ("ch", "Nullable(NUMERIC)"),
        ("ch", "Nullable(NUMERIC(5))"),
        ("ch", "Nullable(Decimal64(4))"),
    ],
)
def test_valid_native_decimal_declarations_do_not_need_a_nonnull_value(backend, native):
    profile = ColumnProfile()
    profile.add(None)

    validate_decimal_type(native, backend)
    validate_profile(profile, native, backend)


@pytest.mark.parametrize(
    ("backend", "native"),
    [
        ("gp", "NUMERIC(0,0)"),
        ("gp", "NUMERIC(1001,0)"),
        ("gp", "NUMBER"),
        ("trino", "NUMERIC"),
        ("trino", "DECIMAL(39)"),
        ("trino", "DECIMAL(5,6)"),
        ("ch", "Decimal()"),
        ("ch", "Decimal(5,bad_scale)"),
        ("ch", "NUMBER"),
        ("ch", "Decimal32(10)"),
        ("ch", "Decimal(77,0)"),
        ("ch", "Decimal(4,2,1)"),
    ],
)
def test_invalid_decimal_schema_rejects_even_when_all_input_is_sql_null(backend, native):
    profile = ColumnProfile()
    profile.add(None)

    with pytest.raises(ValueError, match="decimal type"):
        validate_decimal_type(native, backend)
    with pytest.raises(ValueError, match=r"decimal type|recognized Greenplum built-in"):
        validate_profile(profile, native, backend)


def test_native_decimal_single_precision_and_generic_compatibility_are_exact():
    assert convert_decimal_value(Decimal("123.0"), "NUMERIC(5)", "gp") == (True, Decimal(123))
    assert convert_decimal_value(Decimal("123.0"), "NUMERIC", "") == (
        True,
        Decimal("123.0"),
    )


def test_legacy_decimal_profile_requires_raw_scale_without_observed_zero_proof():
    profile = ColumnProfile(families={"decimal"}, integral_digits=3, fractional_digits=1)

    assert profile.minimal_nonzero_fractional_scale is None
    assert required_fractional_scale(profile) == 1
    with pytest.raises(ValueError, match="decimal precision or scale"):
        validate_profile(profile, "NUMERIC(5,0)", "gp")


@pytest.mark.parametrize(("backend", "raw_scale"), [("gp", 16384), ("trino", 40), ("ch", 80)])
def test_legacy_profile_cannot_infer_a_smaller_scale_without_actual_value_proof(backend, raw_scale):
    profile = ColumnProfile(families={"decimal"}, integral_digits=1, fractional_digits=raw_scale)

    assert required_fractional_scale(profile) == raw_scale
    assert resolve_decimal_type(profile, backend) is None
    assert resolve_profile(profile, backend).text_fallback


def test_observing_a_new_zero_fraction_does_not_override_unknown_earlier_decimal_facts():
    profile = ColumnProfile(families={"decimal"}, integral_digits=1, fractional_digits=40)
    profile.add(Decimal("2.00"))

    assert profile.fractional_digits == 40
    assert profile.minimal_nonzero_fractional_scale == 40
    assert required_fractional_scale(profile) == 40
    assert resolve_decimal_type(profile, "trino") is None


@pytest.mark.parametrize("reverse", [False, True])
def test_merging_legacy_unknown_scale_and_proven_zeros_keeps_unknown_raw_requirement(reverse):
    legacy = ColumnProfile(families={"decimal"}, integral_digits=1, fractional_digits=40)
    observed = ColumnProfile()
    observed.add(Decimal("1." + "0" * 80))
    left, right = (observed, legacy) if reverse else (legacy, observed)
    right_proof = right.minimal_nonzero_fractional_scale
    left.merge(right)

    assert left.fractional_digits == 80
    assert left.minimal_nonzero_fractional_scale == 40
    assert required_fractional_scale(left) == 40
    assert resolve_decimal_type(left, "trino") is None
    assert right.minimal_nonzero_fractional_scale == right_proof


@pytest.mark.parametrize("value", [None, 1, Decimal("123.0")])
def test_empty_integer_and_observed_decimal_profiles_have_safe_effective_scale(value):
    profile = ColumnProfile()
    assert profile.minimal_nonzero_fractional_scale is None
    assert required_fractional_scale(profile) == 0
    profile.add(value)

    assert required_fractional_scale(profile) == 0
    validate_profile(profile, "NUMERIC(5,0)", "gp")


@pytest.mark.parametrize(
    ("backend", "native", "value"),
    [
        ("trino", "ARRAY(DECIMAL(0,0))", None),
        ("trino", "ARRAY(DECIMAL(0,0))", []),
        ("trino", "MAP(VARCHAR,DECIMAL(0,0))", None),
        ("trino", "MAP(DECIMAL(0,0),VARCHAR)", None),
        ("trino", "ROW(ok BIGINT, amount ARRAY(DECIMAL(0,0)))", (1, None)),
        ("ch", "Nullable(Array(Decimal(0,0)))", None),
    ],
)
def test_dataframe_decimal_declarations_reject_invalid_nested_precision_without_values(
    backend, native, value
):
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match="decimal type"):
        validate_dataframe_schema(frame, {"value": native}, backend)

    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize(
    ("backend", "native", "value"),
    [
        ("trino", "ARRAY(DECIMAL(5,2))", None),
        ("trino", "ARRAY(DECIMAL(5,2))", []),
        ("trino", "MAP(VARCHAR,DECIMAL(5,2))", None),
        ("trino", "MAP(VARCHAR,DECIMAL(5,2))", {}),
        ("trino", "ROW(ok BIGINT, amount ARRAY(DECIMAL(5,2)))", (1, None)),
        ("gp", "NUMERIC(5,2)[][]", None),
        ("gp", "NUMERIC(5,2)[][]", []),
        ("ch", "Array(Nullable(Decimal(5,2)))", []),
        ("ch", "Nullable(Decimal(5,2))", None),
    ],
)
def test_valid_nested_decimal_declarations_preserve_null_and_empty_container_contracts(
    backend, native, value
):
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    original = frame.copy(deep=True)

    validate_dataframe_schema(frame, {"value": native}, backend)

    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize(
    ("backend", "native"),
    [
        ("gp", "NUMERIC(0,0)[][]"),
        ("ch", "Array(Nullable(Decimal(0,0)))"),
        ("ch", "Tuple(UInt64, Map(String, Decimal32(10)))"),
        ("trino", "ROW(ok BIGINT, amount DECIMAL(0,0))"),
        ("", "ARRAY(NUMERIC(0,0))"),
    ],
)
def test_declared_decimal_spec_walk_does_not_depend_on_observed_profile_children(backend, native):
    with pytest.raises(ValueError, match="decimal type"):
        validate_decimal_type(native, backend)


def test_declared_decimal_walk_leaves_unknown_nonnumeric_leaves_to_existing_policies():
    for native in ("opaque_type", "ARRAY(opaque_type)", "MAP(VARCHAR,opaque_type)"):
        validate_decimal_type(native, "trino")
    validate_decimal_type("ARRAY(NUMERIC(5,2))", "")


@pytest.mark.parametrize(
    ("raw", "native", "expected"),
    [
        ("123.00", "NUMERIC(3,0)", "123"),
        ("0.0001000", "NUMERIC(4,4)", "0.0001"),
        ("1E-1000", "NUMERIC(1000,1000)", "1E-1000"),
        ("1E+5", "NUMERIC(6,0)", "1E+5"),
    ],
)
def test_fractional_zero_alignment_preserves_integer_digits_and_implicit_fractional_places(
    raw, native, expected
):
    value = Decimal(raw)
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    original = frame.copy(deep=True)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        validate_dataframe_schema(frame, {"value": native}, "gp")
        converted = ResolvedColumn(native, target_backend="gp").convert(value)

    assert converted == value
    assert converted.as_tuple() == Decimal(expected).as_tuple()
    pd.testing.assert_frame_equal(frame, original)
