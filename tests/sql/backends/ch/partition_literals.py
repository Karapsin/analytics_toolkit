"""Partition operands preserve exact String and recursive tuple identities."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from struct import pack, unpack
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.partition_literals import render_partition_value
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from sqlglot import exp, parse_one

from tests.sql._support.ch_partition_scope import install_partition_scope
from tests.sql.backends.ch._support.network_transport import encode_normalized


def _partition_operand(statement):
    tree = parse_one(statement, read=ClickHouseDialect)
    return tree.find(exp.DropPartition).expressions[0].expressions[0]


def _partition_value(expression):
    if isinstance(expression, exp.Literal):
        return expression.this if expression.is_string else int(expression.this)
    assert isinstance(expression, (exp.Tuple, exp.Struct))
    return tuple(_partition_value(item) for item in expression.expressions)


@pytest.mark.parametrize("value", [r"path\next", "tail\\", r"path\'quoted"])
def test_public_drop_partitions_drops_only_the_exact_string_partition(
    monkeypatch, write_sql_connections, value
):
    newline = value.replace("\\n", "\n") if "\\n" in value else "unrelated\npartition"
    rows = {value: [(1,)], newline: [(100,)], "untouched": [(200,)]}
    before = deepcopy(rows)
    state = install_partition_scope(
        monkeypatch, write_sql_connections, driver="http", layout="local"
    )
    target = state.host_tables[state.coordinator]["analytics.events"]
    target.rows = [(key, row[0]) for key, values in rows.items() for row in values]
    resources = state.snapshot()

    assert (
        sql.drop_partitions("ch", "analytics.events", [value], retry_cnt=1, timeout_increment=0)
        is None
    )

    actual = {key: [(identifier,)] for key, identifier in target.rows}
    assert actual == {key: data for key, data in before.items() if key != value}
    resources[state.coordinator]["analytics.events"]["rows"] = [
        (key, row[0]) for key, values in before.items() if key != value for row in values
    ]
    assert state.snapshot() == resources
    assert len(state.commands) == 1
    operand = _partition_operand(state.commands[0])
    assert isinstance(operand, exp.Literal)
    assert operand.is_string
    assert operand.this == value
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("Tuple(String, UInt8)", (r"path\next", 7)),
        ("Tuple(UInt8, Tuple(String, UInt8))", (7, (r"path\next", 9))),
    ],
)
def test_upsert_tuple_partition_values_remain_typed_after_final_sql_regeneration(
    transport, native, value
):
    assert encode_normalized(native, [deepcopy(value)], transport)
    statements = get_backend_adapter("ch").build_drop_upsert_partition_sqls(
        "sandbox.events", partition_column="p", partition_values=[value], ch_only_shard=True
    )
    assert len(statements) == 1
    tree = parse_one(statements[0], read=ClickHouseDialect)
    regenerated = tree.sql(dialect=ClickHouseDialect)
    assert _partition_value(_partition_operand(regenerated)) == value


@pytest.mark.parametrize("value", [r"path\next", "tail\\", "path\nnext"])
def test_upsert_top_level_string_partition_keeps_existing_exact_literal_contract(value):
    statements = get_backend_adapter("ch").build_drop_upsert_partition_sqls(
        "sandbox.events", partition_column="p", partition_values=[value], ch_only_shard=True
    )
    assert _partition_value(_partition_operand(statements[0])) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (False, "FALSE"),
        (-(2**63), str(-(2**63))),
        (2**64 - 1, str(2**64 - 1)),
        (-(2**255), f"toInt256('{-(2**255)}')"),
        (2**255, f"toUInt256('{2**255}')"),
        (
            Decimal("12345678901234567890.123456789"),
            "toDecimal256('12345678901234567890.123456789', 9)",
        ),
        (Decimal("0.0001"), "toDecimal256('0.0001', 4)"),
        (Decimal("1E+5"), "toDecimal256('100000', 0)"),
        (b"\xff\x00", "unhex('ff00')"),
        (bytearray(b"\xff\x00"), "unhex('ff00')"),
        (memoryview(b"\xff\x00"), "unhex('ff00')"),
        (UUID(int=1), "toUUID('00000000-0000-0000-0000-000000000001')"),
        (IPv4Address("192.0.2.1"), "toIPv4('192.0.2.1')"),
        (IPv6Address("2001:db8::1"), "toIPv6('2001:db8::1')"),
        (date(1900, 1, 1), "toDate32('1900-01-01')"),
        (pd.Timestamp(1), "toDateTime64('1970-01-01 00:00:00.000000001', 9)"),
        (ExactTemporal(1, 9, "UTC"), "toDateTime64('1970-01-01 00:00:00.000000001', 9, 'UTC')"),
        ((), "tuple()"),
        ((1,), "tuple(1)"),
    ],
)
def test_native_partition_scalars_keep_exact_type_and_value_in_sql(value, expected):
    actual = parse_one(render_partition_value(value), read=ClickHouseDialect)
    assert actual == parse_one(expected, read=ClickHouseDialect)


@pytest.mark.parametrize("value", [0.0, -0.0, 1.25, float("inf"), float("nan")])
def test_partition_floats_preserve_decoded_ieee_bits_in_typed_expression(value):
    bits = unpack("<Q", pack("<d", value))[0]
    expected = parse_one(f"reinterpretAsFloat64(toUInt64('{bits}'))", read=ClickHouseDialect)
    assert parse_one(render_partition_value(value), read=ClickHouseDialect) == expected


@pytest.mark.parametrize("value", [-(2**255) - 1, 2**256, Decimal("1E+76"), Decimal("NaN")])
def test_partition_values_outside_native_numeric_domains_reject_before_any_sql(value):
    with pytest.raises(ValueError, match=r"^Selected input has no exact supported") as caught:
        render_partition_value(value)
    assert caught.value.analytics_toolkit_sql_retry_safe is False


def test_unsupported_partition_value_never_calls_private_stringification():
    class PrivateValue:
        def __str__(self):
            return pytest.fail("Partition rendering stringified a private unsupported value.")

    with pytest.raises(ValueError, match=r"^Selected input has no exact supported"):
        render_partition_value((7, PrivateValue()))
