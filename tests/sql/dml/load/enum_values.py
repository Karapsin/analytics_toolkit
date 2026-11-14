"""Enum values are converted before destructive dataframe loading."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal
from enum import Enum, IntEnum

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError
from psycopg2 import ProgrammingError

from tests.sql._support.load_column_state import existing_column_target


class NumericState(Enum):
    READY = 2**53 + 1
    OTHER = -(2**53 + 1)
    EMPTY = None


class ScalarValue(Enum):
    AMOUNT = Decimal("12345678901234567890.123456789")
    DAY = dt.date(1900, 1, 1)
    DISABLED = False


class IntegerState(IntEnum):
    READY = 2**53 + 1


class InvalidNumber(Enum):
    TOO_BIG = 2**63


class MapKey(Enum):
    READY = "ready"


def _load(backend, frame):
    return sql.load_df(
        backend,
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
    )


def _target(monkeypatch, backend, native_type):
    store = existing_column_target(
        monkeypatch,
        backend,
        real_metadata=backend != "trino",
        real_gp_adaptation=backend == "gp",
    )
    store.schema["id"] = native_type
    return store


def test_numeric_enum_values_reach_real_gp_adaptation_as_exact_primitives(monkeypatch):
    store = _target(monkeypatch, "gp", "BIGINT")
    frame = pd.DataFrame(
        {
            "id": pd.Series(
                [NumericState.READY, NumericState.EMPTY, NumericState.OTHER], dtype=object
            )
        }
    )
    original_frame = frame.copy(deep=True)
    failure = None
    try:
        result = _load("gp", frame)
    except (AmbiguousTableLoadError, ProgrammingError) as error:
        failure = error
        result = None

    assert store.rows == [{"id": 2**53 + 1}, {"id": None}, {"id": -(2**53 + 1)}]
    assert failure is None
    assert result == 3
    assert type(store.rows[0]["id"]) is int
    assert type(store.rows[2]["id"]) is int
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize(
    ("member", "expected", "native_types"),
    [
        (NumericState.READY, 2**53 + 1, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
        (NumericState.EMPTY, None, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
        (
            ScalarValue.AMOUNT,
            Decimal("12345678901234567890.123456789"),
            ["NUMERIC(29,9)", "DECIMAL(29,9)", "Nullable(Decimal(29,9))"],
        ),
        (ScalarValue.DAY, dt.date(1900, 1, 1), ["DATE", "DATE", "Nullable(Date32)"]),
        (ScalarValue.DISABLED, False, ["BOOLEAN", "BOOLEAN", "Nullable(Bool)"]),
        (IntegerState.READY, 2**53 + 1, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
    ],
    ids=["integer", "enum-null", "decimal", "date", "false", "int-enum"],
)
def test_scalar_enum_values_publish_exact_native_values(
    monkeypatch, backend, member, expected, native_types
):
    store = _target(monkeypatch, backend, native_types[["gp", "trino", "ch"].index(backend)])
    frame = pd.DataFrame({"id": pd.Series([member, None], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 2

    assert store.rows == [{"id": expected}, {"id": None}]
    assert type(store.rows[0]["id"]) is type(expected)
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("backend", "native_type"),
    [("gp", "BIGINT[]"), ("trino", "ARRAY(BIGINT)"), ("ch", "Array(Nullable(Int64))")],
)
def test_nested_array_enums_publish_native_integer_and_sql_null_leaves(
    monkeypatch, backend, native_type
):
    store = _target(monkeypatch, backend, native_type)
    frame = pd.DataFrame(
        {
            "id": pd.Series(
                [[NumericState.READY, NumericState.EMPTY, NumericState.OTHER]], dtype=object
            )
        }
    )
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 1

    assert store.rows == [{"id": [2**53 + 1, None, -(2**53 + 1)]}]
    assert type(store.rows[0]["id"][0]) is int
    assert type(store.rows[0]["id"][2]) is int
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("backend", "native_type"),
    [("trino", "MAP(VARCHAR,BIGINT)"), ("ch", "Map(String,Nullable(Int64))")],
)
def test_nested_map_enums_publish_logical_keys_and_native_values(monkeypatch, backend, native_type):
    store = _target(monkeypatch, backend, native_type)
    frame = pd.DataFrame(
        {
            "id": pd.Series(
                [{MapKey.READY: NumericState.READY, "empty": NumericState.EMPTY}], dtype=object
            )
        }
    )
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 1

    ready_key, empty_key = (b"ready", b"empty") if backend == "ch" else ("ready", "empty")
    assert store.rows == [{"id": {ready_key: 2**53 + 1, empty_key: None}}]
    assert type(store.rows[0]["id"][ready_key]) is int
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("nested", [False, True], ids=["scalar", "array-leaf"])
def test_out_of_range_enum_rejects_before_clearing_existing_rows(monkeypatch, backend, nested):
    native_type = {
        "gp": "BIGINT[]" if nested else "BIGINT",
        "trino": "ARRAY(BIGINT)" if nested else "BIGINT",
        "ch": "Array(Int64)" if nested else "Int64",
    }[backend]
    store = _target(monkeypatch, backend, native_type)
    store.rows = [{"id": [101]}] if nested else [{"id": 101}]
    original_rows = deepcopy(store.rows)
    values = [NumericState.READY, InvalidNumber.TOO_BIG]
    frame = pd.DataFrame({"id": pd.Series([values] if nested else values, dtype=object)})
    original_frame = frame.copy(deep=True)

    with pytest.raises(ValueError, match="authoritative integer schema"):
        _load(backend, frame)

    assert store.rows == original_rows
    assert store.submissions == []
    pd.testing.assert_frame_equal(frame, original_frame)


def test_enum_map_key_collision_rejects_before_clearing_existing_rows(monkeypatch):
    store = _target(monkeypatch, "trino", "MAP(VARCHAR,BIGINT)")
    store.rows = [{"id": {"seed": 101}}]
    original_rows = deepcopy(store.rows)
    frame = pd.DataFrame({"id": pd.Series([{MapKey.READY: 1, "ready": 2}], dtype=object)})

    with pytest.raises(ValueError, match="map"):
        _load("trino", frame)

    assert store.rows == original_rows
    assert store.submissions == []
