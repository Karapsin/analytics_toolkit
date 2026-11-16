"""Authoritative GP numeric text preserves native floats and exact Decimal values."""

from __future__ import annotations

import math
import struct
from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from psycopg2.extensions import AsIs
from sqlglot import exp, parse_one

from tests.sql._support.gp_numeric_text import numeric_text_target

_TYPES = ["REAL", "DOUBLE PRECISION", "NUMERIC(38, 9)", "NUMERIC"]
_EXACT = "12345678901234567890.123456789"


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


def _closed(database):
    assert all(connection.closed and connection.close_calls == 1 for connection in database.opened)


def _assert_float_transport(database, values, native):
    assert len(database.bound_rows) == len(database.wire_rows) == len(values)
    assert [row for _template, row, _wire in database.formatter_observations] == database.bound_rows
    for (actual,), (wire,), expected in zip(database.bound_rows, database.wire_rows, values):
        if expected is None:
            assert actual is None
            assert wire == "NULL"
        elif expected == 0.0 and math.copysign(1.0, expected) < 0:
            assert isinstance(actual, AsIs)
            assert actual.getquoted() == (
                b"'-0.0'::REAL" if native == "REAL" else b"'-0.0'::FLOAT8"
            )
            expression = parse_one(wire, read="postgres")
            assert isinstance(expression, exp.Cast)
            requested = expression.args["to"].sql(dialect="postgres").upper().replace(" ", "")
            assert requested == ("REAL" if native == "REAL" else "DOUBLEPRECISION")
            assert isinstance(expression.this, exp.Literal)
            assert expression.this.is_string
            assert expression.this.this == "-0.0"
        else:
            assert type(actual) is float
            parameter = (
                struct.unpack("!f", struct.pack("!f", expected))[0]
                if native == "REAL"
                else expected
            )
            assert struct.pack("!d", actual) == struct.pack("!d", parameter)
            assert float(wire) == parameter


@pytest.mark.parametrize("native", _TYPES)
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_numeric_text_native_assignment_keeps_exact_values_and_physical_schema(
    monkeypatch, native, explicit
):
    floating = native in {"REAL", "DOUBLE PRECISION"}
    values = (
        ["1.25", "-2.5", None, "0.1", " +1e2 ", 1.75, "-0.0", "+0.0"]
        if floating
        else [_EXACT, "-" + _EXACT, None, "1.25", "1e5", Decimal("-0.125")]
    )
    bound = [
        None if value is None else float(value) if floating else Decimal(value) for value in values
    ]
    stored = [
        struct.unpack("<f", struct.pack("<f", value))[0]
        if value is not None and native == "REAL"
        else value
        for value in bound
    ]
    database = numeric_text_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, native if explicit else None) == len(values)

    assert database.schema == {"payload": native}
    assert database.rows == [{"payload": value} for value in stored]
    expected_type = float if floating else Decimal
    if floating:
        _assert_float_transport(database, bound, native)
    else:
        assert database.bound_rows == [(value,) for value in bound]
        assert [row for _template, row, _wire in database.formatter_observations] == [
            (value,) for value in bound
        ]
        assert all(value is None or type(value) is Decimal for (value,) in database.bound_rows)
    assert all(
        row["payload"] is None or type(row["payload"]) is expected_type for row in database.rows
    )
    for (wire,), value in zip(database.wire_rows, bound):
        if value is None:
            assert wire == "NULL"
        elif not floating:
            assert Decimal(wire) == value
    if not floating:
        assert database.rows[0]["payload"] == Decimal(_EXACT)
        assert database.rows[1]["payload"] == Decimal("-" + _EXACT)
        if native.startswith("NUMERIC("):
            assert all(
                row["payload"] is None or row["payload"].as_tuple().exponent == -9
                for row in database.rows
            )
    else:
        packing = "!f" if native == "REAL" else "!d"
        assert [struct.pack(packing, row["payload"]) for row in database.rows[-2:]] == [
            struct.pack(packing, -0.0),
            struct.pack(packing, 0.0),
        ]
    assert [kind for kind, _statement in database.submissions] == ["CLEAR", "INSERT"]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert database.opened
    _closed(database)


