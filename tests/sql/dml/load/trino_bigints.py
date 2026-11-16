"""Authoritative BIGINT preserves the historical exact Python int conversion."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql._support.trino_bigints import native_bigint_target

_MINIMUM = -(2**63)
_MAXIMUM = 2**63 - 1
_CASES = [
    (["42", "+42", " -42 ", "00042", "\t+00042\n"], [42, 42, -42, 42, 42]),
    ([0, -1, 42], [0, -1, 42]),
    ([_MINIMUM, _MAXIMUM], [_MINIMUM, _MAXIMUM]),
    ([str(_MINIMUM), str(_MAXIMUM)], [_MINIMUM, _MAXIMUM]),
    ([1.9, -1.9, 0.9, -0.9], [1, -1, 0, 0]),
    (
        [
            Decimal("1.9"),
            Decimal("-1.9"),
            Decimal("9223372036854775807.9"),
            Decimal("-9223372036854775808.9"),
        ],
        [1, -1, _MAXIMUM, _MINIMUM],
    ),
]


def _load(frame, *, explicit):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **({"table_schema": {"payload": "BIGINT"}} if explicit else {}),
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
@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=["numeric-text", "native-int", "native-bounds", "text-bounds", "double", "decimal"],
)
def test_trino_bigints_public_load_keeps_explicit_int_assignment_and_signed_bounds(
    monkeypatch, explicit, case
):
    incoming, converted = case
    database = native_bigint_target(monkeypatch)
    values = [None, *incoming, None]
    expected = [None, *converted, None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, explicit=explicit)

    assert type(result) is int
    assert result == len(values)
    assert database.rows == expected
    assert all(type(value) is int for value in database.rows if value is not None)
    assert database.native_type == "BIGINT"
    assert database.table_types == {database.target: "BIGINT"}
    inserts = _insertions(database)
    assert len(inserts) == (len(values) + 1) // 2
    assert database.mutations == ["clear"] + ["insert"] * len(inserts)
    assert all("CAST(? AS BIGINT)" in statement for statement, _params, _wire in inserts)
    params = [value for _statement, values, _wire in inserts for value in values]
    wire = [value for _statement, _params, values in inserts for value in values]
    assert params == expected
    assert all(type(value) is int for value in params if value is not None)
    assert wire == ["NULL" if value is None else str(value) for value in expected]
    assert any(
        "information_schema.columns" in statement
        for statement, _params, _wire in database.statements
    )
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "bad",
    ["private-invalid-bigint", str(_MAXIMUM + 1), Decimal(_MINIMUM - 1), float(2**63)],
    ids=["private-invalid-token", "text-overflow", "decimal-underflow", "double-overflow"],
)
def test_trino_bigints_invalid_late_assignment_preserves_target_before_clear_or_insert(
    monkeypatch, explicit, bad
):
    database = native_bigint_target(monkeypatch)
    before = deepcopy(database.table_rows)
    frame = pd.DataFrame({"payload": pd.Series([0, None, 1, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match=r"(?i)bigint|integer") as error:
        _load(frame, explicit=explicit)

    assert "private-invalid-bigint" not in str(error.value)
    assert database.table_rows == before
    assert database.table_types == {database.target: "BIGINT"}
    assert database.mutations == []
    assert _insertions(database) == []
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)
