"""Finite numeric text uses declared ClickHouse types before raw family checks."""

from __future__ import annotations

import datetime as dt
import importlib
from copy import deepcopy
from decimal import Decimal, localcontext

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.storage_types import logical_storage_type
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    decimal_domain,
    declared_domain_fits,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs


@pytest.fixture
def numeric_policy():
    return importlib.import_module("analytics_toolkit.sql.backends.ch.numeric_text")


def _scaled_integer(value, scale):
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    power = int(exponent) + scale
    assert power >= 0
    return (-coefficient if sign else coefficient) * 10**power


@pytest.mark.parametrize(
    ("native", "text", "expected"),
    [
        ("Decimal(38, 9)", "12345678901234567890.123456789", "12345678901234567890.123456789"),
        (
            "Nullable(Decimal(38, 9))",
            "-12345678901234567890.123456789",
            "-12345678901234567890.123456789",
        ),
        ("Decimal(4, 4)", "0.0001", "0.0001"),
        ("Decimal(6, 0)", "1E+5", "100000"),
        ("Decimal(10, 2)", "1.2300", "1.23"),
        ("Decimal32(2)", "-1234567.89", "-1234567.89"),
        ("Decimal64(4)", "  +0012.3400\t", "12.3400"),
        ("LowCardinality(Nullable(Decimal128(4)))", "1_234.5000", "1234.5000"),
        ("SimpleAggregateFunction(sum, Decimal(10, 2))", "12.34", "12.34"),
        ("Decimal", "12", "12"),
        ("Decimal(9)", "12", "12"),
        ("Numeric(10,2)", "12.34", "12.34"),
    ],
)
def test_ch_numeric_text_decimal_preserves_exact_value_and_native_domain(
    numeric_policy, native, text, expected
):
    with localcontext() as context:
        context.prec = 2
        handled, result = numeric_policy.convert_numeric_text(text, native)

    assert handled is True
    assert type(result) is Decimal
    assert result == Decimal(expected)
    canonical = get_backend_adapter("ch").canonical_native_type(native)
    domain = decimal_domain(logical_storage_type(canonical))
    assert domain is not None
    precision, scale = domain
    scaled = _scaled_integer(result, scale)
    assert scaled == _scaled_integer(Decimal(expected), scale)
    assert abs(scaled) < 10**precision
    assert numeric_policy.normalize_authoritative_numeric_text(text, native) == Decimal(expected)


@pytest.mark.parametrize(
    ("native", "text", "expected"),
    [
        ("Float32", "1.25", 1.25),
        ("Nullable(Float32)", "  -1.25\t", -1.25),
        ("Float64", "+1.25e2", 125.0),
        ("Nullable(Float64)", "-0.0", -0.0),
        ("LowCardinality(Nullable(Float64))", "1_234.5", 1234.5),
        ("SimpleAggregateFunction(sum, Float64)", "0.125", 0.125),
    ],
)
def test_ch_numeric_text_float_uses_requested_float_conversion(
    numeric_policy, native, text, expected
):
    handled, result = numeric_policy.convert_numeric_text(text, native)

    assert handled is True
    assert type(result) is float
    assert result.hex() == expected.hex()
    assert numeric_policy.normalize_authoritative_numeric_text(text, native).hex() == expected.hex()


@pytest.mark.parametrize("native", ["Decimal(20, 4)", "Float32", "Nullable(Float64)"])
@pytest.mark.parametrize(
    "value",
    ["private-numeric-token-691", "", "NaN", "sNaN", "+Infinity", "-inf", "1e10000", "NULL"],
)
def test_ch_numeric_text_invalid_and_nonfinite_strings_reject_before_null_normalization(
    numeric_policy, native, value
):
    with pytest.raises(numeric_policy.ClickHouseNumericTextValueError, match="finite") as error:
        numeric_policy.convert_numeric_text(value, native)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert repr(value) not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("Decimal(5, 2)", "1000.00"),
        ("Decimal(5, 2)", "1.234"),
        ("Decimal(4, 4)", "0.00001"),
        ("Decimal(6, 0)", "1E+6"),
        ("Decimal32(2)", "10000000"),
        ("SimpleAggregateFunction(sum, Decimal(5,2))", "1000.00"),
        ("LowCardinality(Nullable(Numeric(5,2)))", "1.234"),
        ("Float32", "1e39"),
        ("Float64", "1e309"),
    ],
)
def test_ch_numeric_text_reuses_existing_precision_scale_and_float_range_checks(
    numeric_policy, native, value
):
    with pytest.raises(numeric_policy.ClickHouseNumericTextValueError, match="precision"):
        numeric_policy.convert_numeric_text(value, native)