@pytest.mark.parametrize("native", ["REAL", "DOUBLE PRECISION"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_numeric_text_native_signed_zero_preserves_exact_physical_ieee_bits(
    monkeypatch, native, explicit
):
    database = numeric_text_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series([-0.0, 0.0, None], dtype=object)})
    original = frame.copy(deep=True)
    packing = "!f" if native == "REAL" else "!d"
    expected_bits = [struct.pack(packing, -0.0), struct.pack(packing, 0.0), None]

    assert _load(frame, native if explicit else None) == 3

    assert database.schema == {"payload": native}
    assert len(database.rows) == 3
    assert [
        None if row["payload"] is None else struct.pack(packing, row["payload"])
        for row in database.rows
    ] == expected_bits
    _assert_float_transport(database, [-0.0, 0.0, None], native)
    assert len(database.formatter_observations) == 3
    first_wire = parse_one(database.wire_rows[0][0], read="postgres")
    assert isinstance(first_wire, exp.Cast)
    wire_type = first_wire.args["to"].sql(dialect="postgres").upper().replace(" ", "")
    assert wire_type in {"REAL", "FLOAT4", "DOUBLEPRECISION", "FLOAT8"}
    assert isinstance(first_wire.this, exp.Literal)
    assert first_wire.this.is_string
    assert first_wire.this.this == "-0.0"
    assert database.wire_rows[2] == ("NULL",)
    assert [kind for kind, _statement in database.submissions] == ["CLEAR", "INSERT"]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert [
        None if value is None else struct.pack(packing, value) for value in frame["payload"]
    ] == expected_bits
    assert database.opened
    _closed(database)


def _reject_before_mutation(database, frame, schema, capsys):
    target = database.target
    rows, native = deepcopy(target.rows), dict(target.schema)
    original = frame.copy(deep=True)

    with pytest.raises(
        ValueError, match=r"numeric|decimal|float|REAL|DOUBLE|NUMERIC|finite"
    ) as error:
        _load(frame, schema)

    output = capsys.readouterr()
    assert "private-numeric-value" not in str(error.value) + output.out + output.err
    assert database.target is target
    assert database.rows == rows
    assert database.schema == native
    assert database.live_mutations == []
    assert database.stage_inserts == []
    assert database.bound_rows == database.wire_rows == database.formatter_observations == []
    assert all(
        not isinstance(
            parse_one(database._ddl_schema_prefix(statement), read="postgres"),
            (exp.Create, exp.Drop, exp.Alter, exp.Insert, exp.Delete, exp.TruncateTable),
        )
        for statement in database.commands
    )
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    _closed(database)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    ("native", "bad"),
    [
        ("REAL", "private-numeric-value"),
        ("DOUBLE PRECISION", ""),
        ("NUMERIC(38, 9)", "1 2"),
        ("NUMERIC", "1e"),
    ],
    ids=["private-invalid", "empty", "interior-space", "incomplete-exponent"],
)
def test_gp_numeric_text_invalid_late_value_keeps_live_rows_and_never_creates_stage(
    monkeypatch, capsys, native, bad, explicit
):
    database = numeric_text_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series(["1.25", None, "-2.5", bad], dtype=object)})

    _reject_before_mutation(database, frame, native if explicit else None, capsys)


@pytest.mark.parametrize(
    ("native", "bad", "explicit"),
    [
        ("REAL", "3.5e38", False),
        ("REAL", "-3.5e38", True),
        ("DOUBLE PRECISION", "1e309", False),
        ("DOUBLE PRECISION", "-1e309", True),
        ("NUMERIC(38, 9)", "1e29", False),
        ("NUMERIC(38, 9)", "1.1234567891", True),
        ("NUMERIC", "NaN", False),
        ("NUMERIC", "Infinity", True),
        ("REAL", "NaN", False),
        ("DOUBLE PRECISION", "-Infinity", True),
        ("NUMERIC", "1e-16384", True),
        ("NUMERIC", "1e131072", False),
    ],
    ids=[
        "real-positive-overflow",
        "real-negative-overflow",
        "double-positive-overflow",
        "double-negative-overflow",
        "numeric-integral-overflow",
        "numeric-scale-overflow",
        "numeric-nan",
        "numeric-infinity",
        "real-nan",
        "double-infinity",
        "unbounded-scale-limit",
        "unbounded-integral-limit",
    ],
)
def test_gp_numeric_text_range_scale_and_nonfinite_reject_before_clear(
    monkeypatch, capsys, native, bad, explicit
):
    database = numeric_text_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series(["1.25", "-2.5", None, bad], dtype=object)})

    _reject_before_mutation(database, frame, native if explicit else None, capsys)


def test_gp_numeric_text_without_authority_remains_inferred_text(monkeypatch):
    database = numeric_text_target(monkeypatch, "NUMERIC", exists=False)
    values = ["1.25", "-2.5", _EXACT, None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, write_mode="append") == len(values)

    assert database.schema == {"payload": "TEXT"}
    assert database.rows == [{"payload": value} for value in values]
    assert database.bound_rows == [(value,) for value in values]
    assert database.wire_rows == [
        ("'1.25'",),
        ("'-2.5'",),
        ("'" + _EXACT + "'",),
        ("NULL",),
    ]
    assert [kind for kind, _statement in database.submissions] == ["DDL", "INSERT"]
    pd.testing.assert_frame_equal(frame, original)
    assert database.opened
    _closed(database)
