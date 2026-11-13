"""Accepted NumPy scalars are transportable before destructive dataframe loads."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from enum import Enum

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError
from psycopg2 import ProgrammingError

from tests.sql._support.load_column_state import existing_column_target


class BooleanState(Enum):
    DISABLED = np.bool_(False)
    READY = np.bool_(True)


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


def test_numpy_boolean_integer_load_reaches_real_gp_adaptation_as_zero_and_one(monkeypatch):
    store = _target(monkeypatch, "gp", "BIGINT")
    frame = pd.DataFrame({"id": pd.Series([np.bool_(False), np.bool_(True), None], dtype=object)})
    original_frame = frame.copy(deep=True)
    failure = None
    try:
        result = _load("gp", frame)
    except (AmbiguousTableLoadError, ProgrammingError) as error:
        failure = error
        result = None

    assert store.rows == [{"id": 0}, {"id": 1}, {"id": None}]
    assert failure is None
    assert result == 3
    assert type(store.rows[0]["id"]) is int
    assert type(store.rows[1]["id"]) is int
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("producer", ["nullable-boolean", "enum-boolean"])
def test_numpy_boolean_producers_publish_integers_without_mutating_input(
    monkeypatch, backend, producer
):
    native_type = "Nullable(Int64)" if backend == "ch" else "BIGINT"
    store = _target(monkeypatch, backend, native_type)
    values = (
        pd.Series([False, True, pd.NA], dtype="boolean")
        if producer == "nullable-boolean"
        else pd.Series([BooleanState.DISABLED, BooleanState.READY, None], dtype=object)
    )
    frame = pd.DataFrame({"id": values})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 3

    assert store.rows == [{"id": 0}, {"id": 1}, {"id": None}]
    assert type(store.rows[0]["id"]) is int
    assert type(store.rows[1]["id"]) is int
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize(
    ("value", "expected", "native_types"),
    [
        (np.bool_(False), 0, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
        (np.bool_(True), 1, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
        (np.bool_(False), False, ["BOOLEAN", "BOOLEAN", "Nullable(Bool)"]),
        (np.bool_(True), True, ["BOOLEAN", "BOOLEAN", "Nullable(Bool)"]),
        (np.int64(2**53 + 1), 2**53 + 1, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
        (np.int8(-128), -128, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
        (np.uint32(2**32 - 1), 2**32 - 1, ["BIGINT", "BIGINT", "Nullable(Int64)"]),
        (
            np.uint64(2**64 - 1),
            {"gp": Decimal(2**64 - 1), "trino": Decimal(2**64 - 1), "ch": 2**64 - 1},
            ["NUMERIC(20,0)", "DECIMAL(20,0)", "Nullable(UInt64)"],
        ),
        (np.float32(1.25), 1.25, ["DOUBLE PRECISION", "DOUBLE", "Nullable(Float64)"]),
        (np.float64(np.pi), float(np.pi), ["DOUBLE PRECISION", "DOUBLE", "Nullable(Float64)"]),
        (np.str_("hé🙂"), "hé🙂", ["TEXT", "VARCHAR", "Nullable(String)"]),
        (np.bytes_(b"\x00\xff"), b"\x00\xff", ["BYTEA", "VARBINARY", "Nullable(String)"]),
        (
            Decimal("12345678901234567890.123456789"),
            Decimal("12345678901234567890.123456789"),
            ["NUMERIC(29,9)", "DECIMAL(29,9)", "Nullable(Decimal(29,9))"],
        ),
    ],
    ids=[
        "false-to-integer",
        "true-to-integer",
        "false-to-boolean",
        "true-to-boolean",
        "int64",
        "int8",
        "uint32",
        "uint64-maximum",
        "float32",
        "float64",
        "unicode",
        "bytes",
        "decimal-control",
    ],
)
def test_accepted_scalar_families_publish_exact_transport_values(
    monkeypatch, backend, value, expected, native_types
):
    native_type = native_types[["gp", "trino", "ch"].index(backend)]
    store = _target(monkeypatch, backend, native_type)
    frame = pd.DataFrame({"id": pd.Series([value, None], dtype=object)})
    original_frame = frame.copy(deep=True)
    expected_value = expected[backend] if isinstance(expected, dict) else expected

    assert _load(backend, frame) == 2

    assert store.rows == [{"id": expected_value}, {"id": None}]
    assert isinstance(store.rows[0]["id"], type(expected_value))
    if isinstance(expected_value, (int, bool, float)):
        assert type(store.rows[0]["id"]) is type(expected_value)
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("backend", "native_type"),
    [("gp", "BIGINT[]"), ("trino", "ARRAY(BIGINT)"), ("ch", "Array(Nullable(Int64))")],
)
def test_nested_numpy_boolean_array_leaves_publish_integer_zero_one_and_null(
    monkeypatch, backend, native_type
):
    store = _target(monkeypatch, backend, native_type)
    values = [np.bool_(False), None, np.bool_(True)]
    frame = pd.DataFrame({"id": pd.Series([values, []], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 2

    assert store.rows == [{"id": [0, None, 1]}, {"id": []}]
    assert type(store.rows[0]["id"][0]) is int
    assert type(store.rows[0]["id"][2]) is int
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("backend", "native_type"),
    [("trino", "MAP(BIGINT,BIGINT)"), ("ch", "Map(Int64,Int64)")],
)
def test_numpy_boolean_map_keys_and_values_reach_native_integer_identity(
    monkeypatch, backend, native_type
):
    store = _target(monkeypatch, backend, native_type)
    values = {np.bool_(False): np.bool_(False), np.bool_(True): np.bool_(True)}
    frame = pd.DataFrame({"id": pd.Series([values], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 1

    assert store.rows == [{"id": {0: 0, 1: 1}}]
    assert all(
        type(key) is int and type(value) is int for key, value in store.rows[0]["id"].items()
    )
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("backend", "native_type"),
    [("trino", "ROW(flag BIGINT, value BIGINT)"), ("ch", "Tuple(Int64,Int64)")],
)
def test_numpy_boolean_positional_row_leaves_publish_native_integers(
    monkeypatch, backend, native_type
):
    store = _target(monkeypatch, backend, native_type)
    frame = pd.DataFrame({"id": pd.Series([(np.bool_(False), np.bool_(True))], dtype=object)})

    assert _load(backend, frame) == 1

    assert store.rows == [{"id": (0, 1)}]
    assert all(type(value) is int for value in store.rows[0]["id"])


@pytest.mark.parametrize(
    ("backend", "native_type"),
    [("gp", "DOUBLE PRECISION"), ("trino", "DOUBLE"), ("ch", "Nullable(Float64)")],
)
def test_float_nan_and_none_keep_the_existing_sql_null_contract(monkeypatch, backend, native_type):
    store = _target(monkeypatch, backend, native_type)
    values = [np.float64(1.25), np.float64(np.nan), np.float32(np.nan), None]
    frame = pd.DataFrame({"id": pd.Series(values, dtype=object)})
    original_frame = frame.copy(deep=True)

    assert _load(backend, frame) == 4

    assert store.rows == [{"id": 1.25}, {"id": None}, {"id": None}, {"id": None}]
    assert type(store.rows[0]["id"]) is float
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_last_numpy_uint64_overflow_rejects_before_clearing_seed_rows(monkeypatch, backend):
    native_type = "Int64" if backend == "ch" else "BIGINT"
    store = _target(monkeypatch, backend, native_type)
    original_rows = deepcopy(store.rows)
    frame = pd.DataFrame({"id": pd.Series([np.int64(1), np.uint64(2**64 - 1)], dtype=object)})
    original_frame = frame.copy(deep=True)

    with pytest.raises(ValueError, match="authoritative integer schema"):
        _load(backend, frame)

    assert store.rows == original_rows
    assert store.submissions == []
    pd.testing.assert_frame_equal(frame, original_frame)