@pytest.mark.parametrize(
    "value",
    [
        None,
        pd.NA,
        42,
        True,
        np.uint64(2**64 - 1),
        1.25,
        float("inf"),
        float("nan"),
        Decimal("1.2300"),
        Decimal("NaN"),
        b"12.34",
        bytearray(b"12.34"),
    ],
)
@pytest.mark.parametrize("native", ["Decimal(38, 9)", "Nullable(Float64)"])
def test_ch_numeric_text_nonstring_scalar_inputs_keep_the_existing_path(
    numeric_policy, native, value
):
    handled, result = numeric_policy.convert_numeric_text(value, native)

    assert handled is False
    assert result is value
    assert numeric_policy.normalize_authoritative_numeric_text(value, native) is value


def test_ch_numeric_text_nonstring_objects_do_not_trigger_conversion_callbacks(numeric_policy):
    calls = []

    class Opaque:
        def __str__(self):
            calls.append("str")
            return "12.34"

        def __float__(self):
            calls.append("float")
            return 12.34

        def __bool__(self):
            calls.append("bool")
            return True

    value = Opaque()
    assert numeric_policy.convert_numeric_text(value, "Float64") == (False, value)
    assert numeric_policy.normalize_authoritative_numeric_text(value, "Decimal(10,2)") is value
    assert calls == []


@pytest.mark.parametrize("native", [None, "String", "UInt64", "Bool", "UUID", "DateTime64(9)"])
def test_ch_numeric_text_type_selection_does_not_claim_other_families(numeric_policy, native):
    value = "12.34"

    assert numeric_policy.is_numeric_text_type(native) is False
    assert numeric_policy.convert_numeric_text(value, native) == (False, value)
    assert numeric_policy.normalize_authoritative_numeric_text(value, native) is value


@pytest.mark.parametrize("native", ["Decimal(10,2)", "Float64"])
def test_ch_numeric_text_preserves_raw_family_validation_and_string_inference(native):
    profile = ColumnProfile()
    profile.add("12.34")

    with pytest.raises(ValueError, match="authoritative"):
        validate_profile(profile, native, "ch")

    assert declared_domain_fits("String", native, "ch", source_backend="ch") is False
    values = pd.Series(["12.34", "0.01", None], dtype=object)
    original = values.copy(deep=True)
    assert get_backend_adapter("ch").infer_dataframe_column_type(values) == "Nullable(String)"
    pd.testing.assert_series_equal(values, original)


def test_ch_numeric_text_nested_views_use_existing_codecs_without_mutating_callers(numeric_policy):
    values = [("12345678901234567890.123456789", "1.25", 2**63 + 1), (None, None, 2**63 + 2)]
    before = deepcopy(values)
    native = "Array(Tuple(amount Nullable(Decimal(38,9)), ratio Nullable(Float64), id UInt64))"

    result = numeric_policy.normalize_authoritative_numeric_text(values, native)

    assert result == [
        (Decimal("12345678901234567890.123456789"), 1.25, 2**63 + 1),
        (None, None, 2**63 + 2),
    ]
    assert type(result[0][0]) is Decimal
    assert type(result[0][1]) is float
    assert type(result[0][2]) is int
    assert values == before


def test_ch_numeric_text_nested_map_views_retain_ordered_duplicate_pairs(numeric_policy):
    values = OrderedMapPairs((("key", "1.00"), ("key", "2.50")))

    result = numeric_policy.normalize_authoritative_numeric_text(
        values, "Map(String, Decimal(10,2))"
    )

    assert isinstance(result, OrderedMapPairs)
    assert list(result.items()) == [("key", Decimal("1.00")), ("key", Decimal("2.50"))]
    assert list(values.items()) == [("key", "1.00"), ("key", "2.50")]


def test_ch_numeric_text_nested_type_detection_and_nulls_are_shape_preserving(numeric_policy):
    assert numeric_policy.contains_numeric_text_type(
        "Array(Tuple(id UInt64, amount Decimal(10,2)))"
    )
    assert numeric_policy.contains_numeric_text_type("Array(Nullable(Float64))")
    assert numeric_policy.contains_numeric_text_type("Map(String, UInt64)") is False
    assert numeric_policy.normalize_authoritative_numeric_text(None, "Array(Float64)") is None
    opaque = [1, 2, None]
    assert numeric_policy.normalize_authoritative_numeric_text(opaque, "Array(UInt64)") is opaque


def test_ch_main_scalar_view_keeps_naive_timestamp_without_numeric_text(numeric_policy):
    when = dt.datetime.fromisoformat("2024-02-29T12:34:56.123456")
    values = [("42", 1, when)]
    before = deepcopy(values)

    result = numeric_policy.normalize_authoritative_value(
        values, "Array(Tuple(id Int64, flag Bool, seen DateTime64(9)))"
    )

    assert result == [(42, True, when)]
    assert result[0][2] is when
    assert values == before


def test_ch_main_scalar_view_preserves_native_container_identity(numeric_policy):
    when = dt.datetime.fromisoformat("2024-02-29T12:34:56.123456")
    values = [(42, False, when)]

    assert (
        numeric_policy.normalize_authoritative_value(
            values, "Array(Tuple(id Int64, flag Bool, seen DateTime64(9)))"
        )
        is values
    )


