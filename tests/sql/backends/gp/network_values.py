"""Greenplum INET certification follows the PostgreSQL 9.4 native input grammar."""

from __future__ import annotations

import datetime as dt
import socket
from enum import Enum

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.gp.network_values import (
    convert_network_value,
    is_network_type,
    key_validation_frames,
    network_value_identity,
    validate_network_profile,
)
from analytics_toolkit.sql.backends.gp.type_identity import canonical_builtin_type
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)
from analytics_toolkit.sql.dml.load.load_df import _validate_dataframe_key_uniqueness
from psycopg2.extensions import adapt

from tests.sql._support.gp_arrays import AdaptedConnection

V4 = bytes.fromhex("c0000201")
MAPPED_V4 = bytes.fromhex("00000000000000000000ffffc0000201")
V6 = bytes.fromhex("20010db800000000000000000000000a")


def certify(value, native="INET"):
    profile = ColumnProfile(native_type=native, source_backend="gp", temporal_type=native)
    profile.add(value)
    validate_profile(profile, native, "gp")
    resolution = ResolvedColumn(native, target_backend="gp")
    validate_converted_rows([(value,)], [resolution], backend="gp")
    return resolution.convert(value)


class Address(Enum):
    HOST = "192.0.2.1"


@pytest.mark.parametrize(
    ("value", "family", "packed", "prefix"),
    [
        ("0.0.0.0", 4, bytes(4), 32),
        ("255.255.255.255/0", 4, b"\xff" * 4, 0),
        ("192.0.2.1", 4, V4, 32),
        ("192.0.2.1/32", 4, V4, 32),
        ("192.0.2.129/24", 4, bytes.fromhex("c0000281"), 24),
        ("192.000.002.001/00024", 4, V4, 24),
        ("192.0.2.1.", 4, V4, 32),
        ("192.0.2.1./32", 4, V4, 32),
        ("10/8", 4, bytes.fromhex("0a000000"), 8),
        ("10/15", 4, bytes.fromhex("0a000000"), 15),
        ("192.168/17", 4, bytes.fromhex("c0a80000"), 17),
        ("192.168.1/31", 4, bytes.fromhex("c0a80100"), 31),
        ("192./8", 4, bytes.fromhex("c0000000"), 8),
        ("::", 6, bytes(16), 128),
        ("::/0", 6, bytes(16), 0),
        ("2001:db8::a/64", 6, V6, 64),
        ("2001:DB8:0:0:0:0:0:A", 6, V6, 128),
        ("::ffff:192.0.2.1/120", 6, MAPPED_V4, 120),
        ("::ffff:192.0.2", 6, bytes.fromhex("00000000000000000000ffffc0000200"), 128),
        ("::ffff:192.0.2./120", 6, bytes.fromhex("00000000000000000000ffffc0000200"), 120),
        ("::.192", 6, bytes.fromhex("00000000000000000000000000c00000"), 128),
        ("::./0", 6, bytes(16), 0),
        ("1:2:3:4:5:6:7:8", 6, bytes.fromhex("00010002000300040005000600070008"), 128),
        ("1:2:3:4:5:6:7:8:/64", 6, bytes.fromhex("00010002000300040005000600070008"), 64),
        ("::1:2:3:4:5:6:7", 6, bytes.fromhex("00000001000200030004000500060007"), 128),
        ("1:2:3:4:5:6:7::", 6, bytes.fromhex("00010002000300040005000600070000"), 128),
    ],
)
def test_inet_values_preserve_primary_pg94_grammar_and_real_psycopg_wire(
    monkeypatch, value, family, packed, prefix
):
    # Golden bytes follow REL9_4_STABLE/src/backend/utils/adt/inet_net_pton.c:
    # inet_net_pton_ipv4 (250-323), getv4 (367-409), inet_cidr_pton_ipv6 (420-533).
    # In particular IPv4 uses bits / 8 (integer division); embedded-v4 empty
    # fields before dots or a prefix are native zero fields, not DNS lookups.
    def forbidden_lookup(*_args, **_kwargs):
        pytest.fail("Native INET certification attempted DNS resolution")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_lookup)
    converted = certify(value)
    assert converted is value
    assert network_value_identity(converted) == (family, packed, prefix)
    connection = AdaptedConnection()
    get_backend("gp").insert_rows_batch(
        connection,
        "sandbox.events",
        ["payload"],
        [(converted,)],
        target_column_types={"payload": "INET"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=2,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert adapt(value).getquoted() in connection.target.statements[0]
    assert connection.committed


@pytest.mark.parametrize(
    "value",
    [
        "",
        "example.test",
        " 192.0.2.1",
        "192.0.2.1 ",
        "192.0.2.1\x00",
        "192.0.2.999",
        "-1.0.0.1",
        "192..2.1",
        "1.2.3.4.5",
        "1.2.3.4..",
        "192.0.2.1/",
        "192.0.2.1/33",
        "192.0.2.1/-1",
        "192.0.2.1/1/2",
        "10",
        "10/16",
        "192.168/24",
        "192.168.1/32",
        "0xc0000201/32",
        "[2001:db8::a]",
        "fe80::1%0",
        "2001:db8::g",
        "12345::1",
        "1::2::3",
        ":1:2:3:4:5:6:7",
        "1:2:3:4:5:6:7",  # PG9.4 requires all 16 bytes when no :: compression is present.
        "1:2:3:4:5:6:7:",
        "1:2:3:4:5:6:7:8:9",
        "1:2:3:4:5:6:7::8",
        "2001:db8::a/129",
        "2001:db8::a/01",
        "2001:db8::a/",
        "::ffff:192.00.2.1",
        "::ffff:192.0.2.999",
        "::ffff:192.0.2.",
        "::ffff:192.0.2.1./120",
        "::ffff:192.0.2.1/0120",
        "::ffff:192.0.2.1/129",
        "::ffff:192.0.2.1.2",
        "\uff11\uff19\uff12.0.2.1",
        b"\xc0\x00\x02\x01",
        3221225985,
        True,
        dt.date(2026, 1, 1),
    ],
)
def test_inet_values_reject_incompatible_native_values_before_binding(value):
    with pytest.raises(ValueError, match="INET") as error:
        certify(value)
    assert str(value) not in str(error.value) or value == ""


@pytest.mark.parametrize(
    "native", ["INET", "inet", "pg_catalog.inet", '"inet"', '"pg_catalog"."inet"']
)
def test_inet_values_recognize_only_builtin_identity_and_preserve_enum(native):
    assert is_network_type(native)
    assert canonical_builtin_type(native) == "INET"
    assert certify(Address.HOST, native) is Address.HOST.value


@pytest.mark.parametrize(
    "native", ["app.inet", '"INET"', '"PG_CATALOG".inet', "cidr", "macaddr", "inetish"]
)
def test_inet_values_do_not_certify_unselected_domains_or_lookalikes(native):
    profile = ColumnProfile()
    profile.add("192.0.2.1")
    assert not is_network_type(native)
    assert not validate_network_profile(profile, native)
    with pytest.raises(ValueError, match="INET"):
        convert_network_value("192.0.2.1", native)
    with pytest.raises(ValueError, match="INET"):
        network_value_identity("192.0.2.1", native)


@pytest.mark.parametrize("value", [None, pd.NA, float("nan")])
def test_inet_values_keep_sql_null_without_stringification(value):
    assert convert_network_value(value, "INET") is None
    assert certify(value) is None


def test_inet_values_empty_profile_and_authoritative_array_leaf_conversion():
    assert validate_network_profile(ColumnProfile(), "INET")
    for native in ["inet[]", "pg_catalog._inet", '"pg_catalog"."inet"[][]']:
        assert canonical_builtin_type(native) == "INET" + (
            "[][]" if native.endswith("[][]") else "[]"
        )
    value = [["192.0.2.129/24", None], ["2001:db8::a/64", "192.0.2.1"]]
    assert certify(value, "INET[][]") == value
    with pytest.raises(ValueError, match="INET"):
        certify([["192.0.2.1", "192.0.2.1/33"]], "INET[][]")
    with pytest.raises(ValueError, match="Invalid parameters"):
        certify(None, "INET(4)")


@pytest.mark.parametrize(
    ("values", "months", "duplicate"),
    [
        (["192.0.2.1", "192.0.2.1/32"], ["Jan", "Jan"], True),
        (["192.000.002.001", "192.0.2.1/32"], ["Jan", "Jan"], True),
        (["2001:DB8:0:0:0:0:0:A/64", "2001:db8::a/64"], ["Jan", "Jan"], True),
        (["192.0.2.1/24", "192.0.2.1/32"], ["Jan", "Jan"], False),
        (["192.0.2.1", "::ffff:192.0.2.1"], ["Jan", "Jan"], False),
        (["192.0.2.1", "192.0.2.1/32"], ["Jan", "Feb"], False),
    ],
)
def test_inet_values_native_key_views_retain_partition_mask_and_family(values, months, duplicate):
    frame = pd.DataFrame({"addr": pd.Series(values, dtype="string"), "month": months})
    original = frame.copy(deep=True)
    schema = {"addr": "INET", "month": "VARCHAR"}
    views = list(key_validation_frames(frame, ["month", "addr"], [None, schema, schema]))
    assert len(views) == 1
    assert views[0]["addr"].dtype == object
    assert list(views[0]) == ["month", "addr"]
    if duplicate:
        with pytest.raises(ValueError, match=r"Duplicate.*key"):
            _validate_dataframe_key_uniqueness(views[0], ["month", "addr"])
    else:
        _validate_dataframe_key_uniqueness(views[0], ["month", "addr"])
    pd.testing.assert_frame_equal(frame, original)
    assert list(key_validation_frames(frame, None, [schema])) == []
    assert list(key_validation_frames(frame, ["addr"], [{"addr": "VARCHAR"}])) == []


@pytest.mark.parametrize(
    ("native", "clocks", "duplicate"),
    [
        (
            "TIME(6)",
            [
                dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                dt.time(9, tzinfo=dt.timezone.utc),
            ],
            False,
        ),
        (
            "TIME(6)",
            [
                dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                dt.time(12, tzinfo=dt.timezone.utc),
            ],
            True,
        ),
        (
            "TIMETZ(6)",
            [
                dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                dt.time(9, tzinfo=dt.timezone.utc),
            ],
            False,
        ),
        (
            "TIMETZ(6)",
            [dt.time(12, tzinfo=dt.timezone.utc), dt.time(12, tzinfo=dt.timezone.utc)],
            True,
        ),
        ("TIMETZ(6)", [dt.time(12), dt.time(9)], False),
        ("TIMETZ(6)", [dt.time(12), dt.time(12)], True),
        ("TIME(0)", [dt.time(12, 0, 0, 100000), dt.time(12, 0, 0, 400000)], True),
    ],
)
def test_inet_values_composite_keys_use_native_gp_clock_semantics(native, clocks, duplicate):
    # PG9.4 date.c:2017-2045: TIMETZ equality requires both time and zone parts.
    frame = pd.DataFrame({"addr": ["192.0.2.1", "192.0.2.1/32"], "clock": clocks})
    original = frame.copy(deep=True)
    schemas = [{"addr": "INET", "clock": native}]
    view = next(key_validation_frames(frame, ["addr", "clock"], schemas))
    assert bool(view.duplicated().any()) == duplicate
    assert view["clock"].dtype == object
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("native", ["TIMETZ", '"pg_catalog"."timetz"(6)'])
def test_inet_values_mixed_awareness_timetz_keys_require_explicit_offsets(native):
    frame = pd.DataFrame(
        {
            "addr": ["192.0.2.1", "192.0.2.1/32"],
            "clock": [dt.time(12), dt.time(12, tzinfo=dt.timezone.utc)],
        }
    )
    original = frame.copy(deep=True)
    with pytest.raises(ValueError, match=r"Mixed.*TIMETZ.*explicit offset"):
        list(key_validation_frames(frame, ["addr", "clock"], [{"addr": "INET", "clock": native}]))
    pd.testing.assert_frame_equal(frame, original)
    # Ordinary non-INET keys retain the existing policy. Plain TIME has a
    # complete wall-time identity even when the source values mix awareness.
    assert (
        list(
            key_validation_frames(frame, ["addr", "clock"], [{"addr": "VARCHAR", "clock": native}])
        )
        == []
    )
    wall_view = next(
        key_validation_frames(frame, ["addr", "clock"], [{"addr": "INET", "clock": "TIME(6)"}])
    )
    assert wall_view.duplicated().any()


def test_inet_values_validate_both_authoritative_clock_schemas_and_other_composite_keys():
    frame = pd.DataFrame(
        {
            "addr": ["192.0.2.1", "192.0.2.1/32"],
            "clock": [dt.time(12, 0, 0, 1), dt.time(12, 0, 0, 2)],
            "id": [2**63 - 1, 2**63 - 1],
        }
    )
    schemas = [
        {"addr": "INET", "clock": "TIME(6)", "id": "BIGINT"},
        {"addr": "INET", "clock": "TIME(0)", "id": "BIGINT"},
    ]
    views = list(key_validation_frames(frame, ["addr", "clock", "id"], schemas))
    assert len(views) == 2
    assert not views[0].duplicated().any()
    assert views[1].duplicated().any()
    assert list(views[0]["id"]) == [2**63 - 1, 2**63 - 1]
