"""HTTP String leaves preserve Unicode, raw bytes and SQL NULL in one block."""

from __future__ import annotations

import struct
from copy import deepcopy
from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.ch.insert import normalize_typed_row
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

from tests.sql.backends.ch._support.network_transport import encode_normalized


@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        (
            "Array(Nullable(String))",
            [None, "Ω", b"\xff"],
            struct.pack("<Q", 3) + b"\x01\x00\x00\x00\x02\xce\xa9\x01\xff",
        ),
        (
            "SimpleAggregateFunction(any, Array(Nullable(String)))",
            [None, "Ω", b"\xff"],
            struct.pack("<Q", 3) + b"\x01\x00\x00\x00\x02\xce\xa9\x01\xff",
        ),
        (
            "Map(String, Nullable(String))",
            OrderedMapPairs((("Ω", b"\xff"), (b"\xfe", None))),
            struct.pack("<Q", 2) + b"\x02\xce\xa9\x01\xfe\x00\x01\x01\xff\x00",
        ),
        (
            "Nested(label String, payload Nullable(String))",
            [("Ω", b"\xff"), (b"\xfe", None)],
            struct.pack("<Q", 2) + b"\x02\xce\xa9\x01\xfe\x00\x01\x01\xff\x00",
        ),
    ],
)
def test_http_string_transport_nested_values_keep_exact_bytes_and_null_order(
    native, value, expected
):
    original = deepcopy(value)
    (terminal,) = normalize_typed_row(
        ["value"], (value,), {"value": native}, native_transport=False
    )
    assert encode_normalized(native, [terminal], "http") == expected
    assert value == original


@pytest.mark.parametrize("native", ["String", "Nullable(String)"])
def test_http_string_transport_preserves_buffers_and_exact_explicit_text(native):
    values = ["Ω", b"\xff", bytearray(b"\x00"), memoryview(b"\xfe"), 123, Decimal("1.20")]
    expected = [b"\xce\xa9", b"\xff", b"\x00", b"\xfe", b"123", b"1.20"]
    terminal = [
        normalize_typed_row(["value"], (value,), {"value": native}, native_transport=False)[0]
        for value in values
    ]
    assert terminal == expected
    payload = b"".join(bytes([len(value)]) + value for value in expected)
    prefix = bytes(len(values)) if native.startswith("Nullable") else b""
    assert encode_normalized(native, terminal, "http") == prefix + payload
    assert values[0] == "Ω"
    assert values[2] == bytearray(b"\x00")
    assert bytes(values[3]) == b"\xfe"
    assert values[-1] == Decimal("1.20")
    for transport in (None, True):
        assert normalize_typed_row(
            ["value"], ("Ω",), {"value": native}, native_transport=transport
        ) == ("Ω",)
