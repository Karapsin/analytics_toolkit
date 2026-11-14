"""Binary parameters use native bytes before the installed Trino SDK formatter."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.trino import insert

from tests.sql._support.trino_parameters import DriverParameterConnection, render_parameter


def insert_value(value, native="VARBINARY"):
    connection = DriverParameterConnection()
    insert.insert_rows(
        get_backend("trino"),
        connection,
        "sandbox.binary_values",
        ["payload"],
        [(value,)],
        target_column_types={"payload": native},
        trino_insert_chunk_size=1,
    )
    return connection


def test_readonly_memoryview_reaches_real_sdk_as_native_bytes():
    value = memoryview(b"\x00\xff")

    connection = insert_value(value)

    statement, parameters, rendered = connection.statements[0]
    assert "CAST(? AS VARBINARY)" in statement
    assert rendered == ["X'00ff'"]
    assert parameters == [b"\x00\xff"]
    assert type(parameters[0]) is bytes
    assert value.readonly
    assert bytes(value) == b"\x00\xff"
    assert connection.cursor_close_calls == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (bytearray(b"\x00\xff"), b"\x00\xff"),
        (memoryview(bytearray(b"\x00\xff")), b"\x00\xff"),
        (memoryview(b""), b""),
        (bytearray(), b""),
        (memoryview(b"abcdef")[::2], b"ace"),
        (b"\x00\xff", b"\x00\xff"),
        (b"", b""),
        (None, None),
    ],
)
def test_binary_scalar_parameters_preserve_exact_bytes_order_empty_and_null(value, expected):
    original = None if value is None else bytes(value)

    connection = insert_value(value)

    statement, parameters, rendered = connection.statements[0]
    assert "CAST(? AS VARBINARY)" in statement
    assert parameters == [expected]
    assert type(parameters[0]) is (type(None) if expected is None else bytes)
    assert rendered == ["NULL" if expected is None else f"X'{expected.hex()}'"]
    assert value is None if original is None else bytes(value) == original
    assert connection.cursor_close_calls == 1


def leaves(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(key)
            yield from leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from leaves(item)
    else:
        yield value


@pytest.mark.parametrize("kind", ["array", "row", "map_values", "map_keys"])
def test_recursive_binary_native_leaves_reach_real_sdk_without_mutating_callers(kind):
    native, value, expected = {
        "array": (
            "ARRAY(VARBINARY)",
            [memoryview(b"\x00\xff"), bytearray(), None, memoryview(b"abcdef")[::2]],
            [b"\x00\xff", b"", None, b"ace"],
        ),
        "row": (
            "ROW(first VARBINARY, rest ARRAY(VARBINARY), absent VARBINARY)",
            (bytearray(b"\x00\xff"), [memoryview(b"abcd")[::2], None], None),
            (b"\x00\xff", [b"ac", None], None),
        ),
        "map_values": (
            "MAP(VARCHAR, VARBINARY)",
            {"first": memoryview(b"\x00\xff"), "empty": bytearray(), "null": None},
            {"first": b"\x00\xff", "empty": b"", "null": None},
        ),
        "map_keys": (
            "MAP(VARBINARY, VARCHAR)",
            {memoryview(b"\x00\xff"): "first", b"": None},
            {b"\x00\xff": "first", b"": None},
        ),
    }[kind]
    original_leaves = list(leaves(value))
    binary_snapshots = [
        (item, bytes(item))
        for item in original_leaves
        if isinstance(item, (bytes, bytearray, memoryview))
    ]

    connection = insert_value(value, native)

    statement, parameters, rendered = connection.statements[0]
    assert f"CAST(? AS {native})" in statement
    assert parameters == [expected]
    assert rendered == [render_parameter(expected)]
    for actual, target in zip(leaves(parameters[0]), leaves(expected)):
        if isinstance(target, bytes):
            assert type(actual) is bytes
    current_leaves = list(leaves(value))
    assert len(current_leaves) == len(original_leaves)
    assert all(before is after for before, after in zip(original_leaves, current_leaves))
    assert all(bytes(item) == snapshot for item, snapshot in binary_snapshots)
    assert connection.cursor_close_calls == 1
