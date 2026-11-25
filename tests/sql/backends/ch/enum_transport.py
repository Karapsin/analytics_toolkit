"""Scalar terminal values avoid first-value inference in native binary encoders."""

from __future__ import annotations

import struct
from enum import Enum
from ipaddress import IPv4Address

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.insert import normalize_typed_row
from analytics_toolkit.sql.backends.ch.network_values import key_validation_frames
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs

from tests.sql.backends.ch._support.network_transport import encode_normalized

_PairKey = Enum("_PairKey", {"PAIR": [1, 2]})


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("Enum8('zero' = 0, 'one' = 1, 'two' = 2)", b"\x01\x02\x00"),
        ("Enum16('zero' = 0, 'one' = 1, 'two' = 2)", struct.pack("<3h", 1, 2, 0)),
    ],
)
def test_ch_enum_transport_preserves_mixed_labels_codes_and_valid_zero(
    transport: str, spec: str, expected: bytes
) -> None:
    source = ["one", 2, 0]
    terminal = [
        normalize_typed_row(
            ["value"], (value,), {"value": spec}, native_transport=transport == "native"
        )[0]
        for value in source
    ]
    assert encode_normalized(spec, terminal, transport) == expected
    assert source == ["one", 2, 0]
    assert normalize_typed_row(["value"], ("one",), {"value": spec}) == ("one",)


@pytest.mark.parametrize("values", [["é", b"\xff", b"abcd"], [b"\xff", "é", b"abcd"]])
def test_ch_http_fixed_string_transport_preserves_exact_padded_bytes(
    values: list[str | bytes],
) -> None:
    original = list(values)
    terminal = [
        normalize_typed_row(
            ["value"], (value,), {"value": "FixedString(4)"}, native_transport=False
        )[0]
        for value in values
    ]
    expected = [
        (value.encode("utf-8") if isinstance(value, str) else value).ljust(4, b"\x00")
        for value in values
    ]
    assert terminal == expected
    assert encode_normalized("FixedString(4)", terminal, "http") == b"".join(expected)
    assert values == original
    assert normalize_typed_row(["value"], ("é",), {"value": "FixedString(4)"}) == ("é",)
    assert normalize_typed_row(
        ["value"], ("é",), {"value": "FixedString(4)"}, native_transport=True
    ) == ("é",)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_ch_tuple_map_keys_preserve_logical_enum_sequences(transport: str) -> None:
    source = {_PairKey.PAIR: "pair"}
    spec = "Map(Tuple(UInt8, UInt8), String)"
    (terminal,) = normalize_typed_row(
        ["value"], (source,), {"value": spec}, native_transport=transport == "native"
    )
    expected_values = [((1, 2), b"pair")] if transport == "http" else [((1, 2), "pair")]
    assert list(terminal.items()) == expected_values
    assert (
        encode_normalized(spec, [terminal], transport) == struct.pack("<QBB", 1, 1, 2) + b"\x04pair"
    )
    assert list(source.items()) == [(_PairKey.PAIR, "pair")]


@pytest.mark.parametrize("transport", ["http", "native"])
def test_ch_enum_map_keys_keep_every_native_duplicate(transport: str) -> None:
    source = {"one": "label", 1: "code"}
    spec = "Map(Enum8('zero' = 0, 'one' = 1, 'two' = 2), String)"
    logical = ResolvedColumn(spec, target_backend="ch").convert(source)
    assert isinstance(logical, OrderedMapPairs)
    assert list(logical.items()) == list(source.items())
    (terminal,) = normalize_typed_row(
        ["value"], (logical,), {"value": spec}, native_transport=transport == "native"
    )
    expected_values = (
        [(1, b"label"), (1, b"code")] if transport == "http" else [(1, "label"), (1, "code")]
    )
    assert list(terminal.items()) == expected_values
    expected = struct.pack("<QBB", 2, 1, 1) + b"\x05label\x04code"
    assert encode_normalized(spec, [terminal], transport) == expected
    assert list(source.items()) == [("one", "label"), (1, "code")]


@pytest.mark.parametrize(
    "spec",
    [
        "Enum8('zero' = 0, 'one' = 1, 'two' = 2)",
        "SimpleAggregateFunction(any, Enum8('zero' = 0, 'one' = 1, 'two' = 2))",
    ],
)
def test_ch_network_enum_composite_keys_follow_exact_native_codes(spec: str) -> None:
    frame = pd.DataFrame(
        {
            "ip": ["192.0.2.1", IPv4Address("192.0.2.1"), "192.0.2.1"],
            "kind": pd.Series(["one", 1, 2], dtype=object),
        }
    )
    original = frame.copy(deep=True)
    (view,) = key_validation_frames(frame, ["ip", "kind"], [{"ip": "IPv4", "kind": spec}])
    assert view.kind.tolist() == [1, 1, 2]
    assert view.duplicated(["ip", "kind"], keep=False).tolist() == [True, True, False]
    pd.testing.assert_frame_equal(frame, original)
