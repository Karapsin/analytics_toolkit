"""Authoritative ClickHouse integers retain historical native integer-text casts."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql._support.ch_integer_text import inferred_string_target, integer_target

_KINDS = [
    (f"{prefix}Int{bits}", bits, prefix == "")
    for bits in (8, 16, 32, 64, 128, 256)
    for prefix in ("", "U")
]
_WIDE_KINDS = [kind for kind in _KINDS if kind[1] in {128, 256}]


def _bounds(kind):
    _native, bits, signed = kind
    return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)


def _wire(values, kind, *, nullable=False):
    _native, bits, signed = kind
    body = b"".join(
        (0 if value is None else value).to_bytes(bits // 8, "little", signed=signed)
        for value in values
    )
    return bytes(value is None for value in values) + body if nullable else body


def _load(frame, native, *, explicit):
    options = {"table_schema": {"payload": native}} if explicit else {}
    return sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _assert_native_write(store, kind, native, values):
    assert store.schema == {"payload": native}
    assert store.bound_rows == [(value,) for value in values]
    assert all(
        row[0] is None or (isinstance(row[0], int) and not isinstance(row[0], bool))
        for row in store.bound_rows
    )
    assert store.rows == [{"payload": value} for value in values]
    assert store.wire_columns == [
        ("payload", native, _wire(values, kind, nullable=native.startswith("Nullable(")))
    ]
    assert [name for name, _statement in store.submissions] == ["CLEAR", "INSERT"]
    assert store.create_calls == []
    assert store.opened
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("kind", _KINDS, ids=[kind[0] for kind in _KINDS])
def test_ch_integer_text_load_preserves_native_width_values_and_driver_bytes(
    monkeypatch, transport, explicit, kind
):
    native = kind[0]
    lower, upper = _bounds(kind)
    values = [str(lower), upper, "0", "+1", "-0", "0001"]
    expected = [lower, upper, 0, 1, 0, 1]
    store = integer_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, kind, native, expected)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("kind", _KINDS, ids=[kind[0] for kind in _KINDS])
def test_ch_native_integer_boundaries_use_the_actual_transport_codec(monkeypatch, transport, kind):
    native = kind[0]
    values = list(_bounds(kind))
    store = integer_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=False) == len(values)

    _assert_native_write(store, kind, native, values)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("position", [0, 1, 2], ids=["null-first", "null-middle", "null-last"])
@pytest.mark.parametrize("kind", _WIDE_KINDS, ids=[kind[0] for kind in _WIDE_KINDS])
def test_ch_nullable_wide_integer_text_keeps_every_null_position_and_exact_bits(
    monkeypatch, transport, explicit, position, kind
):
    native = f"Nullable({kind[0]})"
    lower, upper = _bounds(kind)
    values = [str(lower), str(upper)]
    expected = [lower, upper]
    values.insert(position, None)
    expected.insert(position, None)
    store = integer_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, kind, native, expected)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("kind", [kind for kind in _KINDS if kind[1] == 16])
def test_ch_integer_text_keeps_supported_python_int_string_forms(
    monkeypatch, transport, explicit, kind
):
    native = kind[0]
    values = ["1_000", "\uff11\uff12", "\u00a0+7\u2003", " \t+0013\r\n", "-0"]
    expected = [1000, 12, 7, 13, 0]
    store = integer_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, kind, native, expected)
    pd.testing.assert_frame_equal(frame, original)


def _assert_rejected_before_clear(store, frame, native, *, explicit):
    before = deepcopy(store.rows)
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match=r"integer|Int|UInt") as error:
        _load(frame, native, explicit=explicit)

    assert "private-integer-value" not in str(error.value)
    assert store.rows == before
    assert store.schema == {"payload": native}
    assert store.submissions == []
    assert store.create_calls == []
    assert store.wire_columns == []
    assert store.bound_rows == []
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "bad", ["private-integer-value", "1.5", "1e2", "1__0", "0x10", "--1", "", "²"]
)
def test_ch_invalid_integer_text_rejects_before_target_clear(monkeypatch, transport, explicit, bad):
    native = "Int256"
    store = integer_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(["0", "1", bad], dtype=object)})

    _assert_rejected_before_clear(store, frame, native, explicit=explicit)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("kind", _KINDS, ids=[kind[0] for kind in _KINDS])
@pytest.mark.parametrize("side", ["lower", "upper"])
def test_ch_integer_text_outside_native_width_rejects_before_target_clear(
    monkeypatch, transport, explicit, kind, side
):
    native = kind[0]
    lower, upper = _bounds(kind)
    bad = lower - 1 if side == "lower" else upper + 1
    store = integer_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(["0", str(bad)], dtype=object)})

    _assert_rejected_before_clear(store, frame, native, explicit=explicit)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_ch_numeric_strings_without_authority_keep_inferred_string(
    monkeypatch, write_sql_connections, transport
):
    store = inferred_string_target(monkeypatch, write_sql_connections, transport)
    values = ["001", "-2"]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
    ) == len(values)

    assert store.schema == {"payload": "String"}
    assert store.rows == [{"payload": value} for value in values]
    assert store.bound_rows == [(value,) for value in values]
    assert store.wire_columns == [("payload", "String", b"\x03001\x02-2")]
    assert len(store.created_sql) == len(store.create_calls) == 1
    creation = parse_one(store.created_sql[0], read="clickhouse")
    assert isinstance(creation, exp.Create)
    assert isinstance(creation.this, exp.Schema)
    assert [column.name for column in creation.this.expressions] == ["payload"]
    assert [kind for kind, _statement in store.submissions] == ["CREATE", "INSERT"]
    assert store.exists
    pd.testing.assert_frame_equal(frame, original)
    assert store.opened
    assert all(connection.close_calls == 1 for connection in store.opened)
