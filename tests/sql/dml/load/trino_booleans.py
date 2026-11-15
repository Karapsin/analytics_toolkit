"""Explicit BOOLEAN casts do not turn integer inference into Boolean inference."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp, parse_one

from tests.sql._support.trino_booleans import native_boolean_target


def _load(frame, *, explicit):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **({"table_schema": {"payload": "BOOLEAN"}} if explicit else {}),
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
@pytest.mark.parametrize("kind", ["integer", "native-boolean"])
def test_trino_booleans_public_load_keeps_supported_native_casts_and_nulls(
    monkeypatch, explicit, kind
):
    database = native_boolean_target(monkeypatch)
    values = [None, 0, 1, None] if kind == "integer" else [None, False, True, None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, explicit=explicit)

    assert type(result) is int
    assert result == 4
    assert database.rows == [None, False, True, None]
    assert database.native_type == "BOOLEAN"
    assert database.table_types == {database.target: "BOOLEAN"}
    assert database.mutations == ["clear", "insert", "insert"]
    inserts = _insertions(database)
    assert len(inserts) == 2
    assert all("CAST(? AS BOOLEAN)" in statement for statement, _params, _wire in inserts)
    params = [value for _statement, values, _wire in inserts for value in values]
    wire = [value for _statement, _params, values in inserts for value in values]
    assert params[0] is None
    assert params[3] is None
    assert wire[0] == wire[3] == "NULL"
    for value, rendered, expected in zip(params[1:3], wire[1:3], [False, True]):
        assert type(value) in {int, bool}
        if type(value) is bool:
            assert value is expected
            assert rendered == ("true" if expected else "false")
        else:
            assert value == int(expected)
            assert rendered == str(int(expected))
    assert any(
        "information_schema.columns" in statement
        for statement, _params, _wire in database.statements
    )
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_trino_booleans_invalid_late_input_preserves_the_existing_target(monkeypatch, explicit):
    database = native_boolean_target(monkeypatch)
    before = deepcopy(database.table_rows)
    frame = pd.DataFrame(
        {"payload": pd.Series([0, 1, None, "private-invalid-boolean"], dtype=object)}
    )
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match=r"(?i)bool") as error:
        _load(frame, explicit=explicit)

    assert "private-invalid-boolean" not in str(error.value)
    assert database.table_rows == before
    assert database.table_types == {database.target: "BOOLEAN"}
    assert database.mutations == []
    assert _insertions(database) == []
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("dtype", [object, "Int64"])
def test_trino_boolean_zero_and_one_inference_retains_bigint(dtype):
    values = pd.Series([0, 1, None], dtype=dtype)
    original = values.copy(deep=True)

    assert get_backend_adapter("trino").infer_dataframe_column_type(values) == "BIGINT"

    pd.testing.assert_series_equal(values, original)
