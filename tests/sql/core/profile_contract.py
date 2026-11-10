from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.dataframe_types import _infer_column_type
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    NativeType,
    ResolvedColumn,
    decimal_domain,
    declared_domain_fits,
    integer_domain,
    is_timestamp_type,
    parse_native_type,
    resolve_profile,
    timestamp_scale,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal, OrderedMap, OrderedMapPairs


def values(*items):
    result = ColumnProfile()
    for item in items:
        result.add(item)
    return result


@pytest.mark.parametrize(
    ("items", "backend", "expected"),
    [
        ((True, False, None), "ch", "Nullable(Bool)"),
        ((True,), "gp", "BOOLEAN"),
        ((1.25,), "ch", "Float64"),
        ((1.25,), "gp", "DOUBLE PRECISION"),
        ((1.25,), "trino", "DOUBLE"),
        ((dt.date(2024, 1, 1),), "ch", "Date"),
        ((dt.date(1900, 1, 1),), "ch", "Date32"),
        ((dt.date(2024, 1, 1),), "gp", "DATE"),
        ((UUID(int=1),), "ch", "UUID"),
        ((b"x",), "gp", "BYTEA"),
        ((b"x",), "trino", "VARBINARY"),
        (("x",), "ch", "String"),
        (("x",), "gp", "TEXT"),
        (("x",), "trino", "VARCHAR"),
        (([],), "ch", "Array(String)"),
        (((),), "ch", "Tuple()"),
        (((), (1,)), "ch", "String"),
        ((Decimal("Infinity"),), "trino", "VARCHAR"),
        ((Decimal("1E+1000"),), "gp", "NUMERIC"),
        ((10**1000,), "gp", "NUMERIC"),
        ((2**256,), "ch", "String"),
        ((-(2**100),), "ch", "Int128"),
        ((2**127,), "ch", "UInt128"),
        ((2**128,), "ch", "Int256"),
        ((2**255,), "ch", "UInt256"),
        ((pd.Timestamp("2024-01-01T00:00:00Z"),), "ch", "DateTime64(6, 'UTC')"),
        ((pd.Timestamp("2024-01-01T00:00:00Z"),), "trino", "TIMESTAMP(6) WITH TIME ZONE"),
        ((ExactTemporal(1, 9),), "ch", "DateTime64(9)"),
    ],
)
def test_profiles_resolve_native_families_or_exact_text(items, backend, expected):
    assert resolve_profile(values(*items), backend).target_type == expected


@pytest.mark.parametrize(
    ("source", "backend", "expected"),
    [
        ("Int256", "trino", "BIGINT"),
        ("UInt256", "ch", "Nullable(UInt256)"),
        ("Decimal(76, 50)", "trino", "DECIMAL(38, 10)"),
        ("Decimal(38, 9)", "ch", "Nullable(Decimal(38, 9))"),
        ("DateTime64(9, 'UTC')", "trino", "TIMESTAMP(6) WITH TIME ZONE"),
        (None, "gp", "TEXT"),
    ],
)
def test_all_null_profiles_retain_suitable_family_defaults(source, backend, expected):
    assert (
        resolve_profile(values(None), backend, source_type=source, source_backend="ch").target_type
        == expected
    )


@pytest.mark.parametrize(
    ("dtype", "backend", "expected"),
    [
        ("bool", "gp", "BOOLEAN"),
        ("float64", "ch", "Float64"),
        ("datetime64[ns]", "trino", "TIMESTAMP(6)"),
        ("datetime64[ns, UTC]", "trino", "TIMESTAMP(6) WITH TIME ZONE"),
        ("UInt64", "ch", "UInt64"),
        ("object", "gp", "TEXT"),
    ],
)
def test_empty_dataframe_family_defaults(dtype, backend, expected):
    assert _infer_column_type(pd.Series([], dtype=dtype), backend) == expected


def test_profile_merge_preserves_nested_and_temporal_bounds() -> None:
    left = values([1])
    right = values((3, "y"))
    left.merge(right)
    assert left.count == 2
    assert left.families == {"array", "tuple"}
    assert left.structurally_compatible is False
    empty = ColumnProfile()
    empty.merge(values((1,)))
    assert empty.children[0].minimum_integer == 1
    assert empty.tuple_width == 1
    differing = values(())
    differing.merge(values((1,)))
    assert not differing.structurally_compatible
    a = values(pd.Timestamp("2024-01-02T00:00:00Z"))
    a.merge(values(pd.Timestamp("2024-01-01")))
    assert a.aware
    assert a.naive
    assert a.minimum_date == dt.date(2024, 1, 1)
    assert a.maximum_date == dt.date(2024, 1, 2)
    assert resolve_profile(a, "gp").text_fallback