@pytest.mark.parametrize(
    ("native", "text", "expected"),
    [
        ("Decimal(10,2)", "12.34", Decimal("12.34")),
        ("Decimal(6,0)", "+1e5", Decimal("1E+5")),
        ("Nullable(Decimal64(4))", "-1.25E-2", Decimal("-0.0125")),
        ("Decimal(10,2)", "0e9", Decimal("0E+9")),
        ("Float64", "+1.25e2", 125.0),
        ("Nullable(Float64)", "-0.0", -0.0),
        ("Float32", "1.25", 1.25),
    ],
)
def test_ch_native_numeric_text_cast_accepts_verified_canonical_ascii_forms(
    numeric_policy, native, text, expected
):
    result = numeric_policy.native_text_cast_value(text, native)

    assert type(result) is type(expected)
    assert result == expected
    if isinstance(expected, float):
        assert result.hex() == expected.hex()
    assert numeric_policy.native_text_cast_value(None, native) is None


@pytest.mark.parametrize("native", ["Decimal(10,2)", "Float64"])
@pytest.mark.parametrize("value", ["1_2.34", "\u00a012.34", "\uff11\uff12.\uff13\uff14"])
def test_ch_native_numeric_text_cast_rejects_python_only_direct_input_forms(
    numeric_policy, native, value
):
    handled, direct = numeric_policy.convert_numeric_text(value, native)

    assert handled is True
    assert direct == (Decimal("12.34") if native.startswith("Decimal") else 12.34)
    with pytest.raises(numeric_policy.ClickHouseNumericTextValueError, match="canonical"):
        numeric_policy.native_text_cast_value(value, native)


@pytest.mark.parametrize("native", ["Decimal(10,2)", "Float64"])
@pytest.mark.parametrize(
    "value", [" 12.34", "12.34\n", "12.34private", "1e0001", "NaN", ".5", "1.", 12.34]
)
def test_ch_native_numeric_text_cast_rejects_unverified_or_noncanonical_stage_syntax(
    numeric_policy, native, value
):
    with pytest.raises(numeric_policy.ClickHouseNumericTextValueError, match="canonical") as error:
        numeric_policy.native_text_cast_value(value, native)

    assert repr(value) not in str(error.value)
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert error.value.__cause__ is None
    assert "from_sql" in str(error.value)


@pytest.mark.parametrize(("native", "value"), [("Float64", "1e-400"), ("Float32", "1e-46")])
def test_ch_native_numeric_text_cast_rejects_nonzero_underflow_before_sql_mutation(
    numeric_policy, native, value
):
    assert numeric_policy.convert_numeric_text(value, native)[0] is True
    with pytest.raises(numeric_policy.ClickHouseNumericTextValueError, match="canonical"):
        numeric_policy.native_text_cast_value(value, native)


@pytest.mark.parametrize(
    ("native", "value"),
    [("Decimal(10,2)", "0e17"), ("Float64", "0e999"), ("Float32", "1e39")],
)
def test_ch_native_numeric_text_cast_enforces_native_exponent_and_range_guards(
    numeric_policy, native, value
):
    with pytest.raises(numeric_policy.ClickHouseNumericTextValueError, match="canonical"):
        numeric_policy.native_text_cast_value(value, native)


@pytest.mark.parametrize(
    "native", ["Decimal(10,2)", "Float64", "Array(Nullable(Decimal32(2)))", "Object('json')"]
)
def test_ch_numeric_text_type_only_hook_keeps_raw_family_validation_authoritative(
    numeric_policy, native
):
    assert numeric_policy.validate_numeric_text_type_profile(native) is False


@pytest.mark.parametrize("native", ["Decimal(0,0)", "Decimal(5,6)", "Array(Decimal32(10))"])
def test_ch_numeric_text_type_only_hook_rejects_bad_decimal_declarations(numeric_policy, native):
    with pytest.raises(ValueError, match=r"precision|scale"):
        numeric_policy.validate_numeric_text_type_profile(native)


def test_ch_authoritative_numeric_view_without_schema_preserves_source_identity_and_inference(
    numeric_policy,
):
    amounts = ["12345678901234567890.123456789", "NaN", None]
    payload = bytearray(b"00042")
    source = {"id": "00042", "amounts": amounts, "payload": payload, "native": Decimal("1.2300")}
    before = deepcopy(source)

    result = numeric_policy.normalize_authoritative_value(source, None)

    assert result is source
    assert result["amounts"] is amounts
    assert result["payload"] is payload
    assert numeric_policy.normalize_authoritative_value(source["id"], None) is source["id"]
    assert source == before
    values = pd.Series(amounts, dtype=object)
    original = values.copy(deep=True)
    assert get_backend_adapter("ch").infer_dataframe_column_type(values) == "Nullable(String)"
    pd.testing.assert_series_equal(values, original)
