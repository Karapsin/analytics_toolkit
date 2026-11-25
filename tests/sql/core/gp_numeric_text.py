"""Finite GP numeric text preserves native values without relaxing raw profiles."""

from __future__ import annotations

import importlib
import math
import struct
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.native_scalars import NativeDecimalNaN, NativeFloatNaN
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import is_null


@pytest.fixture
def numeric_policy():
    return importlib.import_module("analytics_toolkit.sql.backends.gp.numeric_text")


@pytest.fixture
def finite_float():
    return importlib.import_module("analytics_toolkit.sql.backends.finite_numeric_text")


@pytest.mark.parametrize(
    "native",
    ["NUMERIC", "NUMERIC(38,9)", "DECIMAL(38,9)", "pg_catalog.numeric(38,9)"],
)
def test_gp_numeric_text_preserves_full_decimal_coefficient_without_float(numeric_policy, native):
    raw = "12345678901234567890123456789.123456789"
    expected = Decimal(raw)

    with localcontext() as context:
        context.prec = 2
        result = numeric_policy.numeric_text_value(raw, native)

    assert type(result) is Decimal
    assert result.as_tuple() == expected.as_tuple()
    assert numeric_policy.is_numeric_text_type(native) is True
    assert numeric_policy.validate_numeric_text_type_profile(native) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" \t\r\n\v\f+001.250 \t\r\n\v\f", Decimal("1.250")),
        ("-.5", Decimal("-.5")),
        ("5.", Decimal(5)),
        ("+5e-1", Decimal(".5")),
        ("1E+5", Decimal("1E+5")),
        ("-0.000", Decimal("-0.000")),
        ("1e500", Decimal("1e500")),
    ],
)
def test_gp_numeric_text_native_decimal_syntax_stays_exact(numeric_policy, raw, expected):
    result = numeric_policy.native_text_cast_value(raw, "NUMERIC")

    assert type(result) is Decimal
    assert result.as_tuple() == expected.as_tuple()
    handled, converted = numeric_policy.convert_numeric_text(raw, "NUMERIC")
    assert handled is True
    assert converted.as_tuple() == expected.as_tuple()


@pytest.mark.parametrize(
    "native", ["REAL", "float4", "FLOAT(24)", "DOUBLE PRECISION", "float8", "FLOAT(53)"]
)
def test_gp_numeric_text_floating_aliases_have_finite_native_values(numeric_policy, native):
    handled, result = numeric_policy.convert_numeric_text(" \t-1.25\r\n", native)

    assert handled is True
    assert type(result) is float
    assert result == -1.25
    assert numeric_policy.validate_numeric_text_type_profile(native) is False


@pytest.mark.parametrize("native", ["REAL", "DOUBLE PRECISION", "NUMERIC(38,9)"])
@pytest.mark.parametrize(
    "raw",
    [
        "",
        " \t\r\n\v\f",
        "sNaN",
        "1_000",
        "1 2",
        "0x1p2",
        "\u0661.25",
        "\u00a01.25",
        "1.25\u2003",
        "1.25\x00",
        "private-invalid-numeric",
    ],
)
def test_gp_numeric_text_invalid_strings_reject_privately(numeric_policy, native, raw):
    with pytest.raises(numeric_policy.GreenplumNumericTextValueError) as error:
        numeric_policy.numeric_text_value(raw, native)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert repr(raw) not in str(error.value)
    assert error.value.__cause__ is None
    assert "CAST" in str(error.value)
    assert "from_sql" in str(error.value)


@pytest.mark.parametrize("native", ["REAL", "DOUBLE PRECISION", "NUMERIC(38,9)"])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-inf"])
def test_gp_numeric_special_text_keeps_nonnull_values_and_defers_numeric_infinity(
    numeric_policy, native, raw
):
    handled, result = numeric_policy.convert_numeric_text(raw, native)
    assert handled
    assert not is_null(result)
    if native.startswith("NUMERIC") and raw != "NaN":
        assert result is raw
        profile = ColumnProfile()
        profile.add(raw)
        assert numeric_policy.pending_numeric_assignment(profile, native)
    elif raw == "NaN":
        expected = NativeDecimalNaN if native.startswith("NUMERIC") else NativeFloatNaN
        assert isinstance(result, expected)
    else:
        assert type(result) is float
        assert math.isinf(result)
        assert math.copysign(1.0, result) == (-1.0 if raw.startswith("-") else 1.0)


