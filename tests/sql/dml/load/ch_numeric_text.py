"""Finite numeric text reaches real ClickHouse native values before publication."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.numeric_text import ClickHouseNumericTextValueError

from tests.sql._support.ch_numeric_text import native_numeric_target

_EXACT = "12345678901234567890.123456789"
_TYPES = ["Nullable(Float32)", "Nullable(Float64)", "Nullable(Decimal(38,9))"]
_INVALID = [
    (native, bad) for native in _TYPES for bad in ["private-invalid-numeric", "NaN", "Infinity"]
] + [
    (_TYPES[0], "1E100"),
    (_TYPES[1], "1E1000"),
    (_TYPES[2], "1E29"),
    (_TYPES[2], "0.0000000001"),
]


def _load(frame, native, *, explicit):
    return sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **({"table_schema": {"payload": native}} if explicit else {}),
    )


def _expected_wire(native):
    mask = b"\x01\x00\x00\x01"
    if "Float32" in native:
        return mask + bytes(4) + bytes.fromhex("0000a03f000020c0") + bytes(4)
    if "Float64" in native:
        return mask + bytes(8) + bytes.fromhex("000000000000f43f00000000000004c0") + bytes(8)
    coefficient = 12345678901234567890123456789
    return (
        mask
        + bytes(16)
        + coefficient.to_bytes(16, "little", signed=True)
        + (-1).to_bytes(16, "little", signed=True)
        + bytes(16)
    )


def _assert_closed(store):
    assert store.opened
    assert all(connection.closed for connection in store.opened)
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"], ids=["http-legacy", "native-typed-path"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("native", _TYPES)
def test_ch_numeric_text_public_write_retains_exact_native_values_and_bytes(
    monkeypatch, transport, explicit, native
):
    # Decimal text worked on both base transports. Float text worked through
    # base HTTP; native Float exercises the candidate's deliberate typed cast.
    decimal = "Decimal" in native
    if decimal:
        incoming = [None, _EXACT, "-0.000000001", None]
        expected = [None, Decimal(_EXACT), Decimal("-0.000000001"), None]
    else:
        incoming = [None, "1.25", "-2.5", None]
        expected = [None, 1.25, -2.5, None]
    store = native_numeric_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(incoming, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, native, explicit=explicit)

    assert type(result) is int
    assert result == 4
    assert store.schema == {"payload": native}
    assert store.rows == [{"payload": value} for value in expected]
    assert store.wire_columns == [("payload", native, _expected_wire(native))]
    assert len(store.bound_rows) == 4
    if decimal:
        assert all(isinstance(row[0], Decimal) for row in store.bound_rows if row[0] is not None)
        assert store.bound_rows[1][0] == Decimal(_EXACT)
    else:
        assert all(type(row[0]) is float for row in store.bound_rows if row[0] is not None)
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    assert store.create_calls == []
    _assert_closed(store)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("case", _INVALID)
def test_ch_numeric_text_invalid_last_value_rejects_before_any_native_write(
    monkeypatch, transport, explicit, case
):
    native, bad = case
    store = native_numeric_target(monkeypatch, transport, native)
    before = deepcopy(store.rows)
    frame = pd.DataFrame({"payload": pd.Series(["1.25", None, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(
        ClickHouseNumericTextValueError,
        match=(
            r"^ClickHouse numeric text requires a finite value within the declared "
            r"precision, scale and range\."
        ),
    ) as error:
        _load(frame, native, explicit=explicit)

    assert "private-invalid-numeric" not in str(error.value)
    assert store.rows == before
    assert store.schema == {"payload": native}
    assert store.submissions == store.create_calls == store.wire_columns == store.bound_rows == []
    _assert_closed(store)
    pd.testing.assert_frame_equal(frame, original)


def test_ch_numeric_text_without_authority_remains_string():
    values = pd.Series(["1.25", _EXACT], dtype=object)
    original = values.copy(deep=True)

    assert get_backend_adapter("ch").infer_dataframe_column_type(values) == "String"

    pd.testing.assert_series_equal(values, original)
