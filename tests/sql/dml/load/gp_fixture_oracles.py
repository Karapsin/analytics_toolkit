"""Backend assignment fixtures must preserve the distinctions checked by public loads."""

from __future__ import annotations

import json
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, localcontext
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.gp.json_values import convert_json_value
from psycopg2.extras import Json
from sqlglot import exp

from tests.sql._support.gp_json import JsonConnection, JsonDatabase, json_target
from tests.sql._support.gp_numeric_text import NumericDatabase
from tests.sql.integration.core import audit_gp_json


def _load(values):
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    assert sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
    ) == len(values)


def _decimal_document(value):
    return format(value, "f")


@pytest.mark.parametrize("rounding", [ROUND_HALF_EVEN, ROUND_DOWN, ROUND_UP])
@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.25", "1.3"), ("-1.25", "-1.3"), ("1.24", "1.2"), ("-1.24", "-1.2")],
)
def test_numeric_assignment_oracle_rounds_half_away_from_zero_independent_of_context(
    rounding, value, expected
):
    with localcontext() as context:
        context.rounding = rounding
        actual = NumericDatabase.decode_value(exp.Literal.string(value), "NUMERIC(3,1)")
    assert actual == Decimal(expected)


@pytest.mark.parametrize("native", ["JSON", "JSONB", "JSON[]", "JSONB[]"])
def test_public_json_load_fixture_retains_distinct_exact_numeric_tokens(monkeypatch, native):
    texts = ["9007199254740993.0", "9007199254740992.0"]
    expected = [Decimal(text) for text in texts]
    database = json_target(monkeypatch, native)
    if native.endswith("[]"):
        values = [[Json(value, dumps=_decimal_document) for value in expected], None]
        stored = [expected, None]
    else:
        values = [*texts, None]
        stored = [*expected, None]
    _load(values)
    assert database.target.rows == [{"payload": value} for value in stored]
    numeric = (
        database.target.rows[0]["payload"]
        if native.endswith("[]")
        else [row["payload"] for row in database.target.rows[:2]]
    )
    assert all(isinstance(value, Decimal) for value in numeric)
    assert numeric[0] != numeric[1]


@pytest.mark.parametrize("document", ["1e131072", "-1e131072", "1e-16384", '{"x":[1e131072]}'])
def test_jsonb_assignment_oracle_rejects_numbers_outside_numeric_storage_domain(document):
    database = JsonDatabase("JSONB", exists=True)
    with pytest.raises(ValueError, match=r"(?i)numeric.*domain"):
        database.decode_value(exp.Literal.string(document), "JSONB")


@pytest.mark.parametrize("native", ["JSON", "JSONB"])
@pytest.mark.parametrize("document", ["1e131071", "1e-16383"])
def test_json_numeric_assignment_oracle_keeps_exact_supported_boundary_values(native, document):
    database = JsonDatabase(native, exists=True)
    assert database.decode_value(exp.Literal.string(document), native) == Decimal(document)


@pytest.mark.parametrize("document", ["1e131072", "1e-16384"])
def test_json_text_storage_is_not_limited_by_jsonb_numeric_range(document):
    database = JsonDatabase("JSON", exists=True)
    assert database.decode_value(exp.Literal.string(document), "JSON") == Decimal(document)


@pytest.mark.parametrize("native", ["JSON", "JSONB"])
def test_public_json_storage_oracle_counts_only_sql_null_as_null(monkeypatch, native):
    database = json_target(monkeypatch, native)
    _load([None, Json(None), Json("null")])
    cursor = JsonConnection(database).cursor()
    cursor.execute("SELECT COUNT(*) AS total, COUNT(payload) AS present FROM sandbox.events")
    assert cursor.fetchall() == [(3, 2)]
    cursor.execute("SELECT payload FROM sandbox.events WHERE payload IS NULL")
    assert cursor.fetchall() == [(None,)]
    cursor.execute("SELECT payload FROM sandbox.events WHERE payload IS NOT NULL")
    assert len(cursor.fetchall()) == 2
    assert database.target.rows[1]["payload"] is not None
    assert database.target.rows[2]["payload"] == "null"


@pytest.mark.parametrize("native", ["JSON[]", "JSONB[]"])
def test_public_json_array_storage_oracle_keeps_json_null_and_sql_null_elements_distinct(
    monkeypatch, native
):
    database = json_target(monkeypatch, native)
    _load([[Json(None), None, "null"]])
    elements = database.target.rows[0]["payload"]
    rows = [{"payload": value} for value in elements]
    assert database.aggregate(exp.Count(this=exp.column("payload")), rows) == 2
    condition = exp.Is(this=exp.column("payload"), expression=exp.Null())
    assert [database.matches(condition, row) for row in rows] == [False, True, False]
    assert elements[2] == "null"


def test_integration_json_matrix_seed_contains_json_null_string_null_and_sql_null(monkeypatch):
    frames = []

    def capture_load(_alias, _table, frame, **_options):
        frames.append(frame.copy(deep=True))
        return len(frame)

    monkeypatch.setattr(audit_gp_json.sql, "load_df", capture_load)
    monkeypatch.setattr(audit_gp_json.sql, "read", lambda *_args, **_kwargs: "jsonb[]")
    monkeypatch.setattr(audit_gp_json, "_check_matrix", lambda *_args: None)
    registry = SimpleNamespace(table=lambda _alias, name: name)
    audit_gp_json._assert_native_jsonb_matrix("gp", registry)
    assert len(frames) == 2
    for frame in frames:
        matrix = convert_json_value(frame["payload"].iloc[0], "JSONB[][]")
        assert matrix[0][1] is None
        assert json.loads(str(matrix[1][0])) is None
        assert json.loads(str(matrix[1][1])) == "null"
        assert len(matrix) == 2
        assert all(len(row) == 3 for row in matrix)
        assert frame["payload"].iloc[1] is None
