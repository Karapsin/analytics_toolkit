"""Network preflight preserves native bounds, private values and untyped keys."""

from __future__ import annotations

from ipaddress import IPv4Address

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.network_values import (
    convert_network_value,
    key_validation_frames,
)
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn, validate_profile
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

from tests.sql.backends.ch._support.network_transport import encode_normalized

_PRIVATE = "private-selected-network-value"


@pytest.mark.parametrize(
    "native", ["nullable()", "nullable(IPv4, IPv6)", "lowcardinality(IPv4, IPv6)"]
)
def test_network_contract_casefolded_malformed_wrappers_reject_before_values_escape(native):
    with pytest.raises(ValueError, match="wrapper") as error:
        validate_converted_rows(
            [(_PRIVATE,)], [ResolvedColumn(native, target_backend="ch")], backend="ch"
        )
    assert _PRIVATE not in str(error.value)


@pytest.mark.parametrize("native", ["String", "Map(IPv4, String)", "IPv40"])
def test_network_contract_wrong_family_is_rejected_without_private_value_text(native):
    with pytest.raises(ValueError, match="authoritative ClickHouse") as error:
        convert_network_value(_PRIVATE, native)
    assert _PRIVATE not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("native", "source_type", "bits"), [("IPv4", "UInt64", 32), ("IPv6", "Int256", 128)]
)
@pytest.mark.parametrize("overflow", [False, True])
def test_network_contract_complete_wide_source_profiles_enforce_native_address_bounds(
    native, source_type, bits, overflow
):
    maximum = 2**bits - 1
    selected = [0, maximum + int(overflow)]
    profile = ColumnProfile(native_type=source_type, source_backend="ch")
    for value in selected:
        profile.add(value)
    assert profile.native_constraints_valid
    if overflow:
        with pytest.raises(ValueError, match="authoritative ClickHouse"):
            validate_profile(profile, native, "ch")
    else:
        validate_profile(profile, native, "ch")
        resolution = ResolvedColumn(
            native, target_backend="ch", source_backend="ch", source_type=source_type
        )
        converted = [resolution.convert(value) for value in selected]
        byte_order = "little" if native == "IPv4" else "big"
        expected = bytes(bits // 8) + maximum.to_bytes(bits // 8, byte_order)
        for transport in ("native", "http"):
            assert encode_normalized(native, converted, transport) == expected
    assert profile.count == 2
    assert profile.maximum_integer == selected[-1]


def test_network_contract_partial_key_schema_keeps_untyped_partition_and_large_ids():
    identifier = 2**100 + 1
    frame = pd.DataFrame(
        {
            "month": ["Jan ", "Feb", "Jan "],
            "ip": ["192.0.2.1", IPv4Address("192.0.2.1"), "192.0.2.1"],
            "id": pd.Series([identifier] * 3, dtype=object),
        }
    )
    original = frame.copy(deep=True)
    schema = {"ip": "IPv4"}
    (view,) = key_validation_frames(frame, ["month", "ip", "id"], [schema])
    assert view.month.tolist() == ["Jan ", "Feb", "Jan "]
    assert view.id.tolist() == [identifier] * 3
    assert view.duplicated(["month", "ip", "id"], keep=False).tolist() == [True, False, True]
    assert schema == {"ip": "IPv4"}
    pd.testing.assert_frame_equal(frame, original)
