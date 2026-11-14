"""Authoritative IPADDRESS values retain existing native parameter casts."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.trino_binary_buffers import use_actual_formatter
from tests.sql._support.trino_ipaddress_casts import (
    native_ipaddress_key_target,
    native_ipaddress_target,
)


def _load(frame, **options):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_trino_ipaddress_casts_valid_ipv4_reaches_native_target(monkeypatch, explicit, sdk):
    use_actual_formatter(monkeypatch, sdk)
    database = native_ipaddress_target(monkeypatch)
    value = "192.0.2.1"
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    options = {"table_schema": {"payload": "IPADDRESS"}} if explicit else {}

    assert _load(frame, **options) == 1

    assert database.rows == [value]
    assert database.mutations == ["clear", "insert"]
    assert "CAST(? AS IPADDRESS)" in database.statements[-1][0]
    assert database.statements[-1][2] == ["'192.0.2.1'"]
    assert database.native_type == "IPADDRESS"
    assert frame.iloc[0, 0] is value
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    ("value", "wire", "stored"),
    [
        ("2001:0db8:0:0:0:0:0:1", "'2001:0db8:0:0:0:0:0:1'", "2001:db8::1"),
        ("::ffff:192.0.2.1", "'::ffff:192.0.2.1'", "192.0.2.1"),
        (b"\xc0\x00\x02\x01", "X'c0000201'", "192.0.2.1"),
        (
            bytes.fromhex("20010db8000000000000000000000001"),
            "X'20010db8000000000000000000000001'",
            "2001:db8::1",
        ),
        (None, "NULL", None),
    ],
    ids=["ipv6", "mapped-ipv6", "four-byte", "sixteen-byte", "sql-null"],
)
def test_trino_ipaddress_casts_native_addresses_keep_exact_cast_identity(
    monkeypatch, sdk, value, wire, stored
):
    use_actual_formatter(monkeypatch, sdk)
    database = native_ipaddress_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert _load(frame, table_schema={"payload": "IPADDRESS"}) == 1

    assert database.rows == [stored]
    assert database.mutations == ["clear", "insert"]
    assert "CAST(? AS IPADDRESS)" in database.statements[-1][0]
    assert database.statements[-1][2] == [wire]
    assert database.native_type == "IPADDRESS"
    assert frame.iloc[0, 0] is value
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "value",
    [
        "db.example.test",
        "192.000.2.1",
        " 192.0.2.1",
        "[2001:db8::1]",
        "192.0.2.1/24",
        "2001:db8:::1",
        b"\xc0\x00\x02",
        42,
    ],
    ids=["dns", "leading-zero", "whitespace", "brackets", "cidr", "ipv6", "length", "integer"],
)
def test_trino_ipaddress_casts_invalid_values_fail_before_target_clear(
    monkeypatch, explicit, value
):
    database = native_ipaddress_target(monkeypatch)
    original_rows = list(database.rows)
    options = {"table_schema": {"payload": "IPADDRESS"}} if explicit else {}
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match=r"IPADDRESS|ipaddress|native|cast"):
        _load(frame, **options)

    assert database.rows == original_rows
    assert database.statements == []
    assert database.mutations == []
    assert frame.iloc[0, 0] is value
    assert database.native_type == "IPADDRESS"
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("write_mode", ["append", "upsert"])
@pytest.mark.parametrize(
    "values",
    [
        ["192.0.2.1", "::ffff:192.0.2.1"],
        [b"\xc0\x00\x02\x01", bytes.fromhex("00000000000000000000ffffc0000201")],
    ],
    ids=["text-ipv4-mapped", "packed-ipv4-mapped"],
)
def test_trino_ipaddress_casts_native_key_aliases_fail_before_stage(
    monkeypatch, write_mode, values
):
    partitioned = write_mode == "upsert"
    database = native_ipaddress_key_target(monkeypatch, partitioned=partitioned)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    options = {}
    if partitioned:
        frame["month"] = ["January", "January"]
        options["upsert_partition_column"] = "month"
    original = frame.copy(deep=True)
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate.*(?:native|IPADDRESS|key)"):
        sql.load_df(
            "trino",
            "iceberg.sandbox.target",
            frame,
            write_mode=write_mode,
            key_columns="payload",
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )

    assert database.rows == []
    assert database.stage_attempts == []
    assert database.statements == []
    assert database.mutations == []
    assert database.native_type == "IPADDRESS"
    pd.testing.assert_frame_equal(frame, original)
    assert all(frame.iloc[index, 0] is value for index, value in enumerate(values))
    assert all(connection.close_calls == 1 for connection in database.opened)
