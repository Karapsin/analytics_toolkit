"""Final native map identity includes every comparable key component."""

from uuid import UUID

import numpy as np
import pytest
from analytics_toolkit.sql.backends.map_values import converted_map
from analytics_toolkit.sql.backends.nested_values import row_items
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

IDENTIFIER = "abcdefab-cdef-abcd-efab-cdefabcdefab"


@pytest.mark.parametrize(
    ("native", "source"),
    [
        ("MAP(ROW(k CHAR(2)), VARCHAR)", {("a",): "first", ("a ",): "second"}),
        ("MAP(ROW(k REAL), VARCHAR)", {(16777216.0,): "first", (16777217.0,): "second"}),
        ("MAP(ROW(k UUID), VARCHAR)", {(IDENTIFIER,): "first", (IDENTIFIER.upper(),): "second"}),
        ("MAP(ROW(k ROW(inner_key CHAR(2))), VARCHAR)", {(("a",),): "first", (("a ",),): "second"}),
        ("MAP(ARRAY(ROW(k CHAR(2))), VARCHAR)", {(("a",),): "first", (("a ",),): "second"}),
    ],
)
def test_composite_key_collisions_are_detected_before_materialization(native, source):
    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(native, target_backend="trino").convert(source)


def test_valid_recursive_row_keys_keep_values_and_caller_input_unchanged():
    source = {(("a", IDENTIFIER), 16777216.0): "first", (("b", IDENTIFIER), 16777217.0): "second"}
    native = "MAP(ROW(k ROW(c CHAR(2), id UUID), r REAL), VARCHAR)"
    result = ResolvedColumn(native, target_backend="trino").convert(source)
    assert result == {
        (("a", UUID(IDENTIFIER)), 16777216.0): "first",
        (("b", UUID(IDENTIFIER)), 16777217.0): "second",
    }
    assert next(iter(source))[0][1] == IDENTIFIER


@pytest.mark.parametrize(
    ("native", "source"),
    [
        ("MAP(ROW(k VARCHAR), VARCHAR)", {(None,): "value"}),
        ("MAP(ROW(k ROW(inner_key VARCHAR)), VARCHAR)", {((None,),): "value"}),
        ("MAP(ARRAY(VARCHAR), VARCHAR)", OrderedMapPairs((([None], "value"),))),
    ],
)
def test_trino_composite_indeterminate_keys_reject_before_driver(native, source):
    with pytest.raises(ValueError, match="NULL components"):
        ResolvedColumn(native, target_backend="trino").convert(source)


def test_clickhouse_nested_null_keys_and_converted_duplicate_pairs_are_retained():
    source = OrderedMapPairs(
        (((None, IDENTIFIER), "first"), ((None, IDENTIFIER.upper()), "second"))
    )
    result = ResolvedColumn(
        "Map(Tuple(Nullable(Int64), UUID), String)", target_backend="ch"
    ).convert(source)
    assert isinstance(result, OrderedMapPairs)
    assert list(result.items()) == [
        ((None, UUID(IDENTIFIER)), "first"),
        ((None, UUID(IDENTIFIER)), "second"),
    ]
    arrays = ResolvedColumn("Map(Array(Nullable(Int64)), String)", target_backend="ch").convert(
        OrderedMapPairs((([None], "first"), ([None], "second")))
    )
    assert list(arrays.items()) == [([None], "first"), ([None], "second")]


@pytest.mark.parametrize("value", [(1, 2), (), "x", {"other": 1}, np.array([[1]])])
def test_row_conversion_never_discards_missing_or_extra_fields(value):
    with pytest.raises(ValueError, match="Row"):
        ResolvedColumn("ROW(k BIGINT)", target_backend="trino").convert(value)


def test_array_key_without_supported_hashable_mapping_is_rejected_explicitly():
    with pytest.raises(ValueError, match="supported hashable representation"):
        ResolvedColumn("MAP(ARRAY(BIGINT), VARCHAR)", target_backend="trino").convert(
            {(1,): "value"}
        )
    with pytest.raises(ValueError, match="positional sequence"):
        converted_map([(1, "value")], "trino", mapping_input=False, key_type="ARRAY(BIGINT)")
    with pytest.raises(ValueError, match="unsupported native key structure"):
        converted_map([({}, "value")], "trino", mapping_input=False, key_type="MAP(BIGINT, BIGINT)")
    assert row_items(np.array([2**63 + 1], dtype=np.uint64), [("id", "NUMERIC")]) == [2**63 + 1]


def test_nonnullable_composite_fields_and_ambiguous_named_arity_fail_closed():
    with pytest.raises(ValueError, match="NULL components"):
        ResolvedColumn("Map(Tuple(Int64), String)", target_backend="ch").convert({(None,): "value"})
    with pytest.raises(ValueError, match="Row fields"):
        row_items({"id": 1}, [("id", "BIGINT"), ("id", "BIGINT")])