@pytest.mark.parametrize(
    ("native", "raw"),
    [
        ("NUMERIC(5,2)", "1.234"),
        ("NUMERIC(3,0)", "1000"),
        ("NUMERIC", "1e131072"),
        ("NUMERIC", "1e-16384"),
        ("REAL", "1e39"),
        ("DOUBLE PRECISION", "1e309"),
        ("REAL", "1e-999999999"),
        ("DOUBLE PRECISION", "1e-999999999"),
        ("REAL", "1e999999999"),
        ("DOUBLE PRECISION", "1e999999999"),
    ],
)
def test_gp_numeric_text_certifies_existing_exact_decimal_and_float_bounds(
    numeric_policy, native, raw
):
    with pytest.raises(numeric_policy.GreenplumNumericTextValueError):
        numeric_policy.numeric_text_value(raw, native)


@pytest.mark.parametrize("native", ["REAL", "DOUBLE PRECISION", "NUMERIC(38,9)"])
@pytest.mark.parametrize("value", [None, pd.NA, pd.NaT, 1, 1.25, Decimal("1.25"), b"1.25"])
def test_gp_numeric_text_does_not_change_nontext_scalar_paths(numeric_policy, native, value):
    handled, result = numeric_policy.convert_numeric_text(value, native)

    assert handled is False
    assert result is value
    assert numeric_policy.normalize_authoritative_numeric_text(value, native) is value


@pytest.mark.parametrize(
    "native", [None, "TEXT", "INTEGER", "BOOLEAN", "public.numeric", '"Numeric"']
)
def test_gp_numeric_text_leaves_other_types_and_domains_untouched(numeric_policy, native):
    raw = "001.25"

    assert numeric_policy.is_numeric_text_type(native) is False
    if native is not None:
        assert numeric_policy.contains_numeric_text_type(native) is False
    assert numeric_policy.normalize_authoritative_numeric_text(raw, native) is raw


@pytest.mark.parametrize("native", ["NUMERIC(8,3)[]", "NUMERIC(8,3)[][]", "pg_catalog._float4"])
def test_gp_numeric_text_builtin_arrays_convert_text_without_mutating_cells(numeric_policy, native):
    raw = [["+001.25", None], ["-.5", "0"]] if native.endswith("[][]") else ["+001.25", None]
    original = deepcopy(raw)

    result = numeric_policy.normalize_authoritative_numeric_text(raw, native)

    expected = (
        [[Decimal("1.25"), None], [Decimal("-.5"), Decimal(0)]]
        if native.endswith("[][]")
        else ([1.25, None] if native == "pg_catalog._float4" else [Decimal("1.25"), None])
    )
    assert result == expected
    assert result is not raw
    assert raw == original
    assert numeric_policy.is_numeric_text_type(native) is False
    assert numeric_policy.contains_numeric_text_type(native) is True


def test_gp_numeric_text_keeps_already_native_nested_inputs_and_opaque_arrays(numeric_policy):
    values = np.array([Decimal("1.25"), None], dtype=object)

    assert numeric_policy.normalize_authoritative_numeric_text(values, "NUMERIC[]") is values
    assert numeric_policy.normalize_authoritative_numeric_text(np.array(1), "NUMERIC[]").shape == ()


@pytest.mark.parametrize("native", ["REAL", "DOUBLE PRECISION", "NUMERIC(38,9)"])
def test_gp_numeric_text_raw_family_and_inference_are_not_relaxed(numeric_policy, native):
    profile = ColumnProfile()
    profile.add("1.25")
    with pytest.raises(
        ValueError,
        match=r"authoritative (?:floating-point domain|decimal precision or scale)",
    ):
        validate_profile(profile, native, "gp")
    assert profile.families == {"string"}
    assert declared_domain_fits("TEXT", native, "gp", source_backend="gp") is False
    assert get_backend_adapter("gp").infer_dataframe_column_type(pd.Series(["001.25"])) == "TEXT"
    assert numeric_policy.validate_numeric_text_type_profile(native) is False


@pytest.mark.parametrize("bits", [32, 64])
@pytest.mark.parametrize("raw", ["0", "-0", "0e999999999", "-0e-999999999"])
def test_finite_float_zero_sign_survives_extreme_exponents(finite_float, bits, raw):
    value = Decimal(raw)
    result = finite_float.decimal_float_value(value, bits, reject_underflow=True)

    assert result == 0
    assert math.copysign(1, result) == (-1 if value.is_signed() else 1)


def test_finite_float32_corrects_double_rounding_at_exact_native_midpoint(finite_float):
    midpoint = Decimal("1.000000059604644775390625")
    above = Decimal("1.0000000596046447753906250000000000000000000000000000000000000001")
    below = Decimal("1.0000000596046447753906249999999999999999999999999999999999999999")
    successor = float.fromhex("0x1.000002p0")

    with localcontext() as context:
        context.prec = 2
        assert finite_float.decimal_float_value(midpoint, 32) == 1
        assert finite_float.decimal_float_value(above, 32) == successor
        assert finite_float.decimal_float_value(below, 32) == 1
        assert finite_float.decimal_float_value(midpoint.copy_negate(), 32) == -1
        assert finite_float.decimal_float_value(above.copy_negate(), 32) == -successor


