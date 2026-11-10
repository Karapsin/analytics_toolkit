"""Complete-domain edge contracts for exact nested representation and recovery."""

from enum import Enum
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import dataframe_types
from analytics_toolkit.sql.backends.ch.value_validation import validate_constrained_value
from analytics_toolkit.sql.backends.nested_types import (
    convert_nested,
    declared_nested_fits,
    map_declared_nested,
    resolve_nested,
    validate_nested,
)
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    _integer_type,
    _resolve_native,
    declared_domain_fits,
    resolve_profile,
)
from analytics_toolkit.sql.core.exact_values import truncate_temporal


def observed(value, native=None):
    profile = ColumnProfile(native_type=native)
    profile.add(value)
    return profile


@pytest.mark.parametrize(
    ("profile", "backend", "source", "expected"),
    [
        (ColumnProfile(), "trino", "ARRAY(BIGINT)", "ARRAY(BIGINT)"),
        (ColumnProfile(), "trino", "BIGINT", None),
        (ColumnProfile(), "gp", "BIGINT[]", "BIGINT[]"),
        (ColumnProfile(families={"array"}), "gp", None, "TEXT[]"),
        (observed([1]), "gp", "Array(Int8)", "SMALLINT[]"),
        (observed([1]), "unsupported", None, None),
        (ColumnProfile(families={"array"}, null_count=1), "ch", None, None),
        (observed((2**300,)), "trino", None, None),
        (ColumnProfile(families={"map"}), "ch", None, "Map(String, String)"),
        (ColumnProfile(families={"map"}), "trino", None, "MAP(VARCHAR, VARCHAR)"),
        (observed({(1, 2): 3}), "trino", None, None),
    ],
)
def test_nested_inference_defaults_and_unsupported_native_structures(
    profile, backend, source, expected
):
    assert resolve_nested(profile, backend, source) == expected


@pytest.mark.parametrize(
    ("source", "target", "backend", "expected"),
    [
        ("Array(Array(Int8))", "", "gp", "SMALLINT[]"),
        ("Map(Int8,Int8)", "", "gp", None),
        ("Array(Tuple(Int8))", "", "gp", None),
        ("Map(Int8,Int8)", "", "ch", "Map(Int8, Int8)"),
        ("ROW(a BIGINT)", "", "ch", "Tuple(Nullable(Int64))"),
    ],
)
def test_declared_nested_native_backend_mapping(source, target, backend, expected):
    assert (
        map_declared_nested(source, backend, "trino" if source.startswith("ROW") else "ch")
        == expected
    )


def test_nested_validation_rank_keys_and_identity():
    assert convert_nested([[1, 2]], "BIGINT[][]", "gp") == [[1, 2]]
    assert convert_nested(7, "BIGINT", "gp") == 7
    assert validate_nested(ColumnProfile(families={"array"}), "BIGINT[]", "gp")
    assert validate_nested(observed([[1]]), "BIGINT[][]", "gp")
    for profile in [observed("bad"), observed([[1], [2, 3]])]:
        with pytest.raises(ValueError, match="rectangular"):
            validate_nested(profile, "BIGINT[]", "gp")
    with pytest.raises(ValueError, match="NULL keys"):
        validate_nested(observed({None: 1}), "Map(Int64, Int64)", "ch")
    assert declared_nested_fits("BIGINT[][]", "BIGINT[]", "gp", "gp")
    assert not declared_nested_fits("Map(Int8,Int8)", "MAP(BIGINT,BIGINT)", "trino", "ch")


def test_exact_scalar_profiles_aliases_and_native_preservation():
    class State(Enum):
        ONE = 1

    profile = observed(State.ONE)
    assert profile.minimum_integer == 1
    assert not observed("\ud800").utf8_valid
    assert observed(float("inf")).maximum_numeric_magnitude == 0
    profile.merge(observed([1]))
    assert profile.array_leaves.minimum_integer == 1
    assert validate_constrained_value(7, "Int8") == 7
    assert ResolvedColumn("DOUBLE").convert(1) == 1.0
    assert (
        resolve_profile(ColumnProfile(), "trino", source_type="Decimal(5,2)").target_type
        == "DECIMAL(5, 2)"
    )
    assert (
        resolve_profile(ColumnProfile(), "trino", source_type="Array(Int8)").target_type
        == "ARRAY(TINYINT)"
    )
    assert (
        resolve_profile(observed(1), "ch", source_type="Int8", source_backend="ch").target_type
        == "Int8"
    )
    assert declared_domain_fits("VARCHAR(2)", "VARCHAR", "gp")
    assert declared_domain_fits("bytea", "Nullable(String)", "ch", source_backend="gp")
    assert dataframe_types._infer_common_sql_type(pd.Series([1])) == "BIGINT"
    assert dataframe_types._is_uuid_series(pd.Series([UUID(int=1)]))
    assert not dataframe_types._is_uuid_series(pd.Series([], dtype=object))
    assert not dataframe_types._is_uuid_series(pd.Series(["x"]))
    assert truncate_temporal(pd.Timestamp("1970-01-01 00:00:00.123456789"), 7).nanosecond == 700


def test_integer_candidates_and_authoritative_enum_conversion():
    assert _integer_type(-128, 127, "ch", "Int8") == "Int8"
    assert _integer_type(0, 255, "ch", "UInt8") == "UInt8"
    assert _integer_type(-10, 10, "ch", "smallint") == "Int64"
    assert ResolvedColumn("UUID").convert("00000000-0000-0000-0000-000000000001") == UUID(int=1)
    native = "Enum8('ok'=1)"
    profile = observed("ok", native)
    assert resolve_profile(profile, "ch", authoritative_type=native).convert("ok") == "ok"


def test_empty_decimal_family_default_is_native_without_guessing_values():
    assert _resolve_native(ColumnProfile(), "trino", "Decimal(5,2)") == "DECIMAL(5, 2)"


def test_authoritative_utf8_fixedstring_accepts_exact_byte_boundary():
    profile = observed("é", "FixedString(2)")
    assert resolve_profile(profile, "ch", authoritative_type="FixedString(2)").convert("é") == "é"


def test_temporal_precision_reduction_and_enum_checkpoint_identity():
    import datetime as dt  # noqa: PLC0415 - exact scalar boundaries

    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - codec boundary
        ExactTemporal,
        OrderedMapPairs,
        decode_value,
        encode_value,
    )

    class State(Enum):
        ONE = 1

    assert decode_value(encode_value(State.ONE)) == 1
    assert truncate_temporal(ExactTemporal(123456789, 9), 6) == ExactTemporal(123456, 6)
    assert (
        truncate_temporal(
            dt.datetime(1970, 1, 1, microsecond=123456, tzinfo=dt.timezone.utc), 3
        ).microsecond
        == 123000
    )
    assert ResolvedColumn("BOOLEAN").convert(True) is True
    profile = observed(OrderedMapPairs((([1], 2),)))
    assert not profile.map_comparable_keys
    assert dataframe_types.infer_gp_dataframe_column_type(None, pd.Series([1])) == "BIGINT"
