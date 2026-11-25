"""Certification and transport consume the same logical Enum values."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pytest
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import (
    OrderedMapPairs,
    decode_value,
    encode_value,
    typed_text,
)
from analytics_toolkit.sql.core.replay_spool import ReplaySpool


def _enum(value):
    return Enum("LogicalValue", {"VALUE": value}).VALUE


@pytest.mark.parametrize(
    ("backend", "native", "value"),
    [
        ("gp", "BIGINT", 2**53 + 1),
        ("trino", "BIGINT", -(2**53 + 1)),
        ("ch", "UInt256", 2**255),
        ("gp", "BIGINT", None),
        ("trino", "BIGINT", None),
        ("ch", "Nullable(Int64)", None),
        ("gp", "BOOLEAN", False),
        ("trino", "BOOLEAN", False),
        ("ch", "Bool", False),
        ("gp", "NUMERIC(29,9)", Decimal("12345678901234567890.123456789")),
        ("trino", "DECIMAL(29,9)", Decimal("12345678901234567890.123456789")),
        ("ch", "Decimal128(9)", Decimal("12345678901234567890.123456789")),
        ("gp", "DATE", dt.date(1900, 1, 1)),
        ("trino", "DATE", dt.date(1900, 1, 1)),
        ("ch", "Date32", dt.date(1900, 1, 1)),
        ("gp", "UUID", UUID(int=1)),
        ("trino", "UUID", UUID(int=1)),
        ("ch", "UUID", UUID(int=1)),
        ("gp", "BYTEA", b"\x00\xff"),
        ("trino", "VARBINARY", b"\x00\xff"),
        ("ch", "String", b"\x00\xff"),
        ("gp", "VARCHAR(1)", "a"),
        ("trino", "VARCHAR(1)", "a"),
        ("ch", "FixedString(1)", "a"),
    ],
)
def test_logical_enum_scalars_match_certified_native_values(backend, native, value):
    wrapped = _enum(_enum(value))
    profile = ColumnProfile(native_type=native, source_backend=backend)
    profile.add(wrapped)
    validate_profile(profile, native, backend)
    resolution = ResolvedColumn(native, target_backend=backend)
    converted = resolution.convert(wrapped)
    assert converted == value
    assert type(converted) is type(value)
    assert not isinstance(converted, Enum)
    assert validate_converted_rows([(wrapped,)], [resolution], backend=backend)[0].count == 1


def test_logical_enum_containers_share_native_shape_and_leaf_conversion():
    number = _enum(2**53 + 1)
    missing = _enum(None)
    inner = _enum([number, missing])
    value = _enum([inner, _enum([number, number])])
    profile = ColumnProfile(native_type="BIGINT[][]", source_backend="gp")
    profile.add(value)
    validate_profile(profile, "BIGINT[][]", "gp")
    converted = ResolvedColumn("BIGINT[][]", target_backend="gp").convert(value)
    assert converted == [[2**53 + 1, None], [2**53 + 1, 2**53 + 1]]
    assert all(not isinstance(item, Enum) for row in converted for item in row)
    row = _enum((_enum([number, missing]), _enum(dt.date(1900, 1, 1))))
    assert ResolvedColumn("Tuple(Array(Nullable(Int64)), Date32)", target_backend="ch").convert(
        row
    ) == ([2**53 + 1, None], dt.date(1900, 1, 1))


def test_logical_enum_text_uses_scalar_values_and_keeps_structured_origin():
    assert typed_text(_enum("a")) == "a"
    assert typed_text(_enum(None)) is None
    assert typed_text(_enum(Decimal("0.0001"))) == "0.0001"
    assert typed_text(_enum(dt.date(1900, 1, 1))) == "1900-01-01"
    assert (
        typed_text(_enum([]), source_backend="ch", source_type="Array(Int64)")
        == '{"v":1,"backend":"ch","type":"Array(Int64)","value":["array",[]]}'
    )


def test_enum_key_collisions_survive_real_version_one_checkpoint():
    value = {_enum(1): "first", 1: "second"}
    assert len(value) == 2
    with ReplaySpool(["value"]) as spool:
        spool.write_rows([(value,)])
        spool.certify()
        assert spool.path.read_bytes().startswith(b"ATK-ROWS\x01\n")
        rows = list(spool.iter_rows())
        assert len(rows) == 1
        replayed = rows[0][0]
        assert isinstance(replayed, OrderedMapPairs)
        assert list(replayed.items()) == [(1, "first"), (1, "second")]
        assert list(spool.iter_rows()) == [(replayed,)]
        with pytest.raises(ValueError, match="keys are not unique"):
            validate_converted_rows(
                [(replayed,)],
                [ResolvedColumn("MAP(BIGINT,VARCHAR)", target_backend="trino")],
                backend="trino",
            )
        converted = ResolvedColumn("Map(Int64,String)", target_backend="ch").convert(replayed)
        assert isinstance(converted, OrderedMapPairs)
        assert list(converted.items()) == [(1, "first"), (1, "second")]


def test_enum_map_keys_retain_null_collisions_and_hashability_facts():
    profile = ColumnProfile()
    profile.add({_enum(1): "first", 1: "second", _enum(None): "missing"})
    assert profile.map_duplicate_keys
    assert profile.map_null_keys
    value = decode_value(encode_value({_enum([1, 2]): "array key"}))
    assert isinstance(value, OrderedMapPairs)
    assert list(value.items()) == [([1, 2], "array key")]
    assert decode_value(encode_value({"key": _enum(1)})) == {"key": 1}
    assert isinstance(decode_value(encode_value(OrderedMapPairs((("key", 1),)))), OrderedMapPairs)
