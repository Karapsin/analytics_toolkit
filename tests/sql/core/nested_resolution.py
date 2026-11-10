from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.nested_types import nested_descriptor
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import (
    OrderedMapPairs,
    decimal_shape,
    decode_value,
    encode_value,
)
from analytics_toolkit.sql.dml.load.type_validation import (
    convert_dataframe_schema,
    validate_dataframe_schema,
)
from analytics_toolkit.sql.dml.transfer.schema import map_source_schema_to_target


def profile(items, native=None, source_backend=None):
    result = ColumnProfile(native_type=native, source_backend=source_backend)
    for item in items:
        result.add(item)
    return result


@pytest.mark.parametrize(
    ("items", "backend", "expected"),
    [
        ([[1, None], [2, 3]], "trino", "ARRAY(BIGINT)"),
        ([[Decimal("0.0001"), Decimal("1.2")]], "trino", "ARRAY(DECIMAL(5, 4))"),
        ([(1, "x")], "trino", 'ROW("field0" BIGINT, "field1" VARCHAR)'),
        ([{"x": 1, "y": None}], "trino", "MAP(VARCHAR, BIGINT)"),
        ([[1, None]], "gp", "BIGINT[]"),
        ([[[1, 2], [3, None]]], "gp", "BIGINT[]"),
        ([[[1]], [[2, 3], [4, 5]]], "gp", "BIGINT[]"),
        ([[UUID(int=1), None]], "gp", "UUID[]"),
        ([[]], "gp", "TEXT[]"),
        ([{"x": 1}], "gp", "TEXT"),
    ],
)
def test_nested_profiles_prefer_exact_native_arrays_maps_and_rows(items, backend, expected):
    resolved = resolve_profile(profile(items), backend)
    assert resolved.target_type == expected
    assert "JSONB" not in resolved.target_type


@pytest.mark.parametrize(
    "item",
    [
        [[1], [2, 3]],
        [[1], None],
        [[], []],
        [[1], 2],
        [[[[[[[1]]]]]]],
    ],
)
def test_gp_ragged_null_or_empty_subarrays_use_exact_typed_text(item):
    result = resolve_profile(profile([item]), "gp")
    assert result.target_type == "TEXT"
    payload = json.loads(result.convert(item))
    assert payload["v"] == 1
    assert payload["value"][0] == "array"


def test_partitioned_profile_merge_keeps_nested_extreme_and_shape_facts() -> None:
    first = profile([[1, 2]])
    first.merge(profile([[2**255]]))
    resolved = resolve_profile(first, "trino")
    assert resolved.text_fallback
    assert str(2**255) in resolved.convert([2**255])
    first = profile([[[1], [2]]])
    first.merge(profile([[[3], [4, 5]]]))
    assert resolve_profile(first, "gp").text_fallback


def test_map_key_identity_duplicate_and_coercion_rules_prevent_data_loss() -> None:
    pairs = OrderedMapPairs(((1, "a"), (1, "b")))
    portable = resolve_profile(profile([pairs]), "trino")
    assert portable.text_fallback
    assert json.loads(portable.convert(pairs))["value"][1] == [
        [["int", "1"], ["str", "a"]],
        [["int", "1"], ["str", "b"]],
    ]
    clickhouse = resolve_profile(profile([pairs]), "ch")
    assert clickhouse.target_type == "Map(Int64, String)"
    assert clickhouse.convert(pairs) == pairs
    unique = OrderedMapPairs(((1, "a"), (2, "b")))
    assert resolve_profile(profile([unique]), "trino").convert(unique) == {1: "a", 2: "b"}
    mixed = {1: "a", "1": "b"}
    assert resolve_profile(profile([mixed]), "trino").text_fallback
    authority = profile([mixed], "MAP(VARCHAR, VARCHAR)", "trino")
    with pytest.raises(ValueError, match="keys"):
        validate_profile(authority, "MAP(VARCHAR, VARCHAR)", "trino")
    with pytest.raises(ValueError, match="keys"):
        validate_profile(profile([{None: 1}]), "MAP(VARCHAR, BIGINT)", "trino")
    mixed_values = {"a": 1, "b": "1"}
    assert resolve_profile(profile([mixed_values]), "trino").text_fallback


def test_declared_fixed_rank_and_gp_unconstrained_rank_use_correct_proofs() -> None:
    assert declared_domain_fits("Array(Int64)", "ARRAY(BIGINT)", "trino", source_backend="ch")
    assert not declared_domain_fits("integer[]", "ARRAY(INTEGER)", "trino", source_backend="gp")
    assert declared_domain_fits("integer[]", "INTEGER[][]", "gp", source_backend="gp")
    assert declared_domain_fits("Array(Int64)", "BIGINT[]", "gp", source_backend="ch")
    assert not declared_domain_fits("Array(Array(Int64))", "BIGINT[]", "gp", source_backend="ch")
    native = "integer[]"
    values = [[1], [[2]]]
    assert (
        resolve_profile(
            profile(values, native, "gp"), "gp", source_type=native, source_backend="gp"
        ).target_type
        == native
    )
    assert resolve_profile(
        profile(values, native, "gp"), "trino", source_type=native, source_backend="gp"
    ).text_fallback
    values = [[1], [2, 3]]
    assert (
        resolve_profile(
            profile(values, native, "gp"), "trino", source_type=native, source_backend="gp"
        ).target_type
        == "ARRAY(INTEGER)"
    )
    assert map_source_schema_to_target(
        [SourceColumn("v", "int8")], "trino", source_backend="gp"
    ) == {"v": "BIGINT"}


