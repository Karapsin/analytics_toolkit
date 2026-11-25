"""ClickHouse authoritative integer casts preserve exact values and native widths."""

from __future__ import annotations

import importlib
from copy import deepcopy
from decimal import Decimal, localcontext

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs


@pytest.fixture
def integer_policy():
    return importlib.import_module("analytics_toolkit.sql.backends.ch.integer_values")


@pytest.mark.parametrize("missing", [None, pd.NA, pd.NaT, float("nan")])
def test_ch_nested_integer_missing_column_is_retained_for_nullability_validation(
    integer_policy, missing
):
    native = "Array(Nullable(Int64))"

    assert integer_policy.normalize_authoritative_integer(missing, native) is None

    current = ColumnProfile()
    current.add(None)
    with pytest.raises(ValueError, match=r"NULL.*nonnullable"):
        validate_profile(current, native, "ch")


@pytest.mark.parametrize("bits", [8, 16, 32, 64, 128, 256])
@pytest.mark.parametrize("unsigned", [False, True])
def test_ch_integer_python_cast_preserves_exact_signed_and_unsigned_native_bounds(
    integer_policy, bits, unsigned
):
    native = f"{'U' if unsigned else ''}Int{bits}"
    minimum, maximum = (0, 2**bits - 1) if unsigned else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
    for value in (minimum, maximum, str(minimum), str(maximum)):
        converted = integer_policy.integer_value(value, native)
        assert type(converted) is int
        assert converted == int(value)
    for value in (minimum - 1, maximum + 1, str(minimum - 1), str(maximum + 1)):
        with pytest.raises(integer_policy.ClickHouseIntegerValueError, match="integer range"):
            integer_policy.integer_value(value, native)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("42", 42),
        (" +00042 ", 42),
        ("1_000", 1000),
        ("\u00a042\u00a0", 42),
        ("\U0001d7dc\U0001d7de", 46),
        (b"42", 42),
        (False, 0),
        (np.bool_(True), 1),
        (np.uint64(2**64 - 1), 2**64 - 1),
        (1.75, 1),
        (-1.75, -1),
        (Decimal("1.999999999999999999999999999999999999999999"), 1),
        (Decimal("-1.999999999999999999999999999999999999999999"), -1),
        (Decimal("1E-1000"), 0),
    ],
)
def test_ch_integer_authoritative_assignment_retains_historical_python_int_semantics(
    integer_policy, value, expected
):
    converted = integer_policy.integer_value(value, "Int256")

    assert type(converted) is int
    assert converted == expected
    assert integer_policy.normalize_authoritative_integer(value, "Int256") == expected


def test_ch_integer_decimal_conversion_preserves_all_256_bits_without_binary_float(integer_policy):
    class ExactDecimal(Decimal):
        def __float__(self):
            pytest.fail("Integer conversion used binary float")

    maximum = 2**255 - 1
    value = ExactDecimal(str(maximum) + ".999999999999999999999999999999999999999")
    before = value.as_tuple()
    with localcontext() as context:
        context.prec = 2
        assert integer_policy.integer_value(value, "Int256") == maximum

    assert value.as_tuple() == before


@pytest.mark.parametrize("value", [None, pd.NA, float("nan"), np.float64("nan"), Decimal("NaN")])
def test_ch_integer_assignment_retains_existing_nullable_null_policy(integer_policy, value):
    assert integer_policy.integer_value(value, "Nullable(UInt256)") is None
    assert integer_policy.normalize_authoritative_integer(value, "Nullable(UInt256)") is None


@pytest.mark.parametrize(
    "native",
    [
        "Int64",
        "Nullable(Int64)",
        "LowCardinality(Nullable(Int64))",
        "SimpleAggregateFunction(any, Int64)",
    ],
)
def test_ch_integer_logical_type_wrappers_retain_exact_native_domains(integer_policy, native):
    assert integer_policy.is_integer_type(native) is True
    assert integer_policy.validate_integer_type_profile(native) is False
    assert integer_policy.integer_value("-42", native) == -42


@pytest.mark.parametrize(
    "value", ["private-integer-token", "1.5", {}, [], float("inf"), Decimal("Infinity")]
)
def test_ch_integer_invalid_input_errors_are_private_and_nonretryable(integer_policy, value):
    with pytest.raises(integer_policy.ClickHouseIntegerValueError, match="integer range") as error:
        integer_policy.integer_value(value, "Int64")

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert repr(value) not in str(error.value)
    assert "private-integer-token" not in str(error.value)
    assert "CAST the source expression" in str(error.value)
    assert "in from_sql" in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "native", ["Int64(1)", "UInt256()", "Array(Int8(1))", "Tuple(good Int8, bad UInt8(1))"]
)
def test_ch_integer_schema_only_validation_rejects_known_malformed_types(integer_policy, native):
    with pytest.raises(integer_policy.ClickHouseIntegerValueError, match="unparameterized native"):
        integer_policy.validate_integer_type_profile(native)


def test_ch_integer_type_dispatch_preserves_unrelated_schemas_and_bool_conversion(integer_policy):
    values = ["42", None]

    assert integer_policy.is_integer_type("String") is False
    assert integer_policy.contains_integer_type("Array(String)") is False
    assert integer_policy.validate_integer_type_profile("Object('json')") is False
    assert integer_policy.normalize_authoritative_integer(values, None) is values
    assert integer_policy.normalize_authoritative_integer(values, "Array(String)") is values
    assert integer_policy.normalize_authoritative_integer(0, "Nullable(Bool)") is False
    with pytest.raises(integer_policy.ClickHouseIntegerValueError, match="unparameterized native"):
        integer_policy.integer_value(42, "String")


def test_ch_integer_views_do_not_widen_raw_profiles_or_infer_boolean(integer_policy):
    profile = ColumnProfile()
    profile.add("42")

    with pytest.raises(ValueError, match="authoritative integer schema"):
        validate_profile(profile, "Int64", "ch")

    assert declared_domain_fits("String", "Int64", "ch", source_backend="ch") is False
    assert get_backend_adapter("ch").infer_dataframe_column_type(pd.Series([0, 1])) == "Int64"
    assert integer_policy.validate_integer_type_profile("Int64") is False


def test_ch_integer_recursive_views_preserve_caller_input_and_every_map_pair(integer_policy):
    values = [("42", 0), (None, 1)]
    before = deepcopy(values)

    converted = integer_policy.normalize_authoritative_integer(
        values, "Array(Tuple(id Nullable(Int64), flag Bool))"
    )

    assert converted == [(42, False), (None, True)]
    assert values == before
    pairs = OrderedMapPairs((("01", "first"), ("1", "second")))
    result = integer_policy.normalize_authoritative_integer(pairs, "Map(Int64, String)")
    assert isinstance(result, OrderedMapPairs)
    assert list(result.items()) == [(1, "first"), (1, "second")]
    assert list(pairs.items()) == [("01", "first"), ("1", "second")]
