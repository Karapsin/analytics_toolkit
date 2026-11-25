"""Native single precision rounds exact Decimal once at the actual writer."""

from __future__ import annotations

import struct
from decimal import Decimal, localcontext

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.ch_numeric_text import native_numeric_target as ch_target
from tests.sql._support.gp_numeric_text import numeric_text_target as gp_target
from tests.sql._support.trino_numeric_text import native_numeric_target as trino_target


def _above_binary32_midpoint(sign):
    with localcontext() as context:
        context.prec = 100
        return sign * (Decimal(1) + Decimal(2) ** -24 + Decimal(2) ** -80)


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize("explicit", [False, True])
def test_public_ch_decimal_float32_uses_single_exact_rounding(monkeypatch, transport, explicit):
    native = "Nullable(Float32)"
    store = ch_target(monkeypatch, transport, native)
    values = [_above_binary32_midpoint(1), None, _above_binary32_midpoint(-1)]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **({"table_schema": {"payload": native}} if explicit else {}),
        )
        == 3
    )

    assert store.wire_columns == [
        ("payload", native, b"\x00\x01\x00" + bytes.fromhex("0100803f00000000010080bf"))
    ]
    assert [
        None if row["payload"] is None else struct.pack("<f", row["payload"]) for row in store.rows
    ] == [bytes.fromhex("0100803f"), None, bytes.fromhex("010080bf")]
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("explicit", [False, True])
def test_public_trino_decimal_real_uses_single_exact_rounding(monkeypatch, explicit):
    database = trino_target(monkeypatch, "REAL")
    values = [_above_binary32_midpoint(1), None, _above_binary32_midpoint(-1)]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "trino",
            "iceberg.sandbox.target",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **({"table_schema": {"payload": "REAL"}} if explicit else {}),
        )
        == 3
    )

    assert database.physical_float_values == [bytes.fromhex("0100803f"), bytes.fromhex("010080bf")]
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True])
def test_public_gp_decimal_real_uses_single_exact_rounding(monkeypatch, explicit):
    database = gp_target(monkeypatch, "REAL")
    frame = pd.DataFrame(
        {
            "payload": pd.Series(
                [_above_binary32_midpoint(1), None, _above_binary32_midpoint(-1)], dtype=object
            )
        }
    )
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **({"table_schema": {"payload": "REAL"}} if explicit else {}),
        )
        == 3
    )

    assert [
        None if row["payload"] is None else struct.pack("<f", row["payload"])
        for row in database.rows
    ] == [bytes.fromhex("0100803f"), None, bytes.fromhex("010080bf")]
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)
