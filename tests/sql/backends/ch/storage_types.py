from __future__ import annotations

import traceback
from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.ch.storage_types import logical_storage_type, storage_fields
from analytics_toolkit.sql.backends.nested_types import nested_descriptor
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    declared_domain_fits,
    validate_profile,
)


@pytest.mark.parametrize(
    ("physical", "logical"),
    [
        ("SimpleAggregateFunction(sum, UInt64)", "UInt64"),
        ("SimpleAggregateFunction(anyLast, Nullable(IPv6))", "Nullable(IPv6)"),
        ("Point", "Tuple(Float64, Float64)"),
        ("Ring", "Array(Tuple(Float64, Float64))"),
        ("Polygon", "Array(Array(Tuple(Float64, Float64)))"),
        ("MultiPolygon", "Array(Array(Array(Tuple(Float64, Float64))))"),
        ("Array(Point)", "Array(Tuple(Float64, Float64))"),
        ("Nullable(SimpleAggregateFunction(sum, UInt64))", "Nullable(UInt64)"),
        (
            "LowCardinality(Nullable(SimpleAggregateFunction(any, String)))",
            "LowCardinality(Nullable(String))",
        ),
        ("Map(String, Ring)", "Map(String, Array(Tuple(Float64, Float64)))"),
        (
            "Tuple(position Point, value UInt64)",
            "Tuple(position Tuple(Float64, Float64), value UInt64)",
        ),
        ("Tuple(Point, String)", "Tuple(Tuple(Float64, Float64), String)"),
        ("Nested(x UInt64, y String)", "Array(Tuple(x UInt64, y String))"),
        (
            "Nested(Date Point, String UInt64)",
            "Array(Tuple(Date Tuple(Float64, Float64), String UInt64))",
        ),
        (
            "Nested(point Point, values Array(SimpleAggregateFunction(sum, UInt64)))",
            "Array(Tuple(point Tuple(Float64, Float64), values Array(UInt64)))",
        ),
        (
            "Array(Nested(`key with spaces` UInt64, `value.with.dots` Point))",
            "Array(Array(Tuple(`key with spaces` UInt64, "
            "`value.with.dots` Tuple(Float64, Float64))))",
        ),
    ],
)
def test_structural_storage_aliases_have_explicit_logical_domains(physical, logical) -> None:
    assert logical_storage_type(physical) == logical
    assert logical_storage_type(logical) == logical
    assert declared_domain_fits(logical, logical, "ch", source_backend="ch")


@pytest.mark.parametrize(
    "physical",
    [
        "UInt64",
        "Tuple()",
        "Tuple(a UInt64, b String)",
        "Nullable(IPv4)",
        "Object('json')",
        "JSON(max_dynamic_paths=100)",
        "AggregateFunction(quantilesTDigest(0.1, 0.9), Float64)",
        "CustomNativeType('literal, (bytes)')",
        "point",
    ],
)
def test_non_aliases_keep_their_original_declaration(physical) -> None:
    assert logical_storage_type(physical) == physical


def test_named_field_and_literal_bytes_survive_an_adjacent_alias() -> None:
    raw = "Nested(`quote``name` Point, `case Sensitive` Enum8('a, (b)  c' = 1, 'd\\\\e' = 2))"
    logical = logical_storage_type(raw)
    assert logical == (
        "Array(Tuple(`quote``name` Tuple(Float64, Float64), "
        "`case Sensitive` Enum8('a, (b)  c' = 1, 'd\\\\e' = 2)))"
    )
    fields = nested_descriptor(nested_descriptor(logical)[1][0][1])[1]
    assert [name for name, _ in fields] == ["quote`name", "case Sensitive"]


def test_nested_field_identity_is_case_sensitive() -> None:
    logical = logical_storage_type("Nested(Events UInt64, events String)")
    assert logical == "Array(Tuple(Events UInt64, events String))"


