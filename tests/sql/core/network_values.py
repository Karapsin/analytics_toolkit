from __future__ import annotations

import json
from ipaddress import IPv4Address, IPv6Address

import pytest
from analytics_toolkit.sql.core.exact_values import (
    OrderedMapPairs,
    decode_value,
    encode_value,
    typed_text,
)
from analytics_toolkit.sql.core.replay_spool import ReplaySpool

ADDRESSES = (
    IPv4Address(0),
    IPv4Address(2**32 - 1),
    IPv6Address(0),
    IPv6Address(2**128 - 1),
    IPv6Address("::ffff:192.0.2.1"),
    IPv6Address("fe80::1%interface-id"),
)


@pytest.mark.parametrize("address", ADDRESSES)
def test_address_family_survives_certified_binary_replay(address) -> None:
    rows = [(address,), (None,), (address,)]
    with ReplaySpool(["address"]) as spool:
        spool.write_rows(rows)
        spool.certify()
        for replay in (list(spool.iter_rows()), list(spool.iter_rows())):
            assert replay == rows
            assert type(replay[0][0]) is type(address)
            assert str(replay[0][0]) == str(address)
        assert spool.row_count == len(rows)
        path = spool.path
    assert not path.exists()


def test_native_addresses_retain_ordered_map_keys_and_nested_nulls() -> None:
    address = IPv4Address("192.0.2.1")
    value = OrderedMapPairs(
        (
            (address, [IPv6Address("2001:db8::1"), None]),
            ("192.0.2.1", (address,)),
            (address, None),
        )
    )
    encoded = json.loads(json.dumps(encode_value(value)))
    assert decode_value(encoded) == value
    fallback = json.loads(typed_text(value, source_backend="ch", source_type="Map(IPv4, IPv6)"))
    assert fallback["v"] == 1
    assert fallback["backend"] == "ch"
    assert fallback["type"] == "Map(IPv4, IPv6)"
    assert decode_value(fallback["value"]) == value


@pytest.mark.parametrize("address", ADDRESSES)
def test_native_address_text_is_canonical_and_family_preserving(address) -> None:
    assert typed_text(address) == str(address)
    result = decode_value(json.loads(json.dumps(encode_value(address))))
    assert type(result) is type(address)
    assert result == address


@pytest.mark.parametrize("payload", ["private-invalid-address", 1, None])
def test_invalid_exact_address_payload_has_no_private_value_in_error(payload) -> None:
    with pytest.raises(ValueError, match="Invalid exact IP address encoding") as caught:
        decode_value(["ip", payload])
    assert "private-invalid-address" not in str(caught.value)
