"""Inferred schemas retain the exact conversion that justified their selection."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from sqlglot import exp

from tests.sql._support.inferred_load_values import inferred_values_target


def test_public_inferred_decimal_load_keeps_the_exact_binary_float_value(monkeypatch):
    database = inferred_values_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series([2**53 + 1, 0.1], dtype=object)})
    original = frame.copy(deep=True)

    result = sql.load_df("gp", "sandbox.events", frame, retry_cnt=1, return_metadata=True)

    assert result.rows == 2
    assert exp_native(database.schema["payload"]) == exp_native("NUMERIC(71,55)")
    assert database.rows == [
        {"payload": Decimal(2**53 + 1)},
        {"payload": Decimal.from_float(0.1)},
    ]
    assert database.bound_rows == [(Decimal(2**53 + 1),), (Decimal.from_float(0.1),)]
    assert database.formatter_observations
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("exists", [False, True])
def test_public_authoritative_decimal_load_retains_deliberate_float_conversion(monkeypatch, exists):
    database = inferred_values_target(monkeypatch, exists=exists)
    frame = pd.DataFrame({"payload": pd.Series([0.1], dtype=object)})

    result = sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        table_schema=None if exists else {"payload": "NUMERIC(71,55)"},
        write_mode="truncate_insert",
        retry_cnt=1,
    )

    assert result == 1
    assert database.rows == [{"payload": Decimal("0.1")}]
    assert database.bound_rows == [(Decimal("0.1"),)]
    assert frame.iloc[0, 0] == 0.1


def test_public_inferred_text_load_returns_its_complete_fallback_provenance(monkeypatch):
    database = inferred_values_target(monkeypatch, native_type="TEXT")
    values = [{"nested": [1, b"\xff"]}, {"nested": [2]}]
    original = deepcopy(values)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})

    result = sql.load_df("gp", "sandbox.events", frame, retry_cnt=1, return_metadata=True)

    assert result.rows == 2
    assert database.schema == {"payload": "TEXT"}
    (resolution,) = result.metadata.type_resolutions
    assert resolution["reason"] == "no exact native representation"
    assert resolution["encoding_version"] == 1
    assert resolution["certified_input_count"] == 2
    assert all(isinstance(row["payload"], str) for row in database.rows)
    assert frame["payload"].tolist() == original


def exp_native(raw):
    return exp.DataType.build(raw, dialect="postgres")
