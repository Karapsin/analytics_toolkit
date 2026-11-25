"""Native DATE/TIME/INTERVAL text is certified before live replacement."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.native_scalars import ExactInterval

from tests.sql._support.gp_native_temporal_text import native_temporal_target

_CASES = [
    (
        "DATE",
        [" 2025-01-02 ", "January 8, 1999", "2025-01-02 03:04:05", "20250102", None],
        [dt.date(2025, 1, 2), dt.date(1999, 1, 8), dt.date(2025, 1, 2), dt.date(2025, 1, 2), None],
    ),
    (
        "TIME",
        ["12:34:56.123456", "00:00:00", None],
        [dt.time(12, 34, 56, 123456), dt.time(0), None],
    ),
    (
        "INTERVAL",
        ["1 month", "30 days", None],
        [ExactInterval(1, 0, 0), ExactInterval(0, 30, 0), None],
    ),
]


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(("native", "values", "expected"), _CASES, ids=["date", "time", "interval"])
def test_gp_native_temporal_text_uses_complete_owned_assignment(
    monkeypatch, explicit, native, values, expected
):
    database = native_temporal_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    result = sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        table_schema={"payload": native} if explicit else None,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == len(values)
    assert database.target.rows == [{"payload": value} for value in expected]
    assert database.target.schema == {"payload": native}
    assert database.stage_inserts
    assert database.stage_inserts[0][1] == [(value,) for value in expected]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    ("native", "values", "_expected"), _CASES, ids=["date", "time", "interval"]
)
def test_gp_native_temporal_late_invalid_input_preserves_live_target(
    monkeypatch, explicit, native, values, _expected
):
    database = native_temporal_target(monkeypatch, native)
    old_rows = deepcopy(database.target.rows)
    secret = "private-invalid-native-temporal"
    frame = pd.DataFrame({"payload": pd.Series([values[0], secret], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(
        ValueError, match=r"(?i)(native assignment|temporal|authoritative)"
    ) as error:
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            table_schema={"payload": native} if explicit else None,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert secret not in str(error.value)
    assert database.target.rows == old_rows
    assert database.live_mutations == []
    pd.testing.assert_frame_equal(frame, original)
