"""Native equality composes across every column of Trino's effective key."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_ipaddress_casts import native_ipaddress_key_target

_TARGET = "iceberg.sandbox.target"
_IPV4 = "192.0.2.1"
_MAPPED = "::ffff:192.0.2.1"
_STAGE_WITNESS = "Native IPADDRESS aliases reached SQL stage creation before key rejection"


def _target(monkeypatch, *, column_types=None):
    database = native_ipaddress_key_target(monkeypatch, partitioned=True)
    column_types = column_types or {"payload": "IPADDRESS", "month": "CHAR(2)"}
    monkeypatch.setattr(
        load_df_module, "get_table_column_types", lambda *_args, **_kwargs: dict(column_types)
    )
    monkeypatch.setattr(
        get_backend_adapter("trino"),
        "get_writable_table_column_types",
        lambda *_args, **_kwargs: dict(column_types),
    )
    return database


def _frame(addresses, months):
    index = pd.Index(["second-input", "first-input"], name="source-row")
    return pd.DataFrame(
        {
            "payload": pd.Series(addresses, index=index, dtype=object),
            "month": pd.Series(months, index=index, dtype=object),
        },
        index=index,
    )


def _load(frame, write_mode):
    keys = ["payload", "month"] if write_mode == "append" else ["payload"]
    options = {"upsert_partition_column": "month"} if write_mode == "upsert" else {}
    return sql.load_df(
        "trino",
        _TARGET,
        frame,
        write_mode=write_mode,
        key_columns=keys,
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _assert_untouched(database, frame, original):
    assert database.rows == []
    assert database.statements == []
    assert database.mutations == []
    pd.testing.assert_frame_equal(frame, original)
    assert frame.index is original.index
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("write_mode", ["append", "upsert"])
def test_trino_composite_effective_key_aliases_reject_before_stage(monkeypatch, write_mode):
    database = _target(monkeypatch)
    frame = _frame([_IPV4, _MAPPED], ["a", "a "])
    original = frame.copy(deep=True)
    original.index = frame.index
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate.*(?:native|IPADDRESS|key)"):
        _load(frame, write_mode)

    assert database.stage_attempts == []
    _assert_untouched(database, frame, original)
    assert frame.iloc[0, 0] is _IPV4
    assert frame.iloc[1, 0] is _MAPPED


@pytest.mark.parametrize("native", ["CHAR(2)", "CHARACTER(2)"])
def test_trino_native_key_equality_does_not_require_an_ip_component(monkeypatch, native):
    database = _target(monkeypatch, column_types={"payload": native, "month": native})
    frame = _frame(["a", "a "], ["z", "z"])
    original = frame.copy(deep=True)
    original.index = frame.index
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate.*(?:native|key)"):
        _load(frame, "append")

    assert database.stage_attempts == []
    _assert_untouched(database, frame, original)


def test_trino_plain_time_and_char_key_aliases_reject_before_stage(monkeypatch):
    database = _target(monkeypatch, column_types={"payload": "TIME(0)", "month": "CHAR(2)"})
    clocks = [
        dt.time(12, tzinfo=dt.timezone.utc),
        dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1))),
    ]
    frame = _frame(clocks, ["a", "a "])
    original = frame.copy(deep=True)
    original.index = frame.index
    assert clocks[0] != clocks[1]
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate.*(?:native|key)"):
        _load(frame, "append")

    assert database.stage_attempts == []
    _assert_untouched(database, frame, original)
    assert all(frame.iloc[index, 0] is clock for index, clock in enumerate(clocks))


@pytest.mark.parametrize("distinct", ["wall-clock", "label"])
def test_trino_distinct_plain_time_and_char_keys_reach_the_stage_boundary(monkeypatch, distinct):
    database = _target(monkeypatch, column_types={"payload": "TIME(0)", "month": "CHAR(2)"})
    clocks = [
        dt.time(12, tzinfo=dt.timezone.utc),
        dt.time(
            13 if distinct == "wall-clock" else 12,
            tzinfo=dt.timezone(dt.timedelta(hours=1)),
        ),
    ]
    labels = ["a", "a " if distinct == "wall-clock" else "b"]
    frame = _frame(clocks, labels)
    original = frame.copy(deep=True)
    original.index = frame.index

    with pytest.raises(AssertionError, match=_STAGE_WITNESS):
        _load(frame, "append")

    assert len(database.stage_attempts) == 1
    pd.testing.assert_frame_equal(database.stage_attempts[0], original)
    _assert_untouched(database, frame, original)
    assert all(frame.iloc[index, 0] is clock for index, clock in enumerate(clocks))


@pytest.mark.parametrize("write_mode", ["append", "upsert"])
@pytest.mark.parametrize(
    ("addresses", "months"),
    [([_IPV4, _MAPPED], ["a", "b"]), ([_IPV4, "192.0.2.2"], ["a", "a "])],
    ids=["distinct-char", "distinct-address"],
)
def test_trino_distinct_composite_effective_keys_reach_the_stage_boundary(
    monkeypatch, write_mode, addresses, months
):
    database = _target(monkeypatch)
    frame = _frame(addresses, months)
    original = frame.copy(deep=True)
    original.index = frame.index

    with pytest.raises(AssertionError, match=_STAGE_WITNESS):
        _load(frame, write_mode)

    assert len(database.stage_attempts) == 1
    pd.testing.assert_frame_equal(database.stage_attempts[0], original)
    _assert_untouched(database, frame, original)


def test_trino_nullable_upsert_partition_retains_the_existing_nonnull_key_policy(monkeypatch):
    database = _target(monkeypatch)
    frame = _frame([_IPV4, "192.0.2.2"], [None, None])
    original = frame.copy(deep=True)
    original.index = frame.index

    with pytest.raises(
        ValueError, match="Null key values found in DataFrame for key_columns: month"
    ):
        _load(frame, "upsert")

    assert database.stage_attempts == []
    assert frame["month"].tolist() == [None, None]
    _assert_untouched(database, frame, original)
