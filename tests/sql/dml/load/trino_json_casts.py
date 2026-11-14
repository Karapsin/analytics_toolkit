"""Authoritative JSON columns retain native VARCHAR-to-JSON string casts."""

from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.trino_binary_buffers import use_actual_formatter
from tests.sql._support.trino_json_casts import NativeJsonCast, native_json_target


def _load(frame, **options):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **options,
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    "value",
    ["not JSON ? 'quoted' 雪", "", '{"a":1}', "[1,null]", "null", "line1\\line2\nnext"],
    ids=["arbitrary-varchar", "empty", "object-text", "array-text", "null-text", "slash-newline"],
)
def test_trino_json_casts_accept_arbitrary_varchar_without_parsing_document_text(
    monkeypatch, explicit, sdk, value
):
    use_actual_formatter(monkeypatch, sdk)
    database = native_json_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    options = {"table_schema": {"payload": "JSON"}} if explicit else {}

    assert _load(frame, **options) == 1

    assert database.rows == [json.dumps(value, ensure_ascii=False)]
    assert json.loads(database.rows[0]) == value
    assert database.mutations == ["clear", "insert"]
    assert "CAST(? AS JSON)" in database.statements[-1][0]
    assert database.statements[-1][2] == ["'" + value.replace("'", "''") + "'"]
    assert frame.iloc[0, 0] is value
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_trino_json_casts_sql_null_remains_distinct_from_json_string_null(monkeypatch, sdk):
    use_actual_formatter(monkeypatch, sdk)
    database = native_json_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series([None, "null", None], dtype=object)})

    assert _load(frame) == 3

    assert database.rows == [None, '"null"', None]
    assert json.loads(database.rows[1]) == "null"
    assert database.statements[-2][2] == ["NULL", "'null'"]
    assert database.statements[-1][2] == ["NULL"]
    assert database.mutations == ["clear", "insert", "insert"]
    pd.testing.assert_frame_equal(
        frame, pd.DataFrame({"payload": pd.Series([None, "null", None], dtype=object)})
    )


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    "case",
    [
        (True, "true", "true", True),
        (42, "42", "42", 42),
        (1.25, "DOUBLE '1.25'", "1.25", Decimal("1.25")),
        (
            Decimal("12345678901234567890.123456789"),
            "DECIMAL '12345678901234567890.123456789'",
            "12345678901234567890.123456789",
            Decimal("12345678901234567890.123456789"),
        ),
        (Decimal("1E+5"), "DECIMAL '100000'", "100000", Decimal("1E+5")),
        (
            Decimal("1E-38"),
            "DECIMAL '0.00000000000000000000000000000000000001'",
            "0.00000000000000000000000000000000000001",
            Decimal("1E-38"),
        ),
        (dt.date(2024, 1, 2), "DATE '2024-01-02'", '"2024-01-02"', "2024-01-02"),
        (
            dt.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=dt.timezone.utc).replace(tzinfo=None),
            "TIMESTAMP '2024-01-02 03:04:05.123456'",
            '"2024-01-02 03:04:05.123456"',
            "2024-01-02 03:04:05.123456",
        ),
        ([1, None, 2], "ARRAY[1,NULL,2]", "[1,null,2]", [1, None, 2]),
        (
            {"a": 1, "b": None},
            "MAP(ARRAY['a','b'], ARRAY[1,NULL])",
            '{"a":1,"b":null}',
            {"a": 1, "b": None},
        ),
        (
            {"number": Decimal("1E-38"), "missing": None},
            "MAP(ARRAY['number','missing'], "
            "ARRAY[DECIMAL '0.00000000000000000000000000000000000001',NULL])",
            '{"number":0.00000000000000000000000000000000000001,"missing":null}',
            {"number": Decimal("1E-38"), "missing": None},
        ),
        (
            {"number": Decimal("1E+5"), "missing": None},
            "MAP(ARRAY['number','missing'], ARRAY[DECIMAL '100000',NULL])",
            '{"number":100000,"missing":null}',
            {"number": Decimal("1E+5"), "missing": None},
        ),
    ],
    ids=[
        "boolean",
        "integer",
        "double",
        "decimal",
        "decimal-positive-exponent",
        "decimal-negative-exponent",
        "date",
        "naive-timestamp",
        "array",
        "map",
        "decimal-negative-exponent-map",
        "decimal-positive-exponent-map",
    ],
)
def test_trino_json_casts_supported_native_values_keep_original_cast_semantics(
    monkeypatch, sdk, case
):
    value, wire, native_json, logical = case
    use_actual_formatter(monkeypatch, sdk)
    database = native_json_target(monkeypatch, NativeJsonCast(wire, native_json))
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original = deepcopy(value)

    assert _load(frame, table_schema={"payload": "JSON"}) == 1

    assert database.rows == [native_json]
    assert json.loads(database.rows[0], parse_float=Decimal) == logical
    assert database.statements[-1][2] == [wire]
    assert "CAST(? AS JSON)" in database.statements[-1][0]
    assert database.native_type == "JSON"
    assert database.mutations == ["clear", "insert"]
    assert frame.iloc[0, 0] is value
    assert value == original


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "value",
    [
        b"\x00\xff",
        UUID("12345678-1234-5678-9abc-123456789abc"),
        dt.time(12, 30),
        dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc),
        [1, "different SQL type"],
        (),
    ],
    ids=["binary", "uuid", "clock", "zoned-timestamp", "heterogeneous-array", "empty-row"],
)
def test_trino_json_casts_unsupported_native_values_fail_before_target_clear(
    monkeypatch, explicit, value
):
    database = native_json_target(monkeypatch)
    original_rows = list(database.rows)
    options = {"table_schema": {"payload": "JSON"}} if explicit else {}
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match=r"JSON|json|native|cast"):
        _load(frame, **options)

    assert database.rows == original_rows
    assert database.statements == []
    assert database.mutations == []
    assert frame.iloc[0, 0] is value
    assert all(connection.close_calls == 1 for connection in database.opened)
