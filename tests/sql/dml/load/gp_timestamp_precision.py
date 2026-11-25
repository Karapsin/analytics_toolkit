"""Explicit GP timestamp precision follows native rounding before publication."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_timestamp_precision import timestamp_precision_target


@pytest.mark.parametrize(
    ("physical", "declared", "incoming", "expected"),
    [
        (
            "TIMESTAMP(3)",
            "TIMESTAMP(3)",
            "2025-01-02T03:04:05.123789",
            "2025-01-02T03:04:05.124000",
        ),
        ("TIMESTAMP(0)", "TIMESTAMP(0)", "2025-12-31T23:59:59.600000", "2026-01-01T00:00:00"),
        (
            "TIMESTAMP(3)",
            "TIMESTAMP(3)",
            "1999-12-31T23:59:59.999500",
            "1999-12-31T23:59:59.999000",
        ),
        (
            "TIMESTAMP(3)",
            "TIMESTAMP(3)",
            "2000-01-01T00:00:00.000500",
            "2000-01-01T00:00:00.001000",
        ),
        (
            "TIMESTAMP(3)",
            "TIMESTAMP(3)",
            "1999-12-31T23:59:59.123500",
            "1999-12-31T23:59:59.123000",
        ),
        (
            "TIMESTAMP(3)",
            "TIMESTAMP(3)",
            "2000-01-01T00:00:00.123500",
            "2000-01-01T00:00:00.124000",
        ),
        (
            "TIMESTAMP(6)",
            "TIMESTAMP(3)",
            "2025-01-02T03:04:05.123789",
            "2025-01-02T03:04:05.124000",
        ),
        (
            "TIMESTAMP(3)",
            "TIMESTAMP(3)",
            "2025-01-02T03:04:05.123000",
            "2025-01-02T03:04:05.123000",
        ),
    ],
    ids=[
        "round-up",
        "year-carry",
        "pre-epoch-tie",
        "post-epoch-tie",
        "negative-tie",
        "positive-tie",
        "requested-before-final",
        "aligned-control",
    ],
)
def test_explicit_timestamp_precision_keeps_native_gp_rounding(
    monkeypatch, physical, declared, incoming, expected
):
    database = timestamp_precision_target(monkeypatch, physical)
    value = dt.datetime.fromisoformat(incoming)
    frame = pd.DataFrame({"payload": pd.Series([None, value], dtype=object)})
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            table_schema={"payload": declared},
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )

    assert database.rows == [{"payload": None}, {"payload": dt.datetime.fromisoformat(expected)}]
    assert database.schema == {"payload": physical}
    assert database.formatter_observations
    assert all(connection.close_calls == 1 for connection in database.opened)
    pd.testing.assert_frame_equal(frame, original)


def test_existing_timestamp_precision_still_rejects_loss_before_live_clear(monkeypatch):
    database = timestamp_precision_target(monkeypatch, "TIMESTAMP(3)")
    before = deepcopy(database.rows)
    value = dt.datetime.fromisoformat("2025-01-02T03:04:05.123789")
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match="timestamp precision"):
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert database.rows == before
    assert database.live_mutations == []
    assert frame.iloc[0, 0] is value
