"""Accepted NumPy scalars reach native transport without precision loss."""

from decimal import Decimal
from enum import Enum

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    resolve_profile,
)
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs
from analytics_toolkit.sql.dml.load.type_validation import validate_dataframe_schema
from psycopg2.extensions import adapt


@pytest.mark.parametrize(
    ("backend", "native"), [("gp", "BIGINT"), ("trino", "BIGINT"), ("ch", "Nullable(Int64)")]
)
@pytest.mark.parametrize("value", [np.bool_(False), np.bool_(True)])
def test_numpy_booleans_use_native_integer_transport(backend, native, value):
    resolution = ResolvedColumn(native, target_backend=backend)
    profiles = validate_converted_rows([(value,), (None,)], [resolution], backend=backend)
    converted = resolution.convert(value)
    assert type(converted) is int
    assert converted == (1 if value else 0)
    assert adapt(converted).getquoted() == (b"1" if value else b"0")
    assert profiles[0].families == {"integer"}
    assert profiles[0].null_count == 1


@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        ("BOOLEAN", np.bool_(False), False),
        ("BIGINT", np.int8(-128), -128),
        ("BIGINT", np.int64(2**53 + 1), 2**53 + 1),
        ("BIGINT", np.uint32(2**32 - 1), 2**32 - 1),
        ("NUMERIC(20,0)", np.uint64(2**64 - 1), Decimal(2**64 - 1)),
        ("DOUBLE PRECISION", np.float16(1.5), 1.5),
        ("DOUBLE PRECISION", np.float32(1.5), 1.5),
        ("DOUBLE PRECISION", np.float64(1.5), 1.5),
        ("VARCHAR(2)", np.str_("é"), "é"),
        ("BYTEA", np.bytes_(b"\x00\xff"), b"\x00\xff"),
    ],
)
def test_accepted_numpy_scalar_families_reach_real_gp_adaptation(native, value, expected):
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    validate_dataframe_schema(frame, {"value": native}, "gp")
    converted = ResolvedColumn(native, target_backend="gp").convert(value)
    assert converted == expected
    assert adapt(converted).getquoted()


def test_numpy_boolean_nested_integer_leaves_and_map_keys_are_normalized():
    value = [np.bool_(False), np.bool_(True), None]
    for backend, native in [
        ("gp", "BIGINT[]"),
        ("trino", "ARRAY(BIGINT)"),
        ("ch", "Array(Nullable(Int64))"),
    ]:
        converted = ResolvedColumn(native, target_backend=backend).convert(value)
        assert converted == [0, 1, None]
        assert all(item is None or type(item) is int for item in converted)
        if backend == "gp":
            assert adapt(converted).getquoted() == b"ARRAY[0,1,NULL]"
    assert ResolvedColumn("ROW(v BIGINT, b BOOLEAN)", target_backend="trino").convert(
        (np.bool_(True), np.bool_(False))
    ) == (1, False)
    pairs = OrderedMapPairs(((np.bool_(True), "first"), (1, "second")))
    with pytest.raises(ValueError, match="keys are not unique"):
        ResolvedColumn("MAP(BIGINT,VARCHAR)", target_backend="trino").convert(pairs)
    converted = ResolvedColumn("Map(Int64,String)", target_backend="ch").convert(pairs)
    assert isinstance(converted, OrderedMapPairs)
    assert list(converted.items()) == [(1, "first"), (1, "second")]
    assert all(type(key) is int for key, _ in converted.items())


def test_numpy_boolean_enum_and_default_inference_keep_their_logical_family():
    member = Enum("Flag", {"OFF": np.bool_(False)}).OFF
    assert ResolvedColumn("BIGINT", target_backend="gp").convert(member) == 0
    assert ResolvedColumn("BOOLEAN", target_backend="gp").convert(member) is False
    for values, expected in [([np.bool_(False), np.bool_(True)], "BOOLEAN"), ([0, 1], "BIGINT")]:
        profile = ColumnProfile()
        for value in values:
            profile.add(value)
        assert resolve_profile(profile, "gp").target_type == expected


@pytest.mark.parametrize("native", ["NUMERIC(4,0)", "DOUBLE PRECISION"])
def test_numpy_boolean_does_not_broaden_other_authoritative_numeric_policies(native):
    frame = pd.DataFrame({"value": pd.Series([np.bool_(True)], dtype=object)})
    with pytest.raises(ValueError, match="authoritative"):
        validate_dataframe_schema(frame, {"value": native}, "gp")


@pytest.mark.parametrize("unit", ["D", "ns"])
def test_numpy_temporal_objects_are_rejected_without_epoch_integer_coercion(unit):
    value = np.datetime64("2025-01-01", unit)
    frame = pd.DataFrame({"value": pd.Series([value], dtype=object)})
    with pytest.raises(TypeError, match="Unsupported exact replay value type"):
        validate_dataframe_schema(frame, {"value": "TIMESTAMP(6)"}, "gp")
    assert frame.iloc[0, 0] is value
