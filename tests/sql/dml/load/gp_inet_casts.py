"""Authoritative Greenplum INET cells retain native string input semantics."""

from __future__ import annotations

import datetime as dt
import ipaddress
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_inet_casts import native_inet_target


def _load(frame, **options):
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_inet_casts_valid_string_reaches_native_execute_values(monkeypatch, explicit):
    database = native_inet_target(monkeypatch)
    value = "192.0.2.1/24"
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    options = {"table_schema": {"payload": "INET"}} if explicit else {}

    assert _load(frame, **options) == 1

    assert database.target.rows == [{"payload": ipaddress.ip_interface(value)}]
    assert database.bound_rows == [(value,)]
    assert database.wire_rows == [("'192.0.2.1/24'",)]
    assert database.target.schema == {"payload": "INET"}
    assert len(database.live_mutations) == 2
    assert frame.iloc[0, 0] is value


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_inet_casts_preserve_nullable_host_bits_prefix_and_address_family(monkeypatch, explicit):
    database = native_inet_target(monkeypatch)
    values = [
        None,
        "192.0.2.1",
        "192.0.2.129/24",
        "2001:db8::a/64",
        "::ffff:192.0.2.1/120",
        None,
    ]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    options = {"table_schema": {"payload": "INET"}} if explicit else {}

    assert _load(frame, gp_insert_chunk_size=2, **options) == len(values)

    assert database.target.rows == [
        {"payload": ipaddress.ip_interface(value) if value is not None else None}
        for value in values
    ]
    stored_host = database.target.rows[2]["payload"]
    assert (stored_host.ip.exploded, stored_host.network.prefixlen) == ("192.0.2.129", 24)
    mapped = database.target.rows[4]["payload"]
    assert (mapped.version, mapped.network.prefixlen) == (6, 120)
    assert mapped.ip.ipv4_mapped == ipaddress.IPv4Address("192.0.2.1")
    assert database.bound_rows == [(value,) for value in values]
    assert database.wire_rows == [("NULL" if value is None else f"'{value}'",) for value in values]
    assert len(database.live_mutations) == 4
    assert database.target.schema == {"payload": "INET"}
    pd.testing.assert_frame_equal(frame, original)
    assert all(frame.iloc[index, 0] is value for index, value in enumerate(values))


def test_gp_inet_casts_mixed_naive_aware_timetz_keys_reject_before_live_write(monkeypatch):
    database = native_inet_target(monkeypatch, exists=False)
    values = ["192.0.2.1", "192.0.2.1/32"]
    clocks = [dt.time(12), dt.time(12, tzinfo=dt.timezone.utc)]
    frame = pd.DataFrame(
        {
            "payload": pd.Series(values, dtype=object),
            "clock": pd.Series(clocks, dtype=object),
        }
    )
    original = frame.copy(deep=True)
    assert not frame.duplicated().any()

    def load_mixed_keys():
        count = sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns=["payload", "clock"],
            table_schema={"payload": "INET", "clock": "TIMETZ"},
            retry_cnt=1,
            timeout_increment=0,
        )
        assert count == 2
        assert database.target.rows[0] == database.target.rows[1]
        assert database.target.rows[0]["clock"] == (43_200_000_000, 0)
        pytest.fail("Load accepted equal INET/TIMETZ keys under the UTC fixture session.")

    # Explicit TIMETZ delegates wall-clock assignment to this frozen UTC server
    # session. Its typed checkpoint, not a guessed local offset, detects the tie.
    with pytest.raises(ValueError, match=r"Duplicate native key"):
        load_mixed_keys()

    assert ("sandbox", "events") not in database.tables
    assert database.live_mutations == []
    assert database.bound_rows == [
        (values[0], "12:00:00.000000"),
        (values[1], "12:00:00.000000+00:00:00"),
    ]
    assert len(database.wire_rows) == 2
    assert database.tables == {}
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("aware", [False, True], ids=["uniform-naive", "aware-offset-tie"])
def test_gp_inet_casts_uniform_timetz_keys_keep_existing_native_identity(monkeypatch, aware):
    database = native_inet_target(monkeypatch, exists=False)
    clocks = (
        [
            dt.time(12, tzinfo=dt.timezone.utc),
            dt.time(13, tzinfo=dt.timezone(dt.timedelta(hours=1))),
        ]
        if aware
        else [dt.time(12), dt.time(13)]
    )
    values = ["192.0.2.1", "192.0.2.1/32"]
    frame = pd.DataFrame(
        {
            "payload": pd.Series(values, dtype=object),
            "clock": pd.Series(clocks, dtype=object),
        }
    )
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns=["payload", "clock"],
            table_schema={"payload": "INET", "clock": "TIMETZ"},
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )

    expected_clocks = [(43_200_000_000, 0), (46_800_000_000, 3600 if aware else 0)]
    assert database.target.rows == [
        {"payload": ipaddress.ip_interface(value), "clock": clock}
        for value, clock in zip(values, expected_clocks)
    ]
    assert database.target.rows[0] != database.target.rows[1]
    assert database.target.schema == {"payload": "INET", "clock": "TIMETZ"}
    transport = (
        ["12:00:00.000000+00:00:00", "13:00:00.000000+01:00:00"]
        if aware
        else ["12:00:00.000000", "13:00:00.000000"]
    )
    assert database.bound_rows == list(zip(values, transport))
    assert database.wire_rows == [
        (f"'{value}'", f"'{clock}'") for value, clock in zip(values, transport)
    ]
    pd.testing.assert_frame_equal(frame, original)
    assert all(frame.iloc[index, 1] is clock for index, clock in enumerate(clocks))


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_inet_casts_native_arrays_retain_real_psycopg_leaf_casts(monkeypatch, explicit):
    database = native_inet_target(monkeypatch, "INET[]")
    value = ["192.0.2.129/24", None, "2001:db8::a/64"]
    frame = pd.DataFrame({"payload": pd.Series([value, None], dtype=object)})
    original = deepcopy(value)
    options = {"table_schema": {"payload": "INET[]"}} if explicit else {}

    assert _load(frame, **options) == 2

    assert database.target.rows == [
        {
            "payload": [
                ipaddress.ip_interface("192.0.2.129/24"),
                None,
                ipaddress.ip_interface("2001:db8::a/64"),
            ]
        },
        {"payload": None},
    ]
    assert database.bound_rows == [(value,), (None,)]
    assert database.wire_rows == [
        ("CAST(ARRAY['192.0.2.129/24', NULL, '2001:db8::a/64'] AS INET[])",),
        ("CAST(NULL AS INET[])",),
    ]
    assert database.target.schema == {"payload": "INET[]"}
    assert len(database.live_mutations) == 2
    assert frame.iloc[0, 0] is value
    assert value == original


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_inet_casts_invalid_array_last_leaf_rejects_before_clear(monkeypatch, explicit):
    database = native_inet_target(monkeypatch, "INET[]")
    seed = deepcopy(database.target.rows)
    value = ["192.0.2.129/24", None, "192.0.2.1/33"]
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original = deepcopy(value)
    options = {"table_schema": {"payload": "INET[]"}} if explicit else {}

    with pytest.raises(ValueError, match=r"INET|inet|native|cast"):
        _load(frame, **options)

    assert database.target.rows == seed
    assert database.live_mutations == []
    assert database.bound_rows == []
    assert database.wire_rows == []
    assert database.target.schema == {"payload": "INET[]"}
    assert frame.iloc[0, 0] is value
    assert value == original


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "value",
    [
        "db.example.test",
        "192.0.2.999",
        "192.0.2.1/33",
        "2001:db8::1/129",
        "2001:db8:::1",
        "192.0.2.1/24/16",
        b"\xc0\x00\x02\x01",
        42,
    ],
    ids=["dns", "octet", "ipv4-mask", "ipv6-mask", "ipv6", "multiple-masks", "binary", "int"],
)
def test_gp_inet_casts_invalid_values_reject_before_seed_clear(monkeypatch, explicit, value):
    database = native_inet_target(monkeypatch)
    seed = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series(["192.0.2.1/24", value], dtype=object)})
    original = frame.copy(deep=True)
    options = {"table_schema": {"payload": "INET"}} if explicit else {}

    with pytest.raises(ValueError, match=r"INET|inet|native|cast"):
        _load(frame, **options)

    assert database.target.rows == seed
    assert database.live_mutations == []
    assert database.bound_rows == []
    assert database.wire_rows == []
    assert database.target.schema == {"payload": "INET"}
    pd.testing.assert_frame_equal(frame, original)
    assert frame.iloc[1, 0] is value


