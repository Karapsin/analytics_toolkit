"""Finite authoritative numeric text reaches exact Trino native parameters."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.trino.numeric_text import TrinoNumericTextValueError
from sqlglot import exp, parse_one

from tests.sql._support.trino_numeric_text import native_numeric_target

_EXACT = "12345678901234567890.123456789"
_TYPES = ["REAL", "DOUBLE", "DECIMAL(38,9)"]
_INVALID = [(native, "private-invalid-numeric") for native in _TYPES] + [
    ("DECIMAL(38,9)", "NaN"),
    ("DECIMAL(38,9)", "Infinity"),
    ("REAL", "1E100"),
    ("DOUBLE", "1E1000"),
    ("DECIMAL(38,9)", "1E29"),
    ("DECIMAL(38,9)", "0.0000000001"),
]


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


def _assert_native_float_wire(wire, expected):
    value = parse_one(wire, read="trino")
    if isinstance(value, exp.Cast):
        # Current SDKs render native floats as DOUBLE '1.25'; older supported
        # SDKs emitted the corresponding unquoted numeric literal.
        assert value.args["to"].is_type("DOUBLE")
        value = value.this
        assert isinstance(value, exp.Literal)
        assert value.is_string
        assert float(value.this) == expected
    else:
        sign = 1
        if isinstance(value, exp.Neg):
            sign = -1
            value = value.this
        assert isinstance(value, exp.Literal)
        assert not value.is_string
        assert sign * float(value.this) == expected


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("native", _TYPES)
def test_trino_numeric_text_public_native_parameters_preserve_finite_values_exactly(
    monkeypatch, explicit, native
):
    decimal = native.startswith("DECIMAL")
    if decimal:
        incoming = [None, _EXACT, "-0.000000001", None]
        expected = [None, Decimal(_EXACT), Decimal("-0.000000001"), None]
    else:
        incoming = [None, "1.25", "-2.5", None]
        expected = [None, 1.25, -2.5, None]
    database = native_numeric_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series(incoming, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, native, explicit=explicit)

    assert type(result) is int
    assert result == 4
    assert database.rows == expected
    assert database.table_types == {database.target: native}
    inserts = _insertions(database)
    assert len(inserts) == 2
    assert database.mutations == ["clear", "insert", "insert"]
    assert all(f"CAST(? AS {native})" in statement for statement, _params, _wire in inserts)
    params = [value for _statement, values, _wire in inserts for value in values]
    wire = [value for _statement, _params, values in inserts for value in values]
    assert params == expected
    assert wire[0] == wire[-1] == "NULL"
    if decimal:
        assert all(isinstance(value, Decimal) for value in params if value is not None)
        assert wire[1:3] == [f"DECIMAL '{_EXACT}'", "DECIMAL '-0.000000001'"]
        assert database.physical_float_values == []
    else:
        assert all(type(value) is float for value in params if value is not None)
        _assert_native_float_wire(wire[1], 1.25)
        _assert_native_float_wire(wire[2], -2.5)
        expected_bits = (
            [bytes.fromhex("0000a03f"), bytes.fromhex("000020c0")]
            if native == "REAL"
            else [bytes.fromhex("000000000000f43f"), bytes.fromhex("00000000000004c0")]
        )
        assert database.physical_float_values == expected_bits
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("case", _INVALID)
def test_trino_numeric_text_invalid_last_value_never_clears_or_inserts(monkeypatch, explicit, case):
    native, bad = case
    database = native_numeric_target(monkeypatch, native)
    before = deepcopy(database.table_rows)
    frame = pd.DataFrame({"payload": pd.Series(["1.25", None, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(
        TrinoNumericTextValueError,
        match=(
            r"^Trino numeric text requires a finite native numeric value "
            r"that fits the declared type\."
        ),
    ) as error:
        _load(frame, native, explicit=explicit)

    assert "private-invalid-numeric" not in str(error.value)
    assert database.table_rows == before
    assert database.table_types == {database.target: native}
    assert database.mutations == []
    assert _insertions(database) == []
    assert database.physical_float_values == []
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


def test_trino_numeric_text_without_authority_remains_varchar():
    values = pd.Series(["1.25", _EXACT], dtype=object)
    original = values.copy(deep=True)

    assert get_backend_adapter("trino").infer_dataframe_column_type(values) == "VARCHAR"

    pd.testing.assert_series_equal(values, original)