@pytest.mark.parametrize(
    ("physical", "fields"),
    [
        ("Nested(x UInt64, y String)", (("x", "UInt64"), ("y", "String"))),
        (
            "Tuple(Nested(x UInt64), Array(Point))",
            (("field0", "Nested(x UInt64)"), ("field1", "Array(Point)")),
        ),
        (
            "Tuple(`a.b` Nested(x UInt64), `with space` Enum8('a, (b)  c' = 1))",
            (("a.b", "Nested(x UInt64)"), ("with space", "Enum8('a, (b)  c' = 1)")),
        ),
        ("Nested(`é name` Point)", (("é name", "Point"),)),
        ("Tuple()", ()),
        ("Array(Nested(x UInt64))", None),
        ("Object('json')", None),
    ],
)
def test_codec_field_layout_preserves_raw_nested_markers_and_literal_bytes(
    physical, fields
) -> None:
    assert storage_fields(physical) == fields


@pytest.mark.parametrize(
    "physical",
    [
        "SimpleAggregateFunction()",
        "SimpleAggregateFunction(sum)",
        "SimpleAggregateFunction(, UInt64)",
        "SimpleAggregateFunction(sum,)",
        "SimpleAggregateFunction(sum, UInt64, String)",
        "Nested()",
        "Nested(UInt64)",
        "Nested(x UInt64, x String)",
        "Nested(`` UInt64)",
        "Nested(x)",
        "Nested(x UInt64, y)",
        "Point(Float64)",
        "Array()",
        "Map(Point)",
        "Nullable(Point, UInt64)",
        "Array(Point))(UInt64",
    ],
)
def test_invalid_structural_storage_declarations_fail_before_resolution(physical) -> None:
    with pytest.raises(
        ValueError,
        match=r"^Invalid ClickHouse structural storage type declaration\.$",
    ):
        logical_storage_type(physical)


@pytest.mark.parametrize("resolver", [logical_storage_type, storage_fields])
@pytest.mark.parametrize(
    "physical",
    [
        "Nested(`private_identifier UInt64)",
        "SimpleAggregateFunction(any, Enum8('private_literal = 1))",
    ],
)
def test_malformed_type_literals_do_not_expose_private_text_in_error_traces(
    resolver, physical
) -> None:
    with pytest.raises(
        ValueError,
        match=r"^Invalid ClickHouse structural storage type declaration\.$",
    ) as caught:
        resolver(physical)
    error = caught.value
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert "private_identifier" not in rendered
    assert "private_literal" not in rendered


@pytest.mark.parametrize(
    ("physical", "value", "expected"),
    [
        ("SimpleAggregateFunction(sum, UInt64)", 2**64 - 1, 2**64 - 1),
        ("Point", (Decimal("1.25"), Decimal("2.5")), (1.25, 2.5)),
        ("Ring", [(1.25, 2.5), (3.0, 4.0)], [(1.25, 2.5), (3.0, 4.0)]),
        ("Nested(x UInt64, y String)", [{"x": 7, "y": "a"}], [(7, "a")]),
    ],
)
def test_existing_value_validation_and_conversion_certify_the_expanded_domain(
    physical, value, expected
) -> None:
    logical = logical_storage_type(physical)
    profile = ColumnProfile(native_type=logical, source_backend="ch")
    profile.add(value)
    validate_profile(profile, logical, "ch")
    result = ResolvedColumn(logical, target_backend="ch").convert(value)
    assert result == expected


@pytest.mark.parametrize(
    ("physical", "value", "error"),
    [
        (
            "SimpleAggregateFunction(sum, UInt64)",
            -1,
            "Selected input is incompatible with authoritative integer schema",
        ),
        (
            "Point",
            (1.25,),
            "Selected tuple width differs from authoritative nested schema",
        ),
        (
            "Point",
            (1.25, 2.5, 3.0),
            "Selected tuple width differs from authoritative nested schema",
        ),
        (
            "Ring",
            [(1.25, 2.5), (3.0, 4.0, 5.0)],
            "Selected input is incompatible with authoritative nested schema",
        ),
        (
            "Nested(x UInt64, y String)",
            [{"x": 7}],
            "Selected input is incompatible with authoritative nested schema",
        ),
    ],
)
def test_invalid_values_are_rejected_by_the_underlying_native_domain(
    physical,
    value,
    error,
) -> None:
    logical = logical_storage_type(physical)
    profile = ColumnProfile(native_type=logical, source_backend="ch")
    profile.add(value)
    with pytest.raises(ValueError, match=error):
        validate_profile(profile, logical, "ch")