def test_finite_float32_uses_native_overflow_midpoint_and_subnormal_ties(finite_float):
    threshold = (1 << 128) - (1 << 103)
    below = Decimal(threshold - 1)
    maximum = float.fromhex("0x1.fffffep127")
    half_minimum = Decimal.from_float(float.fromhex("0x1p-150"))

    assert finite_float.decimal_float_value(below, 32) == maximum
    with pytest.raises(ValueError, match="floating-point domain"):
        finite_float.decimal_float_value(Decimal(threshold), 32)
    assert finite_float.decimal_float_value(half_minimum, 32) == 0
    with pytest.raises(ValueError, match="floating-point domain"):
        finite_float.decimal_float_value(half_minimum, 32, reject_underflow=True)


@pytest.mark.parametrize("bits", [32, 64])
@pytest.mark.parametrize("raw", ["1e-999999999", "-1e-999999999"])
def test_finite_float_extreme_underflow_obeys_native_backend_policy(finite_float, bits, raw):
    value = Decimal(raw)
    result = finite_float.decimal_float_value(value, bits)

    assert result == 0
    assert math.copysign(1, result) == (-1 if value.is_signed() else 1)
    with pytest.raises(ValueError, match="floating-point domain"):
        finite_float.decimal_float_value(value, bits, reject_underflow=True)


@pytest.mark.parametrize("bits", [32, 64, 128])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "1e999999999"])
def test_finite_float_invalid_domains_fail_without_unbounded_scaling(finite_float, bits, raw):
    with pytest.raises(ValueError, match="floating-point domain"):
        finite_float.decimal_float_value(Decimal(raw), bits)


@pytest.mark.parametrize(
    ("raw", "expected_bits", "double_rounds_up"),
    [
        ("1.0000001788139343261718749999999999999999999999999999999999999999", 0x3F800001, True),
        ("1.000000178813934326171875", 0x3F800002, False),
        ("1.0000001788139343261718750000000000000000000000000000000000000001", 0x3F800002, False),
    ],
)
@pytest.mark.parametrize("negative", [False, True])
def test_finite_float32_does_not_double_round_below_an_upper_even_neighbor(
    finite_float, raw, expected_bits, double_rounds_up, negative
):
    value = Decimal(raw)
    if negative:
        value = value.copy_negate()
        expected_bits |= 0x80000000

    with localcontext() as context:
        context.prec = 2
        result = finite_float.decimal_float_value(value, 32)

    assert struct.pack(">f", result) == expected_bits.to_bytes(4, "big")
    if double_rounds_up:
        # The binary64 approximation lands exactly on the midpoint. Direct
        # binary32 packing chooses the upper even neighbor, which is wrong for
        # this original decimal value just below that midpoint.
        assert struct.pack(">f", float(value)) != struct.pack(">f", result)


@pytest.mark.parametrize("raw", ["1.8e308", "-1.8e308"])
def test_finite_float64_rejects_actual_round_to_infinity_at_the_boundary(finite_float, raw):
    value = Decimal(raw)
    assert value.is_finite()
    assert value.adjusted() == 308
    assert math.isinf(float(value))

    with pytest.raises(
        ValueError, match="Numeric text exceeds the finite floating-point domain"
    ) as error:
        finite_float.decimal_float_value(value, 64)

    assert str(value) not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("trap", [False, True])
def test_gp_numeric_text_rejects_constructor_failure_under_either_decimal_trap_policy(
    numeric_policy, trap
):
    raw = "1e999999999999999999999999999999"

    with localcontext() as context:
        context.traps[InvalidOperation] = trap
        context.clear_flags()
        with pytest.raises(numeric_policy.GreenplumNumericTextValueError) as error:
            numeric_policy.numeric_text_value(raw, "NUMERIC")
        assert context.flags[InvalidOperation] is True

    assert raw not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert "from_sql" in str(error.value)


def test_gp_numeric_authoritative_view_without_a_schema_keeps_original_identity(numeric_policy):
    values = ["001.25", Decimal("1.25"), [None, -0.0]]
    original = deepcopy(values)

    result = numeric_policy.normalize_authoritative_value(values, None)

    assert result is values
    assert values == original
    assert type(result[0]) is str
    assert math.copysign(1, result[2][1]) == -1
