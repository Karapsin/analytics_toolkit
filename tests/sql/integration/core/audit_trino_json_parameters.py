"""Native Trino JSON parameter casts, with independent server-side controls."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import closing
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection

from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from typing import Any

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_NATIVE_ROW_RESULT = object()


def _label() -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        "trino-json-parameter-cast",
    )


def _cases() -> list[tuple[Any, str, Any]]:
    return [
        ("not JSON ? 'quoted' 雪", "VARCHAR 'not JSON ? ''quoted'' 雪'", "not JSON ? 'quoted' 雪"),
        ('{"a":1}', "VARCHAR '{\"a\":1}'", '{"a":1}'),
        ("null", "VARCHAR 'null'", "null"),
        (None, "NULL", None),
        (True, "BOOLEAN 'true'", True),
        (2**53 + 1, "BIGINT '9007199254740993'", 2**53 + 1),
        (
            Decimal("12345678901234567890.123456789"),
            "DECIMAL '12345678901234567890.123456789'",
            Decimal("12345678901234567890.123456789"),
        ),
        (
            Decimal("1E-38"),
            "DECIMAL '0.00000000000000000000000000000000000001'",
            Decimal("1E-38"),
        ),
        ([1, None, 2], "ARRAY[INTEGER '1', NULL, INTEGER '2']", [1, None, 2]),
        (
            {"a": 1, "b": None},
            "MAP(ARRAY[VARCHAR 'a', VARCHAR 'b'], ARRAY[INTEGER '1', NULL])",
            {"a": 1, "b": None},
        ),
        # Native ROW field-name behavior follows this query's engine settings;
        # the independent control deliberately supplies no invented JSON shape.
        ((1, "row", True), "ROW(INTEGER '1', VARCHAR 'row', BOOLEAN 'true')", _NATIVE_ROW_RESULT),
    ]


def _assert_pair(actual: Any, control: Any, value: Any, expected: Any) -> None:
    if value is None:
        assert actual is None
        assert control is None
        return
    assert isinstance(actual, str)
    assert isinstance(control, str)
    logical = json.loads(actual, parse_float=Decimal)
    native = json.loads(control, parse_float=Decimal)
    assert logical == native
    if expected is not _NATIVE_ROW_RESULT:
        assert type(logical) is type(expected)
        assert logical == expected


@pytest.mark.sql_scenario("audit.load.trino_json_parameters")
def test_trino_json_parameters_keep_native_cast_semantics_on_engine() -> None:
    cases = _cases()
    columns = []
    for index, (_value, control, _expected) in enumerate(cases):
        columns.extend(
            [f"CAST(? AS JSON) AS actual_{index}", f"CAST({control} AS JSON) AS native_{index}"]
        )
    # Only the actual side uses production normalization. Independent native
    # VARCHAR/numeric/container controls share the very same query settings.
    parameters = [normalize_value(value, "JSON") for value, _control, _expected in cases]
    query = f"/* {_label()} */ SELECT " + ", ".join(columns)
    with closing(get_sql_connection("trino_source_values")) as connection, closing(
        connection.cursor()
    ) as cursor:
        cursor.execute(query, parameters)
        rows = cursor.fetchall()
    assert len(rows) == 1
    assert len(rows[0]) == len(cases) * 2
    for index, (value, _control, expected) in enumerate(cases):
        _assert_pair(rows[0][index * 2], rows[0][index * 2 + 1], value, expected)