def test_named_rows_preserve_quoted_names_and_native_child_families() -> None:
    native = "Tuple(id UInt256, \"spaced name\" DateTime64(6, 'UTC'))"
    value = {"id": 1, "spaced name": pd.Timestamp("2024-01-01T01:02:03.123456Z")}
    current = profile([value], native, "ch")
    result = resolve_profile(current, "trino", source_type=native, source_backend="ch")
    assert result.target_type == 'ROW("id" BIGINT, "spaced name" TIMESTAMP(6) WITH TIME ZONE)'
    assert result.convert(value) == (1, value["spaced name"].to_pydatetime())
    schema = {"r": 'ROW("Events" BIGINT, "events" UUID)'}
    value = {"Events": 42, "events": UUID(int=1)}
    frame = pd.DataFrame({"r": [value]})
    validate_dataframe_schema(frame, schema, "trino")
    converted, _ = convert_dataframe_schema(frame, schema, "trino")
    assert converted.iloc[0, 0] == (42, UUID(int=1))
    with pytest.raises(ValueError, match="nested"):
        validate_dataframe_schema(pd.DataFrame({"r": [{"Events": 42}]}), schema, "trino")
    assert nested_descriptor('ROW("a.b" BIGINT, " spaced " UUID)')[1][0][0] == "a.b"


def test_nested_existing_timestamp_precision_is_strict_but_explicit_reduction_is_deliberate() -> (
    None
):
    value = [[pd.Timestamp("2024-01-01T00:00:00.123456789Z")]]
    native = "ARRAY(TIMESTAMP(6) WITH TIME ZONE)"
    current = profile(value, native, "trino")
    with pytest.raises(ValueError, match="precision"):
        validate_profile(current, native, "trino", allow_timestamp_precision_loss=False)
    validate_profile(current, native, "trino")
    resolved = resolve_profile(current, "trino", authoritative_type=native)
    assert resolved.convert(value[0])[0].microsecond == 123456
    assert not hasattr(resolved.convert(value[0])[0], "nanosecond")


@pytest.mark.parametrize("zero", [0, Decimal(0), Decimal("0E+5")])
def test_zero_needs_no_integral_digit_when_merging_fractional_precision(zero):
    fraction = Decimal("0." + "0" * 37 + "1")
    result = resolve_profile(profile([zero, fraction]), "trino")
    assert result.target_type == "DECIMAL(38, 38)"
    assert result.convert(fraction) == fraction
    assert decimal_shape([Decimal("0E+5")]) == (1, 0)


def test_authoritative_gp_int8_alias_uses_64_bit_domain() -> None:
    validate_profile(profile([200, 2**63 - 1]), "int8", "gp")
    with pytest.raises(ValueError, match="integer"):
        validate_profile(profile([200]), "Int8", "ch")
    with pytest.raises(ValueError, match="integer"):
        validate_profile(profile([2**63]), "int8", "gp")


def test_numpy_array_cells_keep_large_unsigned_values_and_null_elements() -> None:
    integer = 2**63 + 1
    array = np.array([integer], dtype=np.uint64)
    nullable = np.array([integer, None], dtype=object)
    assert decode_value(encode_value(nullable)) == [integer, None]
    for backend in ("ch", "trino", "gp"):
        current = profile([array, nullable])
        resolved = resolve_profile(current, backend)
        assert not resolved.text_fallback
        expected = [Decimal(integer), None] if backend in {"gp", "trino"} else [integer, None]
        assert resolved.convert(nullable) == expected
        validate_profile(current, resolved.target_type, backend)
    gp = resolve_profile(profile([[array, np.array([integer], dtype=np.uint64)]]), "gp")
    assert not gp.text_fallback
    ragged = resolve_profile(
        profile([[array, np.array([integer, integer], dtype=np.uint64)]]), "gp"
    )
    assert ragged.text_fallback


@pytest.mark.parametrize(
    "native",
    [
        'ROW("MixedCase" BIGINT, "Spaced Name" ARRAY(UUID))',
        'ARRAY(ROW("MixedCase" BIGINT, "whenUTC" TIMESTAMP(6) WITH TIME ZONE))',
        'MAP(VARCHAR, ROW("MixedCase" BIGINT))',
    ],
)
def test_same_trino_native_signatures_preserve_quoted_field_identity(native):
    assert map_source_schema_to_target(
        [SourceColumn("v", native)], "trino", source_backend="trino"
    ) == {"v": native}
