"""ClickHouse network destinations accept native values through both writers."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from ipaddress import IPv4Address, IPv6Address

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.ch_network_addresses import network_target


@pytest.mark.parametrize("mode", ["append", "truncate_insert"])
@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        ("IPv4", ["192.0.2.1", "203.0.113.10"], bytes.fromhex("010200c00a7100cb")),
        (
            "IPv6",
            ["2001:db8::1", "2001:db8::2"],
            bytes.fromhex("20010db800000000000000000000000120010db8000000000000000000000002"),
        ),
    ],
)
def test_ch_network_addresses_reach_real_driver_column_encoding(
    monkeypatch,
    transport,
    explicit,
    case,
    mode,
):
    native, values, expected_wire = case
    store = network_target(monkeypatch, transport, native)
    seed = deepcopy(store.rows)
    frame = pd.DataFrame({"address": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    options = {"table_schema": {"address": native}} if explicit else {}

    assert sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode=mode,
        retry_cnt=1,
        timeout_increment=0,
        **options,
    ) == len(values)

    assert store.schema == {"address": native}
    assert len(store.rows) == len(values) + (len(seed) if mode == "append" else 0)
    if mode == "append":
        assert store.rows[: len(seed)] == seed
    assert store.wire_columns == [("address", native, expected_wire)]
    assert [kind for kind, _detail in store.submissions] == (
        ["INSERT"] if mode == "append" else ["CLEAR", "INSERT"]
    )
    assert store.create_calls == []
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        (
            "IPv4",
            [IPv4Address(0), "192.0.2.1", 2**32 - 1, bytes.fromhex("c0000201"), None],
            [
                IPv4Address(0),
                IPv4Address("192.0.2.1"),
                IPv4Address(2**32 - 1),
                IPv4Address("192.0.2.1"),
                None,
            ],
            bytes.fromhex("00000000010200c0ffffffff010200c000000000"),
        ),
        (
            "IPv6",
            [
                IPv6Address("2001:db8::1"),
                "::",
                1,
                2**128 - 1,
                bytes.fromhex("20010db8000000000000000000000001"),
                IPv4Address("192.0.2.1"),
                bytes.fromhex("c0000201"),
                "192.0.2.1",
                IPv4Address(1),
                None,
            ],
            [
                IPv6Address("2001:db8::1"),
                IPv6Address("::"),
                IPv6Address("::1"),
                IPv6Address(2**128 - 1),
                IPv6Address("2001:db8::1"),
                IPv6Address("::ffff:192.0.2.1"),
                IPv6Address("::ffff:192.0.2.1"),
                IPv6Address("::ffff:192.0.2.1"),
                IPv6Address("::ffff:0.0.0.1"),
                None,
            ],
            None,
        ),
    ],
    ids=["ipv4-native-forms", "ipv6-native-forms"],
)
def test_ch_network_addresses_native_forms_keep_exact_family_width_and_sql_null(
    monkeypatch,
    transport,
    explicit,
    case,
):
    family, values, expected, body = case
    native = f"Nullable({family})"
    store = network_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"address": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    options = {"table_schema": {"address": native}} if explicit else {}
    if body is None:
        body = b"".join(value.packed if value is not None else bytes(16) for value in expected)
    expected_wire = bytes([0] * (len(values) - 1) + [1]) + body

    assert sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    ) == len(values)

    assert store.bound_rows == [(value,) for value in expected]
    assert store.rows == [{"address": value} for value in expected]
    assert store.wire_columns == [("address", native, expected_wire)]
    assert store.schema == {"address": native}
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("null_position", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.parametrize("family", ["IPv4", "IPv6"])
def test_ch_network_addresses_null_positions_are_preserved_by_real_nullable_codecs(
    monkeypatch,
    transport,
    null_position,
    family,
):
    address = IPv4Address("192.0.2.1") if family == "IPv4" else IPv6Address("2001:db8::1")
    values = [address, address]
    values.insert(null_position, None)
    native = f"Nullable({family})"
    store = network_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"address": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 3
    )

    expected_body = (
        [bytes.fromhex("010200c0"), bytes.fromhex("010200c0")]
        if family == "IPv4"
        else [address.packed, address.packed]
    )
    expected_body.insert(null_position, bytes(4 if family == "IPv4" else 16))
    assert store.wire_columns == [
        ("address", native, bytes(value is None for value in values) + b"".join(expected_body))
    ]
    assert store.bound_rows == [(value,) for value in values]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("existing", [True, False], ids=["before-clear", "before-create"])
@pytest.mark.parametrize(
    "case",
    [
        ("IPv4", "192.0.2.1", "999.0.2.1"),
        ("IPv4", "192.0.2.1", 2**32),
        ("IPv4", "192.0.2.1", bytes(3)),
        ("IPv4", "192.0.2.1", IPv6Address("::1")),
        ("IPv6", "2001:db8::1", "2001:::1"),
        ("IPv6", "2001:db8::1", 2**128),
        ("IPv6", "2001:db8::1", bytes(15)),
        ("IPv6", "2001:db8::1", "fe80::1%eth0"),
        ("IPv6", "2001:db8::1", 1.5),
    ],
)
def test_ch_network_addresses_invalid_last_value_rejects_before_live_clear_or_new_ddl(
    monkeypatch,
    transport,
    existing,
    case,
):
    native, good, bad = case
    store = network_target(monkeypatch, transport, native, exists=existing)
    seed = deepcopy(store.rows)
    frame = pd.DataFrame({"address": pd.Series([good, bad], dtype=object)})
    original = frame.copy(deep=True)
    options = {} if existing else {"table_schema": {"address": native}}

    with pytest.raises(ValueError, match=r"IPv[46]|network|native"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )

    assert store.rows == seed
    assert store.submissions == store.wire_columns == store.bound_rows == store.create_calls == []
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        ("IPv4", ["192.0.2.1", int(IPv4Address("192.0.2.1"))]),
        ("IPv6", ["2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0001"]),
        ("IPv6", ["::ffff:192.0.2.1", IPv4Address("192.0.2.1")]),
    ],
)
def test_ch_network_addresses_equivalent_native_keys_reject_before_append_stage_creation(
    monkeypatch,
    transport,
    explicit,
    case,
):
    native, values = case
    store = network_target(monkeypatch, transport, native)
    seed = deepcopy(store.rows)
    frame = pd.DataFrame({"address": pd.Series(values, dtype=object)})
    assert not frame.duplicated().any()
    options = {"table_schema": {"address": native}} if explicit else {}

    with pytest.raises(ValueError, match=r"[Dd]uplicate.*key"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="append",
            key_columns="address",
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )

    assert store.rows == seed
    assert store.submissions == store.wire_columns == store.bound_rows == store.create_calls == []


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("family", ["IPv4", "IPv6"])
def test_ch_network_addresses_nested_array_leaves_keep_native_bytes_and_nulls(
    monkeypatch,
    transport,
    family,
):
    address = IPv4Address("192.0.2.1") if family == "IPv4" else IPv6Address("2001:db8::1")
    native = f"Array(Nullable({family}))"
    values = [[str(address), None], [], [address]]
    store = network_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"address": pd.Series(values, dtype=object)})
    original = deepcopy(values)

    assert (
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 3
    )

    item = bytes.fromhex("010200c0") if family == "IPv4" else address.packed
    offsets = bytes.fromhex("020000000000000002000000000000000300000000000000")
    expected = offsets + b"\x00\x01\x00" + item + bytes(len(item)) + item
    assert store.wire_columns == [("address", native, expected)]
    assert store.bound_rows == [([address, None],), ([],), ([address],)]
    assert values == original


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("duplicate", [False, True], ids=["distinct-instants", "same-instant"])
def test_ch_network_address_timestamp_keys_share_the_frozen_destination_timezone(
    monkeypatch,
    transport,
    duplicate,
):
    store = network_target(monkeypatch, transport, "IPv6", clock_type="DateTime")
    seed = deepcopy(store.rows)
    clock = dt.datetime(1970, 1, 1, 12 if duplicate else 13, tzinfo=dt.timezone.utc).replace(
        tzinfo=None
    )
    naive = dt.datetime(1970, 1, 1, 12, tzinfo=dt.timezone.utc).replace(tzinfo=None)
    frame = pd.DataFrame(
        {
            "address": pd.Series(["2001:db8::1", IPv6Address("2001:db8::1")], dtype=object),
            "clock": pd.Series([naive, clock], dtype=object),
        }
    )
    original = frame.copy(deep=True)
    assert not frame.duplicated().any()
    options = {
        "write_mode": "append" if duplicate else "truncate_insert",
        "key_columns": ["address", "clock"],
        "retry_cnt": 1,
        "timeout_increment": 0,
    }

    if duplicate:
        with pytest.raises(ValueError, match=r"[Dd]uplicate.*key"):
            sql.load_df("ch", "sandbox.events", frame, **options)
        assert store.rows == seed
        assert (
            store.submissions == store.wire_columns == store.bound_rows == store.create_calls == []
        )
    else:
        assert sql.load_df("ch", "sandbox.events", frame, **options) == 2
        early = pd.Timestamp("1970-01-01 17:00:00+00:00")
        later = pd.Timestamp("1970-01-01 18:00:00+00:00")
        expected_clock = b"".join(
            (value.value // 10**9).to_bytes(4, "little", signed=False) for value in (early, later)
        )
        address = IPv6Address("2001:db8::1")
        assert store.wire_columns == [
            ("address", "IPv6", address.packed * 2),
            ("clock", "DateTime", expected_clock),
        ]
        assert store.bound_rows == [(address, early), (address, later)]
        assert store.rows == [
            {"address": address, "clock": early},
            {"address": address, "clock": later},
        ]
        assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    assert store.schema == {"address": "IPv6", "clock": "DateTime"}
    assert "SELECT timezone()" in store.metadata_queries
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize(
    "case",
    [
        ("IPv4", "ipv4"),
        ("IPv6", "IPV6"),
        ("Nullable(IPv4)", "nullable(IPv4)"),
        ("Nullable(IPv6)", "Nullable(ipv6)"),
    ],
)
def test_ch_network_addresses_declared_codec_spec_uses_effective_transport_schema(
    monkeypatch,
    transport,
    case,
):
    native, declared = case
    store = network_target(monkeypatch, transport, native)
    value = "2001:db8::1" if "IPv6" in native else "192.0.2.1"
    frame = pd.DataFrame({"address": [value]})
    original = frame.copy(deep=True)
    options = {
        "write_mode": "truncate_insert",
        "table_schema": {"address": declared},
        "retry_cnt": 1,
        "timeout_increment": 0,
    }
    assert sql.load_df("ch", "sandbox.events", frame, **options) == 1
    address = IPv6Address(value) if "IPv6" in native else IPv4Address(value)
    body = address.packed if "IPv6" in native else address.packed[::-1]
    expected_wire = (b"\x00" if native.startswith("Nullable(") else b"") + body
    assert store.rows == [{"address": address}]
    assert store.bound_rows == [(address,)]
    assert store.wire_columns == [("address", native, expected_wire)]
    assert store.normalization_types == ([(declared,)] if transport == "native" else [])
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    assert store.schema == {"address": native}
    assert store.create_calls == []
    pd.testing.assert_frame_equal(frame, original)
