"""Native map-key conversion must never erase an input entry."""

from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.map_values import converted_map
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs
from analytics_toolkit.sql.dml.load.type_validation import validate_dataframe_schema

IDENTIFIER = "abcdefab-cdef-abcd-efab-cdefabcdefab"
SOURCE = {IDENTIFIER: "first", IDENTIFIER.upper(): "second"}


def test_trino_uuid_key_collision_rejects_before_dictionary_construction():
    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn("MAP(UUID, VARCHAR)", target_backend="trino").convert(SOURCE)


def test_clickhouse_converted_collision_retains_every_ordered_entry():
    converted = ResolvedColumn("Map(UUID, String)", target_backend="ch").convert(SOURCE)
    assert isinstance(converted, OrderedMapPairs)
    assert list(converted.items()) == [(UUID(IDENTIFIER), "first"), (UUID(IDENTIFIER), "second")]


def test_authoritative_dataframe_map_validation_runs_conversion_before_publication():
    frame = pd.DataFrame({"v": pd.Series([{IDENTIFIER: "valid"}, SOURCE], dtype=object)})
    with pytest.raises(ValueError, match="map"):
        validate_dataframe_schema(frame, {"v": "MAP(UUID,VARCHAR)"}, "trino")


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("MAP(REAL, BIGINT)", {16777216.0: 1, 16777217.0: 2}),
        ("MAP(CHAR(2), BIGINT)", {"a": 1, "a ": 2}),
        ("MAP(VARCHAR, BIGINT)", {1: 1, "1": 2}),
    ],
)
def test_unique_map_keys_use_final_native_identity_without_changing_scalar_values(native, value):
    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(native, target_backend="trino").convert(value)
    assert ResolvedColumn("REAL", target_backend="trino").convert(16777217.0) == 16777217.0
    assert ResolvedColumn("CHAR(2)", target_backend="trino").convert("a ") == "a "


def test_noncolliding_native_keys_and_ordinary_container_compatibility():
    source = {"a": 1, "a\u00a0": 2}
    assert ResolvedColumn("MAP(CHAR(2), BIGINT)", target_backend="trino").convert(source) == source
    assert ResolvedColumn("MAP(DOUBLE, BIGINT)", target_backend="trino").convert(
        {16777216.0: 1, 16777217.0: 2}
    ) == {16777216.0: 1, 16777217.0: 2}
    value = {IDENTIFIER: "first"}
    assert ResolvedColumn("Map(UUID, String)", target_backend="ch").convert(value) == {
        UUID(IDENTIFIER): "first"
    }
    pairs = OrderedMapPairs(((IDENTIFIER, "first"),))
    assert isinstance(
        ResolvedColumn("Map(UUID, String)", target_backend="ch").convert(pairs), OrderedMapPairs
    )


def test_clickhouse_float32_collision_keeps_both_physical_entries():
    value = {16777216.0: 1, 16777217.0: 2}
    converted = ResolvedColumn("Map(Float32, Int64)", target_backend="ch").convert(value)
    assert isinstance(converted, OrderedMapPairs)
    assert list(converted.items()) == list(value.items())


def test_converted_map_null_comparability_and_ordered_pair_contracts():
    with pytest.raises(ValueError, match="cannot be NULL"):
        converted_map([(None, "value")], "trino", mapping_input=False)
    with pytest.raises(ValueError, match="not comparable"):
        converted_map([([1], "value")], "trino", mapping_input=False)
    pairs = [([1], "first"), ([1], "second")]
    preserved = converted_map(pairs, "ch", mapping_input=True)
    assert isinstance(preserved, OrderedMapPairs)
    assert list(preserved.items()) == pairs
    with pytest.raises(ValueError, match="not unique"):
        converted_map([(1, "first"), (1, "second")], "", mapping_input=True)
    with pytest.raises(ValueError, match="floating-point representation"):
        ResolvedColumn("MAP(REAL, VARCHAR)", target_backend="trino").convert({1e100: "overflow"})