def test_authoritative_native_nested_and_date_schemas_validate_full_profiles() -> None:
    validate_profile(values(None, [1, None]), "Nullable(Array(Int64))", "gp")
    validate_profile(values([1, 2]), "Array(Int64)", "ch")
    validate_profile(values((1, "x")), "Tuple(Int64, String)", "ch")
    validate_profile(values(dt.date(1900, 1, 1)), "Date32", "ch")
    validate_profile(values(dt.date(1900, 1, 1)), "DATE", "gp")
    validate_profile(values(True), "BOOLEAN", "gp")
    validate_profile(values(Decimal("1.20")), "DECIMAL(3, 2)", "trino")
    with pytest.raises(ValueError, match="nested"):
        validate_profile(values([1], "x"), "Array(Int64)", "ch")
    with pytest.raises(ValueError, match="date schema"):
        validate_profile(values("1900-01-01"), "DATE", "gp")
    with pytest.raises(ValueError, match="integer"):
        validate_profile(values("1"), "BIGINT", "gp")


@pytest.mark.parametrize(
    ("source", "target", "backend", "expected"),
    [
        (None, "BIGINT", "gp", False),
        ("BIGINT", None, "gp", False),
        ("UInt8", "SMALLINT", "trino", True),
        ("UInt16", "SMALLINT", "trino", False),
        ("UInt64", "DECIMAL(20, 0)", "trino", True),
        ("UInt64", "DECIMAL(19, 0)", "trino", False),
        ("Decimal(18, 9)", "Decimal(20, 10)", "ch", True),
        ("Decimal(18, 9)", "Decimal(18, 10)", "ch", False),
        ("Decimal(18, 9)", "String", "ch", False),
        ("DateTime64(9)", "DateTime64(9)", "ch", True),
        ("DateTime64(9)", "DateTime64(6)", "ch", False),
        ("Array(BIGINT)", "array(bigint)", "trino", True),
        ("Array(Int256)", "TEXT", "gp", False),
        ("bytea", "varbinary", "trino", True),
        ("bytea", "String", "ch", True),
    ],
)
def test_declared_domain_fit_requires_a_proof(source, target, backend, expected):
    assert declared_domain_fits(source, target, backend) is expected


def test_native_type_parser_retains_literal_bytes_and_recursive_arguments() -> None:
    parsed = parse_native_type("Tuple(String, Enum8('x,y'=1, 'it\\'s'=2))")
    assert parsed.arguments[1] == "Enum8('x,y'=1, 'it\\'s'=2)"
    assert parse_native_type("bad [ type").name == "bad [ type"
    assert NativeType("Nullable", ("LowCardinality(String)",)).base.name == "string"
    assert integer_domain("bigint") == (-(2**63), 2**63 - 1)
    assert integer_domain("text") is None
    assert decimal_domain("text") is None
    assert timestamp_scale("DateTime('UTC')") == 0
    assert timestamp_scale("timestamp") == 6
    assert is_timestamp_type("TIMESTAMP(6) WITH TIME ZONE")
    assert (
        resolve_profile(
            values({"x": 1}), "ch", source_type="Map(String, Int64)", source_backend="ch"
        ).target_type
        == "Map(String, Int64)"
    )
    assert (
        resolve_profile(values(OrderedMap(((1, "x"),))), "trino").target_type
        == "MAP(BIGINT, VARCHAR)"
    )
    assert (
        resolve_profile(values(OrderedMapPairs(((1, "x"),))), "trino").target_type
        == "MAP(BIGINT, VARCHAR)"
    )


def test_authoritative_numeric_uuid_binary_and_map_validation() -> None:
    validate_profile(values(Decimal("1.20")), "NUMERIC", "gp")
    validate_profile(values(Decimal("1.20")), "Float64", "ch")
    validate_profile(values(str(UUID(int=1))), "UUID", "gp")
    validate_profile(values({1: Decimal("0.0001")}), "Map(Int64, Decimal(10, 4))", "ch")
    assert resolve_profile(values("x"), "ch", authoritative_type="String").convert("x") == "x"
    assert (
        resolve_profile(values(Decimal("1.20")), "ch", authoritative_type="Float64").convert(
            Decimal("1.20")
        )
        == 1.2
    )
    assert resolve_profile(values(str(UUID(int=1))), "gp", authoritative_type="UUID").convert(
        str(UUID(int=1))
    ) == UUID(int=1)
    for items, target, backend in [
        ((UUID(int=1),), "NUMERIC", "gp"),
        ((Decimal("1E+309"),), "DOUBLE", "trino"),
        (("x",), "Float64", "ch"),
        ((1e100,), "Float32", "ch"),
        (("not-a-uuid",), "UUID", "gp"),
        ((1,), "VARBINARY", "trino"),
        (([1],), "Map(Int64, String)", "ch"),
    ]:
        with pytest.raises(ValueError, match="authoritative"):
            validate_profile(values(*items), target, backend)
    assert (
        resolve_profile(values({1: 2, 3: None}), "ch").target_type == "Map(Int64, Nullable(Int64))"
    )
    assert resolve_profile(values({None: 2}), "ch").text_fallback
    assert resolve_profile(values(None, [1]), "ch").text_fallback
    far = ExactTemporal(10382256000 * 10**7 + 1, 7, "UTC")
    assert resolve_profile(values(far), "trino").convert(far) == far.isoformat(utc=True)


