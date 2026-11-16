"""Authoritative Greenplum integer text keeps exact values and width boundaries."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_integers import integer_target

_TYPES = [("SMALLINT", "int2", 16), ("INTEGER", "int4", 32), ("BIGINT", "int8", 64)]
_AUTHORITIES = [
    ("SMALLINT", None, 16),
    ("SMALLINT", "SMALLINT", 16),
    ("SMALLINT", "int2", 16),
    ("INTEGER", None, 32),
    ("INTEGER", "INTEGER", 32),
    ("INTEGER", "int", 32),
    ("INTEGER", "int4", 32),
    ("BIGINT", None, 64),
    ("BIGINT", "BIGINT", 64),
    ("BIGINT", "int8", 64),
]


def _load(frame, schema=None, *, write_mode="truncate_insert"):
    options = {"table_schema": {"payload": schema}} if schema is not None else {}
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode=write_mode,
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _assert_native_write(database, physical, values):
    assert database.target.schema == {"payload": physical}
    assert database.target.rows == [{"payload": value} for value in values]
    assert all(
        row["payload"] is None
        or (isinstance(row["payload"], int) and not isinstance(row["payload"], bool))
        for row in database.target.rows
    )
    assert database.bound_rows == [(value,) for value in values]
    assert database.wire_rows == [("NULL" if value is None else str(value),) for value in values]
    assert [row for _template, row, _wire in database.formatter_observations] == [
        (value,) for value in values
    ]
    assert [kind for kind, _statement in database.submissions] == ["CLEAR", "INSERT"]
    assert set(database.tables) == {("sandbox", "events")}
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("authority", _AUTHORITIES)
def test_gp_integer_text_load_preserves_exact_native_values_and_width(monkeypatch, authority):
    physical, explicit, bits = authority
    minimum, maximum = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    values = [
        "0",
        "-0",
        "+0",
        "00042",
        "-00007",
        " \t\r\n\v\f+13 \t\r\n\v\f",
        str(minimum),
        str(maximum),
        None,
    ]
    expected = [0, 0, 0, 42, -7, 13, minimum, maximum, None]
    database = integer_target(monkeypatch, physical)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, explicit) == len(values)

    _assert_native_write(database, physical, expected)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("native", _TYPES)
def test_gp_native_integer_assignment_keeps_existing_metadata(monkeypatch, native):
    physical, _alias, bits = native
    values = [-(2 ** (bits - 1)), 0, 2 ** (bits - 1) - 1, None]
    database = integer_target(monkeypatch, physical)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame) == len(values)

    _assert_native_write(database, physical, values)
    pd.testing.assert_frame_equal(frame, original)


def _assert_rejects_before_clear(database, frame, schema):
    target = database.target
    before = deepcopy(target.rows)
    schema_before = dict(target.schema)
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match=r"integer|INTEGER|SMALLINT|BIGINT") as failure:
        _load(frame, schema)

    assert "private-integer-value" not in str(failure.value)
    assert database.target is target
    assert database.target.rows == before
    assert database.target.schema == schema_before
    assert database.live_mutations == []
    assert database.bound_rows == []
    assert database.wire_rows == []
    assert database.formatter_observations == []
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("schema", [None, "int8"], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "bad",
    [
        "private-integer-value",
        "1.5",
        "1e2",
        "1_000",
        "\uff11\uff12",
        "\u00a01",
        "1 2",
        "+",
        "",
        "1\x00",
    ],
    ids=[
        "private-invalid",
        "fraction",
        "exponent",
        "underscore",
        "unicode-digits",
        "unicode-space",
        "interior-space",
        "sign-only",
        "empty",
        "nul",
    ],
)
def test_gp_integer_text_invalid_late_value_rejects_before_target_clear(monkeypatch, schema, bad):
    database = integer_target(monkeypatch, "BIGINT")
    frame = pd.DataFrame({"payload": pd.Series(["42", "-7", None, bad], dtype=object)})

    _assert_rejects_before_clear(database, frame, schema)


@pytest.mark.parametrize("native", _TYPES)
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("side", ["lower", "upper"])
def test_gp_integer_text_width_overflow_rejects_before_target_clear(
    monkeypatch, native, explicit, side
):
    physical, alias, bits = native
    bad = -(2 ** (bits - 1)) - 1 if side == "lower" else 2 ** (bits - 1)
    database = integer_target(monkeypatch, physical)
    frame = pd.DataFrame({"payload": pd.Series(["0", str(bad)], dtype=object)})

    _assert_rejects_before_clear(database, frame, alias if explicit else None)


def test_gp_numeric_strings_without_authority_keep_inferred_text(monkeypatch):
    database = integer_target(monkeypatch, "INTEGER", exists=False)
    values = ["001", "-2", None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, write_mode="append") == len(values)

    assert database.target.schema == {"payload": "TEXT"}
    assert database.target.rows == [{"payload": value} for value in values]
    assert database.bound_rows == [(value,) for value in values]
    assert database.wire_rows == [("'001'",), ("'-2'",), ("NULL",)]
    assert [kind for kind, _statement in database.submissions] == ["DDL", "INSERT"]
    pd.testing.assert_frame_equal(frame, original)
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)
