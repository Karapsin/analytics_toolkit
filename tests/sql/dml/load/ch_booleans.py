"""Authoritative ClickHouse Bool preserves accepted integer native-wire inputs."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.boolean_values import ClickHouseBooleanValueError

from tests.sql._support.ch_booleans import native_boolean_target
from tests.sql.backends.ch._support.network_transport import encode_normalized


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


def _assert_native_result(store, native, expected, wire, result):
    assert type(result) is int
    assert result == len(expected)
    assert store.schema == {"payload": native}
    assert store.rows == [{"payload": value} for value in expected]
    assert all(type(row["payload"]) is bool for row in store.rows if row["payload"] is not None)
    assert store.wire_columns == [("payload", native, wire)]
    assert len(store.bound_rows) == len(expected)
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    assert store.create_calls == []
    assert store.opened
    assert all(connection.closed for connection in store.opened)
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("nullable", [False, True], ids=["Bool", "Nullable-Bool"])
def test_ch_booleans_public_integer_zero_one_reaches_exact_native_bytes(
    monkeypatch, transport, explicit, nullable
):
    native = "Nullable(Bool)" if nullable else "Bool"
    incoming = [0, None, 1] if nullable else [0, 1]
    expected = [False, None, True] if nullable else [False, True]
    wire = b"\x00\x01\x00\x00\x00\x01" if nullable else b"\x00\x01"
    # These are the installed SDKs' ordinary Bool encoders, without toolkit
    # value conversion or an exact-temporal codec substituting another type.
    assert encode_normalized(native, incoming, transport) == wire
    store = native_boolean_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(incoming, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, native, explicit=explicit)

    _assert_native_result(store, native, expected, wire, result)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("kind", ["integer", "finite-double", "decimal"])
def test_ch_booleans_public_numeric_nonzero_matches_actual_default_encoders(
    monkeypatch, transport, explicit, kind
):
    if kind == "integer":
        incoming = [-2, 0, 2]
        expected = [True, False, True]
        wire = b"\x01\x00\x01"
    elif kind == "finite-double":
        incoming = [-0.0, 0.5, -0.5]
        expected = [False, True, True]
        wire = b"\x00\x01\x01"
    else:
        incoming = [Decimal(0), Decimal("-0"), Decimal("1E-1000"), Decimal("-2.5")]
        expected = [False, False, True, True]
        wire = b"\x00\x00\x01\x01"
    assert encode_normalized("Bool", incoming, transport) == wire
    store = native_boolean_target(monkeypatch, transport, "Bool")
    frame = pd.DataFrame({"payload": pd.Series(incoming, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, "Bool", explicit=explicit)

    _assert_native_result(store, "Bool", expected, wire, result)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("dtype", [object, "int64"])
def test_ch_boolean_integer_zero_one_inference_remains_integer(dtype):
    values = pd.Series([0, 1], dtype=dtype)
    original = values.copy(deep=True)

    assert get_backend_adapter("ch").infer_dataframe_column_type(values) == "Int64"

    pd.testing.assert_series_equal(values, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_ch_booleans_invalid_last_value_rejects_before_native_encoder_or_target_mutation(
    monkeypatch, transport, explicit
):
    native = "Nullable(Bool)"
    store = native_boolean_target(monkeypatch, transport, native)
    before = deepcopy(store.rows)
    secret = "private-invalid-ch-bool"
    frame = pd.DataFrame({"payload": pd.Series([0, 1, None, secret], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ClickHouseBooleanValueError, match="Bool") as error:
        _load(frame, native, explicit=explicit)

    assert type(error.value) is ClickHouseBooleanValueError
    assert secret not in str(error.value)
    assert store.rows == before
    assert store.schema == {"payload": native}
    assert store.submissions == store.create_calls == store.wire_columns == store.bound_rows == []
    assert store.opened
    assert all(connection.closed for connection in store.opened)
    assert all(connection.close_calls == 1 for connection in store.opened)
    pd.testing.assert_frame_equal(frame, original)
