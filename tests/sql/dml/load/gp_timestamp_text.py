"""Public GP timestamp text preserves native assignment before destructive writes."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from psycopg2.extensions import adapt

from tests.sql._support.gp_timestamp_text import timestamp_target


def _load(frame, *, explicit, native_type):
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        table_schema={"payload": native_type} if explicit else None,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("native_type", ["TIMESTAMP", "TIMESTAMP(6)"])
@pytest.mark.parametrize(
    "values",
    [
        ["2025-01-02 03:04:05.123456", None, "2024-02-29 23:59:59.000001"],
        [dt.datetime.fromisoformat("2025-01-02T03:04:05.123456"), None],
    ],
    ids=["native-text", "native-datetime-control"],
)
def test_gp_timestamp_text_preserves_native_assignment_before_live_write(
    monkeypatch, explicit, native_type, values
):
    database = timestamp_target(monkeypatch, native_type)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    expected = [
        dt.datetime.fromisoformat(value) if isinstance(value, str) else value for value in values
    ]

    result = _load(frame, explicit=explicit, native_type=native_type)

    assert type(result) is int
    assert result == len(values)
    assert database.rows == [{"payload": value} for value in expected]
    assert database.schema == {"payload": native_type}
    assert [kind for kind, _statement in database.submissions] == ["CLEAR", "INSERT"]
    assert database.formatter_observations
    for template, bound, wire in database.formatter_observations:
        encoded_template = template.encode("utf-8") if isinstance(template, str) else template
        assert wire == encoded_template % tuple(adapt(value).getquoted() for value in bound)
        for value in bound:
            assert value is None or isinstance(value, (str, dt.datetime))
            if isinstance(value, dt.datetime):
                assert value.tzinfo is None
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "bad",
    ["private-invalid-timestamp-payload", "2025-02-30 03:04:05", "999999999-01-02 03:04:05"],
    ids=["private-malformed", "invalid-day", "out-of-range"],
)
def test_gp_timestamp_invalid_late_text_keeps_live_target_unchanged(monkeypatch, explicit, bad):
    database = timestamp_target(monkeypatch, "TIMESTAMP")
    before_rows = deepcopy(database.rows)
    before_schema = deepcopy(database.schema)
    frame = pd.DataFrame({"payload": pd.Series(["2025-01-02 03:04:05.123456", bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match=r"[Tt]imestamp|native.*assignment") as error:
        _load(frame, explicit=explicit, native_type="TIMESTAMP")

    assert bad not in str(error.value)
    assert database.rows == before_rows
    assert database.schema == before_schema
    assert database.live_mutations == []
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)


def test_gp_requested_timestamp_rounding_can_carry_into_exact_existing_precision(monkeypatch):
    database = timestamp_target(monkeypatch, "TIMESTAMP(0)")
    original = dt.datetime.fromisoformat("2025-01-02T03:04:05.999999")
    expected = dt.datetime.fromisoformat("2025-01-02T03:04:06.000000")
    frame = pd.DataFrame({"payload": pd.Series([original, None], dtype=object)})

    assert _load(frame, explicit=True, native_type="TIMESTAMP(3)") == 2

    assert database.target.rows == [{"payload": expected}, {"payload": None}]
    assert database.target.schema == {"payload": "TIMESTAMP(0)"}
    assert database.stage_inserts[0][1] == [(expected,), (None,)]
    assert frame.iloc[0, 0] is original


def test_gp_requested_timestamp_precision_does_not_authorize_additional_final_loss(monkeypatch):
    database = timestamp_target(monkeypatch, "TIMESTAMP(0)")
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame(
        {
            "payload": pd.Series(
                [dt.datetime.fromisoformat("2025-01-02T03:04:05.001499")], dtype=object
            )
        }
    )

    with pytest.raises(ValueError, match=r"precision|exact|native.*assignment"):
        _load(frame, explicit=True, native_type="TIMESTAMP(3)")

    assert database.target.rows == original_rows
    assert database.live_mutations == []


def test_gp_existing_timestamp_precision_remains_strict_without_explicit_conversion(monkeypatch):
    database = timestamp_target(monkeypatch, "TIMESTAMP(3)")
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame(
        {
            "payload": pd.Series(
                [dt.datetime.fromisoformat("2025-01-02T03:04:05.123500")], dtype=object
            )
        }
    )

    with pytest.raises(ValueError, match="precision"):
        _load(frame, explicit=False, native_type="TIMESTAMP(3)")

    assert database.target.rows == original_rows
    assert database.live_mutations == []
