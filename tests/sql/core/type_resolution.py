from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.core.type_resolution import (
    ColumnProfile,
    decimal_domain,
    declared_domain_fits,
    integer_domain,
    native_candidates,
    parse_native_type,
    requires_adaptive_resolution,
    resolve_column_type,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.dml.transfer.schema import map_source_schema_to_target


def profile(*values):
    result = ColumnProfile()
    for value in values:
        result.observe(value)
    return result


@pytest.mark.parametrize(
    "native",
    [
        "Int128",
        "Int256",
        "UInt128",
        "UInt256",
        "Date32",
        "DateTime64(9, 'Europe/Moscow')",
        "Array(Nullable(Int256))",
        "Tuple(Int64, Array(UInt256))",
        "Map(String, UInt128)",
    ],
)
def test_clickhouse_native_type_identity_survives_structural_mapping(native: str) -> None:
    result = map_source_schema_to_target([SourceColumn("v", native)], "ch", source_backend="ch")
    assert result == {"v": native}


@pytest.mark.parametrize(
    ("native", "target", "expected"),
    [
        ("UInt8", "trino", "SMALLINT"),
        ("UInt16", "trino", "INTEGER"),
        ("UInt32", "trino", "BIGINT"),
        ("UInt64", "trino", "DECIMAL(20, 0)"),
        ("Int128", "gp", "NUMERIC(39, 0)"),
        ("UInt256", "gp", "NUMERIC(78, 0)"),
        ("UInt256", "trino", "VARCHAR"),
        ("Array(Int256)", "trino", "VARCHAR"),
        ("Map(String, UInt64)", "gp", "TEXT"),
        ("DateTime64(9, 'UTC')", "trino", "TIMESTAMP(6) WITH TIME ZONE"),
    ],
)
def test_cross_backend_mapping_never_uses_a_narrow_integer_domain(native, target, expected) -> None:
    assert map_source_schema_to_target([SourceColumn("v", native)], target) == {"v": expected}


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0, 1], "BIGINT"),
        ([2**64 - 1, None], "DECIMAL(20, 0)"),
        ([2**127 - 1], "VARCHAR"),
        ([Decimal("1.2"), Decimal("0.0034")], "DECIMAL(5, 4)"),
        ([Decimal("0.0001")], "DECIMAL(4, 4)"),
        ([Decimal("1E+5")], "DECIMAL(6, 0)"),
        ([Decimal("1E+38")], "VARCHAR"),
    ],
)
def test_adaptive_inference_uses_complete_actual_values_before_text(values, expected) -> None:
    resolved = resolve_column_type(SourceColumn("v", "Int256"), "trino", profile(*values), "ch")
    assert resolved.target_type == expected
    assert resolved.certified_input_count == len(values)
    if expected == "VARCHAR":
        assert resolved.text_fallback
        expected_text = str(values[0]) if isinstance(values[0], int) else format(values[0], "f")
        assert resolved.convert(values[0]) == expected_text


def test_last_row_extreme_and_all_key_profiles_determine_one_schema() -> None:
    first = profile(0, 1)
    second = profile(None, 2**255)
    first.merge(second)
    resolved = resolve_profile(first, "trino", source_type="Int256", source_backend="ch")
    assert resolved.target_type == "VARCHAR"
    assert resolved.convert(0) == "0"
    assert resolved.metadata()["certified_input_count"] == 4
    assert resolved.metadata()["encoding_version"] == 1
    assert (
        resolve_profile(first, "gp", source_type="Int256", source_backend="ch").target_type
        == "NUMERIC(77, 0)"
    )


def test_supported_declared_family_is_preserved_and_wide_domain_needs_profile() -> None:
    assert (
        resolve_profile(profile(1), "trino", source_type="UInt64", source_backend="ch").target_type
        == "DECIMAL(20, 0)"
    )
    assert not requires_adaptive_resolution([SourceColumn("v", "UInt64")], "trino", "ch")
    assert requires_adaptive_resolution([SourceColumn("v", "Int256")], "trino", "ch")
    assert requires_adaptive_resolution(
        [SourceColumn("v", "UInt64")], "trino", "ch", {"v": "BIGINT"}
    )
    assert not declared_domain_fits("DateTime64(9)", "TIMESTAMP(6)", "trino")
    assert not declared_domain_fits("DateTime64(6, 'UTC')", "TIMESTAMP(6)", "trino")
    assert not declared_domain_fits("unknown", "VARCHAR", "trino")
    assert native_candidates("trino")[0] == "BIGINT"


@pytest.mark.parametrize(
    ("values", "target", "backend"),
    [
        ([2**63], "BIGINT", "trino"),
        ([Decimal("0.0001")], "DECIMAL(4, 2)", "trino"),
        ([None], "Int64", "ch"),
        (
            [
                dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).replace(tzinfo=None),
                dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
            ],
            "TIMESTAMP(6)",
            "trino",
        ),
        ([dt.date(1900, 1, 1)], "Date", "ch"),
        ([1], "BOOLEAN", "gp"),
        ([[1, 2**64]], "Array(Int64)", "ch"),
        ([(1, 2)], "Tuple(Int64)", "ch"),
    ],
)
def test_authoritative_schema_never_silently_widens(values, target, backend) -> None:
    with pytest.raises(ValueError, match=r"authoritative|NULL"):
        resolve_profile(profile(*values), backend, authoritative_type=target)


def test_temporal_profiles_preserve_nanos_and_reject_invented_timezone() -> None:
    nanos = pd.Timestamp("2024-01-01 00:00:00.123456789")
    resolved = resolve_profile(profile(nanos), "trino")
    assert resolved.target_type == "VARCHAR"
    assert resolved.convert(nanos).endswith(".123456789")
    assert resolve_profile(profile(nanos), "ch").target_type == "DateTime64(9)"
    assert resolve_profile(profile(nanos.floor("us")), "trino").target_type == "TIMESTAMP(6)"
    aware = pd.Timestamp("2024-01-01T00:00:00+03:00")
    mixed = resolve_profile(profile(nanos, aware), "ch")
    assert mixed.text_fallback
    # Explicit caller-requested timestamp precision reduction is deliberate.
    validate_profile(profile(nanos), "TIMESTAMP(6)", "trino")


def test_recursive_shapes_and_domain_parsing() -> None:
    assert integer_domain("UInt128") == (0, 2**128 - 1)
    assert decimal_domain("Nullable(Decimal256(9))") == (76, 9)
    assert parse_native_type("Tuple(String, Map(String, Tuple(Int64, String)))").arguments == (
        "String",
        "Map(String, Tuple(Int64, String))",
    )
    with pytest.raises(ValueError, match="Unbalanced"):
        parse_native_type("Array(Tuple(Int64)")
    assert resolve_profile(profile([1, None], [2]), "ch").target_type == "Array(Nullable(Int64))"
    assert resolve_profile(profile((1, "x")), "ch").target_type == "Tuple(Int64, String)"
    assert resolve_profile(profile((1,), (2, 3)), "ch").text_fallback
    assert resolve_profile(profile(b"abc"), "ch").convert(b"abc") == b"abc"
    assert resolve_profile(profile(1), "gp", authoritative_type="TEXT").convert(1) == "1"
    assert resolve_profile(profile(1), "trino", authoritative_type="DECIMAL(20, 0)").convert(
        1
    ) == Decimal(1)
