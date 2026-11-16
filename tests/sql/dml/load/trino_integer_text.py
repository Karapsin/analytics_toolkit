"""Trino TINYINT/SMALLINT/INTEGER keep their native integer-text cast contract."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql._support.trino_integer_text import native_integer_target

_TYPES = [("TINYINT", 8), ("SMALLINT", 16), ("INTEGER", 32), ("INT", 32)]


def _load(frame, native, *, explicit):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **({"table_schema": {"payload": native}} if explicit else {}),
    )


def _insertions(database):
    return [
        (statement, params, wire)
        for statement, params, wire in database.statements
        if isinstance(parse_one(statement, read="trino"), exp.Insert)
    ]


def _assert_closed(database):
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("case", _TYPES, ids=["tinyint", "smallint", "integer", "int-alias"])
def test_trino_integer_text_public_casts_preserve_native_grammar_and_width_bounds(
    monkeypatch, explicit, case
):
    native, width = case
    physical = "INTEGER" if native == "INT" else native
    lower, upper = -(2 ** (width - 1)), 2 ** (width - 1) - 1
    incoming = [
        None,
        "42",
        "+42",
        " -42 ",
        "00042",
        "\t+00042 \r\n",
        str(lower),
        str(upper),
        None,
    ]
    expected = [None, 42, 42, -42, 42, 42, lower, upper, None]
    database = native_integer_target(monkeypatch, physical)
    frame = pd.DataFrame({"payload": pd.Series(incoming, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, native, explicit=explicit)

    assert type(result) is int
    assert result == len(incoming)
    assert database.rows == expected
    assert all(type(value) is int for value in database.rows if value is not None)
    assert database.table_types == {database.target: physical}
    inserts = _insertions(database)
    assert len(inserts) == 5
    assert database.mutations == ["clear"] + ["insert"] * len(inserts)
    wire_type = native if explicit else physical
    assert all(f"CAST(? AS {wire_type})" in statement for statement, _params, _wire in inserts)
    params = [value for _statement, values, _wire in inserts for value in values]
    wire = [value for _statement, _params, values in inserts for value in values]
    for value, rendered, source, number in zip(params, wire, incoming, expected):
        if number is None:
            assert value is None
            assert rendered == "NULL"
        elif type(value) is int:
            assert value == number
            assert rendered == str(number)
        else:
            assert type(value) is str
            assert value == source
            assert rendered == "'" + source.replace("'", "''") + "'"
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("case", _TYPES, ids=["tinyint", "smallint", "integer", "int-alias"])
@pytest.mark.parametrize("fault", ["private", "underscore", "fraction", "overflow"])
def test_trino_integer_text_invalid_late_cast_preserves_target_before_mutation(
    monkeypatch, explicit, case, fault
):
    native, width = case
    physical = "INTEGER" if native == "INT" else native
    bad = {
        "private": "private-invalid-integer",
        "underscore": "4_2",
        "fraction": "42.0",
        "overflow": str(2 ** (width - 1)),
    }[fault]
    database = native_integer_target(monkeypatch, physical)
    before = deepcopy(database.table_rows)
    frame = pd.DataFrame({"payload": pd.Series([0, None, 1, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match=r"(?i)\b(?:integer|tinyint|smallint|int)\b") as error:
        _load(frame, native, explicit=explicit)

    assert "private-invalid-integer" not in str(error.value)
    assert database.table_rows == before
    assert database.table_types == {database.target: physical}
    assert database.mutations == []
    assert _insertions(database) == []
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)
