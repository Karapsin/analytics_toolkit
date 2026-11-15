"""Authoritative ClickHouse timestamps retain native integer seconds and scaled ticks."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.ch_integer_timestamps import timestamp_target

_SCALARS = [
    ("DateTime('UTC')", [0, 1, 1_700_000_000, 2**32 - 1], 4, False),
    ("DateTime64(0, 'UTC')", [0, 1, -1, 1_700_000_000], 8, True),
    ("DateTime64(3, 'UTC')", [0, 1, -1, 1_700_000_000_123], 8, True),
    ("DateTime64(6, 'UTC')", [0, 1, -1, 1_700_000_000_123_456], 8, True),
    ("DateTime64(9, 'UTC')", [0, 1, -1, 1_700_000_000_123_456_789], 8, True),
]


def _wire(ticks, width, *, signed, nullable=False):
    body = b"".join(
        int(value if value is not None else 0).to_bytes(width, "little", signed=signed)
        for value in ticks
    )
    return bytes(value is None for value in ticks) + body if nullable else body


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


def _assert_native_write(store, native, values, wire):
    assert store.schema == {"payload": native}
    assert store.bound_rows == [(value,) for value in values]
    assert store.rows == [{"payload": value} for value in values]
    assert store.wire_columns == [("payload", native, wire)]
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    assert store.create_calls == []
    assert store.opened
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("case", _SCALARS, ids=["seconds", "p0", "p3", "p6", "p9"])
def test_ch_integer_timestamp_values_reach_public_native_writes(
    monkeypatch, transport, explicit, case
):
    native, values, width, signed = case
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, native, values, _wire(values, width, signed=signed))
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("position", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.parametrize("case", [_SCALARS[0], _SCALARS[-1]], ids=["seconds", "p9"])
def test_ch_integer_timestamp_null_positions_keep_the_real_nullable_wire(
    monkeypatch, transport, position, case
):
    inner, _original, width, signed = case
    native = f"Nullable({inner})"
    values = [0, 7]
    values.insert(position, None)
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})

    assert _load(frame, native, explicit=False) == len(values)

    _assert_native_write(store, native, values, _wire(values, width, signed=signed, nullable=True))


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("case", [_SCALARS[0], _SCALARS[-1]], ids=["seconds", "p9"])
def test_ch_integer_timestamp_all_null_input_preserves_nullable_schema(
    monkeypatch, transport, case
):
    inner, _original, width, signed = case
    native = f"Nullable({inner})"
    values = [None, None]
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})

    assert _load(frame, native, explicit=True) == len(values)

    _assert_native_write(store, native, values, _wire(values, width, signed=signed, nullable=True))


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("case", [_SCALARS[0], _SCALARS[-1]], ids=["seconds", "p9"])
def test_ch_authoritative_timestamp_keeps_supported_boolean_integral_values(
    monkeypatch, transport, explicit, case
):
    native, _original, width, signed = case
    values = [False, True]
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, native, values, _wire([0, 1], width, signed=signed))
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        (
            "DateTime('UTC')",
            [np.bool_(False), np.bool_(True)],
            _wire([0, 1], 4, signed=False),
        ),
        (
            "Nullable(DateTime64(9, 'UTC'))",
            [np.bool_(False), None, np.bool_(True)],
            _wire([0, None, 1], 8, signed=True, nullable=True),
        ),
        (
            "Array(DateTime64(3, 'UTC'))",
            [[np.bool_(False), np.bool_(True)], [np.bool_(True)]],
            _wire([2, 3], 8, signed=False) + _wire([0, 1, 1], 8, signed=True),
        ),
    ],
    ids=["scalar", "nullable", "nested"],
)
def test_ch_numpy_boolean_timestamp_values_are_certified_before_native_write(
    monkeypatch, transport, explicit, case
):
    native, values, wire = case
    store = timestamp_target(monkeypatch, transport, native, seed=deepcopy(values[0]))
    frame = pd.DataFrame({"payload": pd.Series(deepcopy(values), dtype=object)})
    original = deepcopy(values)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, native, values, wire)
    assert frame["payload"].tolist() == original


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("scale", [0, 3], ids=["seconds", "millisecond-ticks"])
def test_ch_integer_timestamp_mixed_calendars_do_not_rescale_native_ticks(
    monkeypatch, transport, explicit, scale
):
    native = "DateTime('UTC')" if scale == 0 else "DateTime64(3, 'UTC')"
    values = [
        1,
        dt.datetime(1970, 1, 1, 0, 0, 2, tzinfo=dt.timezone.utc),
        dt.date(1970, 1, 2),
        3,
    ]
    ticks = [1, 2 * 10**scale, 86_400 * 10**scale, 3]
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=explicit) == len(values)

    width = 4 if scale == 0 else 8
    assert store.wire_columns == [("payload", native, _wire(ticks, width, signed=scale != 0))]
    assert store.schema == {"payload": native}
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    assert store.create_calls == []
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        (
            "Array(DateTime('UTC'))",
            [[0, 1], [1_700_000_000, 2**32 - 1]],
            _wire([2, 4], 8, signed=False)
            + _wire([0, 1, 1_700_000_000, 2**32 - 1], 4, signed=False),
        ),
        (
            "Tuple(moment DateTime('UTC'), precise DateTime64(9, 'UTC'))",
            [(0, 1), (1_700_000_000, -1)],
            _wire([0, 1_700_000_000], 4, signed=False) + _wire([1, -1], 8, signed=True),
        ),
    ],
    ids=["array", "tuple"],
)
def test_ch_integer_timestamp_nested_leaves_keep_native_units_and_layout(
    monkeypatch, transport, explicit, case
):
    native, values, wire = case
    store = timestamp_target(monkeypatch, transport, native, seed=deepcopy(values[0]))
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = deepcopy(values)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, native, values, wire)
    assert frame["payload"].tolist() == original


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    ("native", "bad"),
    [
        ("DateTime('UTC')", -1),
        ("DateTime('UTC')", 2**32),
        ("DateTime64(9, 'UTC')", -(2**63) - 1),
        ("DateTime64(9, 'UTC')", 2**63),
        ("DateTime('UTC')", 1.5),
        ("DateTime64(3, 'UTC')", Decimal("1.5")),
    ],
    ids=[
        "negative-seconds",
        "seconds-overflow",
        "ticks-underflow",
        "ticks-overflow",
        "float",
        "decimal",
    ],
)
def test_ch_integer_timestamp_out_of_range_rejects_before_clear(
    monkeypatch, transport, explicit, native, bad
):
    store = timestamp_target(monkeypatch, transport, native)
    before = deepcopy(store.rows)
    frame = pd.DataFrame({"payload": pd.Series([0, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises((ValueError, TypeError, OverflowError)):
        _load(frame, native, explicit=explicit)

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
    ("native", "bad"),
    [
        ("DateTime('UTC')", -1),
        ("DateTime('UTC')", 2**32),
        ("DateTime64(3, 'UTC')", -(2**63) - 1),
        ("DateTime64(9, 'UTC')", 2**63),
    ],
    ids=["negative-seconds", "seconds-overflow", "ticks-underflow", "ticks-overflow"],
)
def test_ch_mixed_calendar_and_integer_timestamp_bounds_reject_before_clear(
    monkeypatch, transport, explicit, native, bad
):
    store = timestamp_target(monkeypatch, transport, native)
    before = deepcopy(store.rows)
    frame = pd.DataFrame(
        {"payload": pd.Series([dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), bad], dtype=object)}
    )
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match="native integer timestamp range"):
        _load(frame, native, explicit=explicit)

    assert store.rows == before
    assert store.schema == {"payload": native}
    assert store.submissions == []
    assert store.create_calls == []
    assert store.wire_columns == []
    assert store.bound_rows == []
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in store.opened)


def _assert_calendar_rejection_before_clear(store, frame, native, *, explicit):
    before = deepcopy(store.rows)
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match="timestamp range"):
        _load(frame, native, explicit=explicit)

    assert store.rows == before
    assert store.schema == {"payload": native}
    assert store.submissions == []
    assert store.create_calls == []
    assert store.wire_columns == []
    assert store.bound_rows == []
    pd.testing.assert_frame_equal(frame, original)
    assert store.opened
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("scale", [0, 3, 6, 9], ids=["p0", "p3", "p6", "p9"])
def test_ch_integer_datetime64_calendar_boundary_ticks_keep_native_units(
    monkeypatch, transport, explicit, scale
):
    native = f"DateTime64({scale}, 'UTC')"
    minimum = -2_208_988_800 * 10**scale
    maximum = min(10_413_792_000 * 10**scale - 1, 2**63 - 1)
    values = [minimum, minimum + 1, 0, maximum - 1, maximum]
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, native, values, _wire(values, 8, signed=True))
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        (0, -2_208_988_801),
        (0, 10_413_792_000),
        (3, -2_208_988_800_001),
        (3, 10_413_792_000_000),
        (6, -2_208_988_800_000_001),
        (6, 10_413_792_000_000_000),
        (9, -2_208_988_800_000_000_001),
    ],
    ids=[
        "p0-before-1900",
        "p0-after-2299",
        "p3-before-1900",
        "p3-after-2299",
        "p6-before-1900",
        "p6-after-2299",
        "p9-before-1900",
    ],
)
def test_ch_integer_datetime64_calendar_overflow_rejects_before_clear(
    monkeypatch, transport, explicit, case
):
    scale, bad = case
    native = f"DateTime64({scale}, 'UTC')"
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame({"payload": pd.Series([0, bad], dtype=object)})

    _assert_calendar_rejection_before_clear(store, frame, native, explicit=explicit)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("scale", [0, 3, 6], ids=["p0", "p3", "p6"])
@pytest.mark.parametrize("side", ["lower", "upper"])
def test_ch_mixed_datetime64_calendar_overflow_rejects_before_clear(
    monkeypatch, transport, explicit, scale, side
):
    native = f"DateTime64({scale}, 'UTC')"
    bad = -2_208_988_800 * 10**scale - 1 if side == "lower" else 10_413_792_000 * 10**scale
    store = timestamp_target(monkeypatch, transport, native)
    frame = pd.DataFrame(
        {"payload": pd.Series([dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), bad], dtype=object)}
    )

    _assert_calendar_rejection_before_clear(store, frame, native, explicit=explicit)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        ("Nullable(DateTime64(3, 'UTC'))", [None, 0, -2_208_988_800_001]),
        ("Array(DateTime64(6, 'UTC'))", [[0], [10_413_792_000_000_000]]),
    ],
    ids=["nullable", "nested-array"],
)
def test_ch_nested_integer_datetime64_calendar_overflow_rejects_before_clear(
    monkeypatch, transport, explicit, case
):
    native, values = case
    store = timestamp_target(monkeypatch, transport, native, seed=deepcopy(values[-2]))
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})

    _assert_calendar_rejection_before_clear(store, frame, native, explicit=explicit)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        (
            "Array(DateTime('UTC'))",
            [(0,), (1, 1_700_000_000)],
            _wire([1, 3], 8, signed=False) + _wire([0, 1, 1_700_000_000], 4, signed=False),
        ),
        (
            "Array(DateTime64(3, 'UTC'))",
            [(0,), (1, 1_001)],
            _wire([1, 3], 8, signed=False) + _wire([0, 1, 1_001], 8, signed=True),
        ),
        (
            "Array(Nullable(DateTime64(9, 'UTC')))",
            [(None, 0), (1, None, -1)],
            _wire([2, 5], 8, signed=False)
            + _wire([None, 0, 1, None, -1], 8, signed=True, nullable=True),
        ),
    ],
    ids=["seconds", "millisecond-ticks", "nullable-nanosecond-ticks"],
)
def test_ch_authoritative_timestamp_array_accepts_tuple_sequences(
    monkeypatch, transport, explicit, case
):
    native, values, wire = case
    store = timestamp_target(monkeypatch, transport, native, seed=deepcopy(values[0]))
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = deepcopy(values)

    assert _load(frame, native, explicit=explicit) == len(values)

    _assert_native_write(store, native, [list(value) for value in values], wire)
    assert frame["payload"].tolist() == original


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_ch_authoritative_timestamp_tuple_accepts_list_cells(monkeypatch, transport, explicit):
    native = "Tuple(moment DateTime('UTC'), precise DateTime64(9, 'UTC'))"
    values = [[0, 1], [1_700_000_000, -1]]
    store = timestamp_target(monkeypatch, transport, native, seed=deepcopy(values[0]))
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = deepcopy(values)

    assert _load(frame, native, explicit=explicit) == len(values)

    wire = _wire([0, 1_700_000_000], 4, signed=False) + _wire([1, -1], 8, signed=True)
    _assert_native_write(store, native, [tuple(value) for value in values], wire)
    assert frame["payload"].tolist() == original


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("alias", ["DateTime32('UTC')", "TIMESTAMP('UTC')"])
def test_ch_explicit_timestamp_aliases_keep_existing_calendar_assignment(
    monkeypatch, transport, alias
):
    physical = "DateTime('UTC')"
    value = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    store = timestamp_target(monkeypatch, transport, physical)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, alias, explicit=True) == 1

    _assert_native_write(store, physical, [value], _wire([946_684_800], 4, signed=False))
    pd.testing.assert_frame_equal(frame, original)


_SECOND_ALIASES = ["DateTime32", "DateTime32('UTC')", "TIMESTAMP", "TIMESTAMP('UTC')"]


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("alias", _SECOND_ALIASES)
def test_ch_explicit_timestamp_aliases_accept_native_integer_seconds(monkeypatch, transport, alias):
    physical = "DateTime('UTC')"
    values = [0, 1, 2**32 - 1, False, True]
    store = timestamp_target(monkeypatch, transport, physical)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, alias, explicit=True) == len(values)

    _assert_native_write(store, physical, values, _wire(values, 4, signed=False))
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("alias", _SECOND_ALIASES)
@pytest.mark.parametrize("bad", [-1, 2**32], ids=["negative", "overflow"])
def test_ch_explicit_timestamp_aliases_reject_out_of_range_before_clear(
    monkeypatch, transport, alias, bad
):
    physical = "DateTime('UTC')"
    store = timestamp_target(monkeypatch, transport, physical)
    before = deepcopy(store.rows)
    frame = pd.DataFrame({"payload": pd.Series([0, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match="timestamp range"):
        _load(frame, alias, explicit=True)

    assert store.rows == before
    assert store.schema == {"payload": physical}
    assert store.submissions == []
    assert store.create_calls == []
    assert store.wire_columns == []
    assert store.bound_rows == []
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in store.opened)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("scale", [0, 3, 9], ids=["seconds", "milliseconds", "nanoseconds"])
def test_ch_scaled_timestamp_alias_preserves_actual_server_precision(monkeypatch, transport, scale):
    physical = "DateTime('UTC')" if scale == 0 else f"DateTime64({scale}, 'UTC')"
    alias = f"TIMESTAMP({scale}, 'UTC')"
    values = [0, 1, 1_700_000_000 * 10**scale]
    if scale:
        values.append(-1)
    store = timestamp_target(monkeypatch, transport, physical)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, alias, explicit=True) == len(values)

    _assert_native_write(
        store, physical, values, _wire(values, 8 if scale else 4, signed=bool(scale))
    )
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize(
    "alias", ["timestamp('UTC')", "dAtEtImE32('UTC')", "TIMESTAMP()", "DateTime32()"]
)
def test_ch_timestamp_alias_case_and_empty_arguments_keep_second_units(
    monkeypatch, transport, alias
):
    physical = "DateTime('UTC')"
    values = [0, 1, False, True]
    store = timestamp_target(monkeypatch, transport, physical)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, alias, explicit=True) == len(values)

    _assert_native_write(store, physical, values, _wire(values, 4, signed=False))
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize(
    "alias",
    [
        "DateTime32(3)",
        "DateTime32('UTC', 'Europe/London')",
        "DateTime32(timezone())",
        "TIMESTAMP(10, 'UTC')",
        "TIMESTAMP(-1, 'UTC')",
        "TIMESTAMP(3.5, 'UTC')",
        "TIMESTAMP(1 + 2)",
        "TIMESTAMP(3, 123)",
        "TIMESTAMP('UTC', 'Europe/London')",
        "TIMESTAMP('UTC' +)",
    ],
    ids=[
        "datetime32-scale",
        "multiple-zones",
        "dynamic-zone",
        "scale-too-large",
        "negative-scale",
        "fractional-scale",
        "expression-scale",
        "numeric-zone",
        "timestamp-multiple-zones",
        "malformed-literal",
    ],
)
def test_ch_invalid_timestamp_alias_arguments_reject_before_clear(monkeypatch, transport, alias):
    physical = "DateTime('UTC')"
    store = timestamp_target(monkeypatch, transport, physical)
    before = deepcopy(store.rows)
    frame = pd.DataFrame({"payload": pd.Series([0, 1], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match="timestamp storage type"):
        _load(frame, alias, explicit=True)

    assert store.rows == before
    assert store.schema == {"payload": physical}
    assert store.submissions == []
    assert store.create_calls == []
    assert store.wire_columns == []
    assert store.bound_rows == []
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in store.opened)
