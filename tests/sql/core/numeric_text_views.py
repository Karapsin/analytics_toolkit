"""Authoritative scalar views retain missing values and ordinary map shapes."""

from __future__ import annotations

from copy import deepcopy
from enum import Enum
from importlib import import_module

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.numeric_text import (
    normalize_authoritative_value as normalize_ch_authoritative_value,
)
from analytics_toolkit.sql.backends.trino.numeric_text import (
    normalize_authoritative_value as normalize_trino_authoritative_value,
)
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, validate_profile
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs


class _Scalar(Enum):
    INTEGER = "42"
    NULL = None


@pytest.mark.parametrize(
    ("backend", "native"),
    [("gp", "BIGINT[]"), ("trino", "ARRAY(BIGINT)"), ("ch", "Array(Nullable(Int64))")],
)
def test_authoritative_array_view_unwraps_enum_and_retains_missing_sentinels(backend, native):
    numeric = import_module(f"analytics_toolkit.sql.backends.{backend}.numeric_text")
    missing = [None, pd.NA, pd.NaT, float("nan")]
    values = [_Scalar.INTEGER, *missing, _Scalar.NULL]

    result = numeric.normalize_authoritative_value(values, native)

    assert result[0] == 42
    assert all(actual is original for actual, original in zip(result[1:5], missing))
    assert result[5] is None
    assert values[0] is _Scalar.INTEGER
    assert values[5] is _Scalar.NULL


def test_authoritative_ch_map_view_retains_dictionary_for_unique_hashable_keys():
    values = {"enabled": np.bool_(True), "disabled": np.bool_(False)}

    result = normalize_ch_authoritative_value(values, "Map(String, Bool)")

    assert type(result) is dict
    assert result == {"enabled": True, "disabled": False}
    assert all(type(value) is bool for value in result.values())
    assert all(isinstance(value, np.bool_) for value in values.values())


def test_authoritative_trino_map_view_retains_original_ordered_pairs_after_conversion():
    values = OrderedMapPairs((("1", "first"), ("2", "second")))

    result = normalize_trino_authoritative_value(values, "MAP(BIGINT, VARCHAR)")

    assert isinstance(result, OrderedMapPairs)
    assert tuple(result.items()) == ((1, "first"), (2, "second"))
    assert tuple(values.items()) == (("1", "first"), ("2", "second"))


@pytest.mark.parametrize("value", [{"id": "private-row-value"}, ("private-row-value",)])
def test_authoritative_row_view_reports_sanitized_nested_schema_failure(value):
    with pytest.raises(ValueError, match="authoritative nested schema") as caught:
        normalize_trino_authoritative_value(value, "ROW(id BIGINT, flag BOOLEAN)")

    assert "private-row-value" not in str(caught.value)


def test_authoritative_map_view_preserves_items_when_array_keys_become_unhashable():
    values = {("1", "2"): "first", ("3",): "second"}
    before = deepcopy(values)

    result = normalize_trino_authoritative_value(values, "MAP(ARRAY(BIGINT), VARCHAR)")

    assert isinstance(result, OrderedMapPairs)
    assert tuple(result.items()) == (([1, 2], "first"), ([3], "second"))
    assert len(result) == len(values)
    assert values == before
    assert all(type(key) is tuple for key in values)


def test_authoritative_map_view_leaves_nonmapping_shape_for_native_schema_validation():
    values = [("private-map-key", "1.25")]
    native = "MAP(BIGINT, DOUBLE)"

    result = normalize_trino_authoritative_value(values, native)

    assert result is values
    assert result == [("private-map-key", "1.25")]
    profile = ColumnProfile(native_type=native, source_backend="trino")
    profile.add(result)
    with pytest.raises(ValueError, match="authoritative nested schema") as caught:
        validate_profile(profile, native, "trino")

    assert "private-map-key" not in str(caught.value)