@pytest.mark.parametrize("write_mode", ["append", "upsert"])
def test_gp_inet_casts_native_key_aliases_reject_before_any_write(monkeypatch, write_mode):
    existing = write_mode == "append"
    database = native_inet_target(monkeypatch, exists=existing, empty=True)
    values = ["192.0.2.1", "192.0.2.1/32"]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    assert not frame.duplicated().any()

    def load_native_keys():
        count = sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode=write_mode,
            key_columns="payload",
            table_schema={"payload": "INET"},
            retry_cnt=1,
            timeout_increment=0,
        )
        assert count == 2
        assert database.target.rows == [
            {"payload": ipaddress.ip_interface(value)} for value in values
        ]
        assert database.target.rows[0] == database.target.rows[1]
        pytest.fail("Load accepted two equal native INET keys without duplicate validation.")

    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        load_native_keys()

    if existing:
        assert database.target.rows == []
    else:
        assert ("sandbox", "events") not in database.tables
    assert database.live_mutations == []
    if write_mode == "upsert":
        assert database.bound_rows == [(value,) for value in values]
        assert database.wire_rows == [(f"'{value}'",) for value in values]
        assert database.tables == {}
    else:
        assert database.bound_rows == []
        assert database.wire_rows == []
    pd.testing.assert_frame_equal(frame, original)
    assert all(frame.iloc[index, 0] is value for index, value in enumerate(values))


@pytest.mark.parametrize(
    ("values", "partitions"),
    [
        (["192.0.2.1/24", "192.0.2.1/32"], None),
        (["192.0.2.1", "::ffff:192.0.2.1"], None),
        (["192.0.2.1", "192.0.2.1/32"], ["January", "February"]),
    ],
    ids=["different-mask", "different-family", "different-partition"],
)
def test_gp_inet_casts_native_key_identity_preserves_mask_family_and_partition(
    monkeypatch, values, partitions
):
    database = native_inet_target(monkeypatch, exists=False)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    schema = {"payload": "INET"}
    options = {}
    if partitions is not None:
        frame["month"] = partitions
        schema["month"] = "TEXT"
        options["upsert_partition_column"] = "month"
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="upsert",
            key_columns="payload",
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )
        == 2
    )

    expected = [{"payload": ipaddress.ip_interface(value)} for value in values]
    if partitions is not None:
        for row, partition in zip(expected, partitions):
            row["month"] = partition
    assert database.target.rows == expected
    assert database.target.schema == schema
    assert database.bound_rows == list(frame.itertuples(index=False, name=None))
    pd.testing.assert_frame_equal(frame, original)
    assert all(frame.iloc[index, 0] is value for index, value in enumerate(values))