def test_authoritative_character_bounds_and_unbounded_numeric_fast_path() -> None:
    assert declared_domain_fits("NUMERIC", "NUMERIC", "gp")
    assert declared_domain_fits("VARCHAR(2)", "VARCHAR(10)", "trino")
    assert not declared_domain_fits("VARCHAR(10)", "VARCHAR(2)", "trino")
    assert not declared_domain_fits("TEXT", "VARCHAR(2)", "gp")
    validate_profile(values("ok"), "VARCHAR(2)", "trino")
    validate_profile(values(None, "x"), "LowCardinality(Nullable(String))", "ch")
    with pytest.raises(ValueError, match="text length"):
        validate_profile(values("too long"), "VARCHAR(2)", "trino")


def test_profile_conversion_certifies_exact_text_and_unrepresentable_native_bounds() -> None:
    assert resolve_profile(values(None), "gp", authoritative_type="NUMERIC").convert(None) is None
    assert resolve_profile(values(ExactTemporal(1, 6)), "gp").convert(
        ExactTemporal(1, 6)
    ) == pd.Timestamp(1000, unit="ns")
    assert (
        ResolvedColumn("VARCHAR", "DateTime64(9)", "ch").convert(pd.Timestamp("2024-01-01"))
        == "2024-01-01T00:00:00.000000000"
    )
    assert resolve_profile(values(Decimal("1E-16384")), "gp").text_fallback
    assert resolve_profile(values(10**131072), "gp").text_fallback
    assert (
        resolve_profile(values(Decimal("0.0001")), "trino").metadata()["encoding_version"] is None
    )
    keys = values({1: 2})
    keys.merge(values({3: 4}))
    assert keys.children[0].maximum_integer == 3
    assert keys.children[1].maximum_integer == 4
    keys = values([1])
    keys.add({2: 3})
    assert not keys.structurally_compatible
    empty_tuple = values(())
    empty_tuple.merge(values(()))
    assert empty_tuple.structurally_compatible


def test_nullable_boolean_native_float_bounds_and_timestamp_authority() -> None:
    nullable = pd.Series([True, pd.NA, False], dtype="boolean")
    assert _infer_column_type(nullable, "ch") == "Nullable(Bool)"
    validate_profile(values(*nullable), "Nullable(Bool)", "ch")
    validate_profile(values(np.finfo(np.float64).max), "DOUBLE", "trino")
    validate_profile(values(np.finfo(np.float32).max), "Float32", "ch")
    for item, target in [
        (pd.Timestamp("1969-01-01"), "DateTime"),
        (pd.Timestamp("2299-01-01"), "DateTime64(9)"),
        (pd.Timestamp("1800-01-01"), "DateTime64(6)"),
        (pd.Timestamp("2024-01-01"), "DateTime64(10)"),
    ]:
        with pytest.raises(ValueError, match="authoritative"):
            validate_profile(values(item), target, "ch")
    validate_profile(values(pd.Timestamp("2024-01-01")), "DateTime64(6)", "ch")
    assert timestamp_scale("TIMESTAMP(3) WITH TIME ZONE") == 3
    assert not declared_domain_fits("Nullable(Int64)", "Int64", "ch", source_backend="ch")
    assert not declared_domain_fits("BIGINT", "Int64", "ch", source_backend="gp")
    assert declared_domain_fits("Enum8('a'=1)", "Enum8('a'=1)", "ch", source_backend="ch")
    adapter = get_backend_adapter("trino")
    assert (
        adapter.map_same_backend_source_type_to_target(SourceColumn("v", "timestamp"))
        == "TIMESTAMP(6)"
    )
    assert (
        adapter.map_same_backend_source_type_to_target(SourceColumn("v", "timestamp(9)"))
        == "TIMESTAMP(6)"
    )
