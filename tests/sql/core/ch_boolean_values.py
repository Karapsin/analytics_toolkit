"""ClickHouse Bool views follow numeric wire semantics without inferring Bool."""

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
def boolean_policy():
    return importlib.import_module("analytics_toolkit.sql.backends.ch.boolean_values")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (False, False),
        (True, True),
        (np.bool_(False), False),
        (np.bool_(True), True),
        (0, False),
        (1, True),
        (-2, True),
        (2**256, True),
        (np.uint64(2**64 - 1), True),
        (0.0, False),
        (-0.0, False),
        (np.float32(0), False),
        (np.float64(-1.5), True),
        (float.fromhex("0x0.0000000000001p-1022"), True),
        (float("inf"), True),
        (float("-inf"), True),
        (Decimal("0.000"), False),
        (Decimal("-0.00"), False),
        (Decimal("1E-10000"), True),
        (Decimal("-1E+10000"), True),
        (Decimal("Infinity"), True),
    ],
)
def test_ch_boolean_numeric_wire_view_preserves_zero_and_nonzero(boolean_policy, value, expected):
    result = boolean_policy.boolean_value(value)

    assert type(result) is bool
    assert result is expected
    assert boolean_policy.normalize_authoritative_boolean(value, "Bool") is expected


@pytest.mark.parametrize("value", [None, pd.NA, float("nan"), np.float64("nan"), Decimal("NaN")])
def test_ch_boolean_existing_null_policy_is_preserved_for_nullable_input(boolean_policy, value):
    assert boolean_policy.normalize_authoritative_boolean(value, "Nullable(Bool)") is None


def test_ch_boolean_decimal_conversion_avoids_float_and_context_rounding(boolean_policy):
    class ExactDecimal(Decimal):
        def __float__(self):
            pytest.fail("Bool wire conversion used binary float for Decimal")

    value = ExactDecimal("1E-10000")
    before = value.as_tuple()
    with localcontext() as context:
        context.prec = 2
        assert boolean_policy.boolean_value(value) is True

    assert value.as_tuple() == before


@pytest.mark.parametrize("value", ["false", "0", b"0", [], {}, pd.Timestamp("2024-01-01")])
def test_ch_boolean_rejects_non_numeric_truthiness_with_private_errors(boolean_policy, value):
    with pytest.raises(boolean_policy.ClickHouseBooleanValueError, match="Bool") as error:
        boolean_policy.boolean_value(value)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert repr(value) not in str(error.value)


def test_ch_boolean_does_not_invoke_arbitrary_truthiness_callbacks(boolean_policy):
    calls = []

    class Truthy:
        def __bool__(self):
            calls.append(True)
            return True

    with pytest.raises(boolean_policy.ClickHouseBooleanValueError):
        boolean_policy.boolean_value(Truthy())

    assert calls == []


@pytest.mark.parametrize(
    "native",
    [
        "Bool",
        "Boolean",
        "Nullable(Bool)",
        "LowCardinality(Nullable(Boolean))",
        "SimpleAggregateFunction(any, Bool)",
    ],
)
def test_ch_boolean_type_views_preserve_logical_aliases_and_wrappers(boolean_policy, native):
    assert boolean_policy.is_boolean_type(native) is True
    assert boolean_policy.validate_boolean_type_profile(native) is False
    assert boolean_policy.normalize_authoritative_boolean(0, native) is False


@pytest.mark.parametrize(
    ("native", "values"),
    [
        ("Bool(1)", []),
        ("Boolean()", []),
        ("Nullable(Bool(1))", []),
        ("Nullable(Bool(1))", [None]),
        ("LowCardinality(Nullable(Boolean(1)))", []),
        ("LowCardinality(Nullable(Boolean(1)))", [None]),
        ("Nullable(Bool, UInt8)", []),
        ("Nullable(Bool, UInt8)", [None]),
        ("Array(Bool(1))", []),
        ("Tuple(valid Bool, invalid Boolean(1))", []),
    ],
)
def test_ch_boolean_invalid_declarations_reject_empty_or_null_only_profiles(native, values):
    profile = ColumnProfile()
    for value in values:
        profile.add(value)

    message = (
        "storage type"
        if native == "Nullable(Bool, UInt8)"
        else "native type declaration does not accept type arguments"
    )
    with pytest.raises(ValueError, match=message):
        validate_profile(profile, native, "ch")

    assert profile.count == len(values)


@pytest.mark.parametrize(
    "native", ["UInt64", "Array(UInt64)", "Object('json')", "AggregateFunction(uniq, UInt64)"]
)
def test_ch_boolean_schema_only_hook_does_not_claim_other_native_encodings(boolean_policy, native):
    assert boolean_policy.is_boolean_type(native) is False
    assert boolean_policy.validate_boolean_type_profile(native) is False


def test_ch_boolean_views_do_not_widen_the_raw_family_gate_or_integer_inference():
    profile = ColumnProfile()
    profile.add(0)
    profile.add(1)
    with pytest.raises(ValueError, match="authoritative boolean schema"):
        validate_profile(profile, "Bool", "ch")

    assert declared_domain_fits("UInt8", "Bool", "ch", source_backend="ch") is False
    values = pd.Series([0, 1], dtype="int64")
    assert get_backend_adapter("ch").infer_dataframe_column_type(values) == "Int64"


def test_ch_boolean_none_is_still_rejected_by_nonnullable_schema_validation():
    profile = ColumnProfile()
    profile.add(None)

    with pytest.raises(ValueError, match="nonnullable authoritative column"):
        validate_profile(profile, "Bool", "ch")


def test_ch_boolean_nested_views_preserve_caller_data_and_duplicate_map_pairs(boolean_policy):
    values = [(0, 11), (1, 12)]
    before = deepcopy(values)
    converted = boolean_policy.normalize_authoritative_boolean(
        values, "Array(Tuple(flag Nullable(Bool), amount UInt64))"
    )
    assert converted == [(False, 11), (True, 12)]
    assert values == before

    pairs = OrderedMapPairs(((1, "first"), (2, "second")))
    result = boolean_policy.normalize_authoritative_boolean(pairs, "Map(Bool, String)")
    assert isinstance(result, OrderedMapPairs)
    assert list(result.items()) == [(True, "first"), (True, "second")]
    assert list(pairs.items()) == [(1, "first"), (2, "second")]


def test_ch_boolean_nonboolean_assignment_views_are_unchanged(boolean_policy):
    values = [0, 1, None]

    assert boolean_policy.is_boolean_type("UInt8") is False
    assert boolean_policy.contains_boolean_type("Array(UInt8)") is False
    assert boolean_policy.normalize_authoritative_boolean(values, None) is values
    assert boolean_policy.normalize_authoritative_boolean(values, "Array(UInt8)") is values
