"""Public Greenplum JSON loading retains native documents and SQL NULLs."""

from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.plans import SqlPlan
from psycopg2.extras import Json

from tests.sql._support.gp_json import json_target


def load(frame, **options):
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        retry_cnt=options.pop("retry_cnt", 1),
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
@pytest.mark.parametrize(
    "explicit_schema", [False, True], ids=["existing-schema", "explicit-schema"]
)
@pytest.mark.parametrize("write_mode", ["append", "truncate_insert"])
def test_native_json_containers_load_through_real_gp_execute_values(
    monkeypatch, native_type, explicit_schema, write_mode
):
    database = json_target(monkeypatch, native_type)
    original_rows = deepcopy(database.target.rows)
    values = [
        None,
        {"x": 1, "items": [True, None], "name": "é🙂"},
        [1, {"flag": False}],
        None,
        {"nested": ("tuple", 2)},
        [],
        None,
    ]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original_frame = frame.copy(deep=True)
    options = {"table_schema": {"payload": native_type}} if explicit_schema else {}

    assert load(frame, write_mode=write_mode, **options) == len(values)

    expected = deepcopy(values)
    expected[4] = {"nested": ["tuple", 2]}
    incoming = [{"payload": value} for value in expected]
    assert database.target.rows == (original_rows if write_mode == "append" else []) + incoming
    assert database.sql_null_rows == [
        (True,),
        (False,),
        (False,),
        (True,),
        (False,),
        (False,),
        (True,),
    ]
    assert [
        json.loads(value[0]) if value[0] is not None else None for value in database.serialized_rows
    ] == expected
    assert len(database.bound_rows) == len(values)
    assert len(database.live_mutations) == (1 if write_mode == "append" else 2)
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
def test_raw_json_text_is_unchanged_and_json_null_differs_from_sql_null(monkeypatch, native_type):
    database = json_target(monkeypatch, native_type)
    values = [None, '{"x":1}', '"hello"', "null", ' [1, {"x": 2}] ', None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})

    assert load(frame, write_mode="truncate_insert") == len(values)

    assert database.serialized_rows == [(value,) for value in values]
    assert database.target.rows == [
        {"payload": None},
        {"payload": {"x": 1}},
        {"payload": "hello"},
        {"payload": None},
        {"payload": [1, {"x": 2}]},
        {"payload": None},
    ]
    assert database.sql_null_rows == [(True,), (False,), (False,), (False,), (False,), (True,)]


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
def test_existing_psycopg_json_wrapper_preserves_json_null(monkeypatch, native_type):
    database = json_target(monkeypatch, native_type)
    frame = pd.DataFrame({"payload": pd.Series([None, Json(None), Json({"x": 1})], dtype=object)})

    assert load(frame, write_mode="truncate_insert") == 3

    assert database.target.rows == [{"payload": None}, {"payload": None}, {"payload": {"x": 1}}]
    assert database.sql_null_rows == [(True,), (False,), (False,)]


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
def test_nested_missing_values_uuid_and_tuples_keep_existing_json_normalization(
    monkeypatch, native_type
):
    database = json_target(monkeypatch, native_type)
    identifier = UUID("abcdefab-cdef-abcd-efab-cdefabcdefab")
    value = {"missing": [np.nan, pd.NA], "identifier": identifier, "tuple": (1, None)}
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert load(frame, write_mode="truncate_insert") == 1

    assert database.target.rows == [
        {"payload": {"missing": [None, None], "identifier": str(identifier), "tuple": [1, None]}}
    ]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"bad": {1, 2}},
        [1, {"bad": Decimal("1.2")}],
        {"bad": dt.date(2025, 1, 1)},
        {"bad": float("inf")},
        {"bad": float("-inf")},
        '{"bad":}',
        "NaN",
        "Infinity",
    ],
    ids=[
        "set",
        "decimal",
        "date",
        "positive-infinity",
        "negative-infinity",
        "malformed",
        "raw-NaN",
        "raw-infinity",
    ],
)
def test_invalid_json_serialization_rejects_before_clearing_seed_rows(
    monkeypatch, native_type, invalid
):
    database = json_target(monkeypatch, native_type)
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([{"ok": 1}, invalid], dtype=object)})
    original_frame = frame.copy(deep=True)

    with pytest.raises(ValueError, match="JSON"):
        load(frame, write_mode="truncate_insert")

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert database.bound_rows == []
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
def test_explicit_json_schema_creates_a_new_native_destination(monkeypatch, native_type):
    database = json_target(monkeypatch, native_type, exists=False)
    frame = pd.DataFrame({"payload": pd.Series([{"x": 1}, None], dtype=object)})

    assert load(frame, table_schema={"payload": native_type}) == 2

    assert database.target.schema == {"payload": native_type}
    assert database.target.rows == [{"payload": {"x": 1}}, {"payload": None}]
    assert len(database.bound_rows) == 2


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
def test_invalid_explicit_json_schema_values_do_not_create_a_new_table(monkeypatch, native_type):
    database = json_target(monkeypatch, native_type, exists=False)
    frame = pd.DataFrame({"payload": pd.Series([{"x": 1}, {"bad": {1}}], dtype=object)})

    with pytest.raises(ValueError, match="JSON"):
        load(frame, table_schema={"payload": native_type})

    assert database.tables == {}
    assert database.live_mutations == []
    assert database.bound_rows == []


@pytest.mark.parametrize("native_type", ["JSON", "JSONB"])
def test_explicit_json_dataframe_dry_run_opens_no_connection(monkeypatch, native_type):
    database = json_target(monkeypatch, native_type, exists=False)
    frame = pd.DataFrame({"payload": pd.Series([{"x": 1}, None], dtype=object)})

    assert isinstance(load(frame, table_schema={"payload": native_type}, dry_run=True), SqlPlan)

    assert database.opens == 0
    assert database.commands == []
    assert database.tables == {}


@pytest.mark.parametrize("planning_flag", ["dry_run", "return_sql"])
def test_json_preview_validates_a_custom_serializer_in_memory_without_connections(
    monkeypatch, planning_flag
):
    database = json_target(monkeypatch, "JSONB", exists=False)
    calls = []

    def preview_dumps(value):
        calls.append(value)
        return '{"preview":1}'

    value = Json({"x": 1}, dumps=preview_dumps)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert isinstance(
        load(frame, table_schema={"payload": "JSONB"}, **{planning_flag: True}), SqlPlan
    )

    assert database.opens == 0
    assert len(calls) == 1
    assert database.commands == []
    assert database.tables == {}


@pytest.mark.parametrize(
    "value", [r'{"s":"\u0000"}', {"s": "\x00"}], ids=["raw-escape", "structured"]
)
def test_jsonb_nul_rejects_before_clearing_seed_rows(monkeypatch, value):
    database = json_target(monkeypatch, "JSONB")
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match="JSONB"):
        load(frame, write_mode="truncate_insert")

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert database.bound_rows == []


def test_explicit_json_encoding_cannot_bypass_existing_jsonb_nul_constraints(monkeypatch):
    database = json_target(monkeypatch, "JSONB")
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": [r'{"s":"\u0000"}']})

    with pytest.raises(ValueError, match="JSONB"):
        load(frame, table_schema={"payload": "JSON"}, write_mode="truncate_insert")

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert database.bound_rows == []


def test_json_preserves_a_valid_escaped_nul_document(monkeypatch):
    database = json_target(monkeypatch, "JSON")
    raw = r'{"s":"\u0000"}'

    assert load(pd.DataFrame({"payload": [raw]}), write_mode="truncate_insert") == 1

    assert database.serialized_rows == [(raw,)]
    assert database.target.rows == [{"payload": {"s": "\x00"}}]


@pytest.mark.parametrize(
    ("native_type", "array"),
    [
        ("JSON[]", [{"x": 1}, None, [2, 3]]),
        ("JSONB[]", [{"x": 1}, None, [2, 3]]),
        ("JSON[][]", [[{"x": 1}], [None]]),
        ("JSONB[][]", [[{"x": 1}], [None]]),
    ],
)
def test_public_native_json_arrays_keep_sql_array_rank_and_json_element_values(
    monkeypatch, native_type, array
):
    catalog_type = native_type.replace("[][]", "[]")
    database = json_target(monkeypatch, catalog_type)
    values = [None, array, None, [], None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original_frame = frame.copy(deep=True)

    options = {"table_schema": {"payload": native_type}} if native_type.endswith("[][]") else {}
    assert load(frame, write_mode="truncate_insert", **options) == len(values)

    assert database.target.rows == [{"payload": value} for value in values]
    assert database.target.schema == {"payload": catalog_type}
    assert any(f"AS {native_type})" in statement for statement in database.commands)
    if native_type.endswith("[][]"):
        assert any("CAST(ARRAY[ARRAY[" in statement for statement in database.commands)
    assert database.sql_null_rows == [(True,), (False,), (True,), (False,), (True,)]
    pd.testing.assert_frame_equal(frame, original_frame)


def test_json_array_wrapper_null_string_and_sql_null_elements_remain_distinct(monkeypatch):
    database = json_target(monkeypatch, "JSONB[]")
    frame = pd.DataFrame({"payload": pd.Series([[Json(None), "null", None]], dtype=object)})

    assert load(frame, write_mode="truncate_insert") == 1

    assert database.target.rows == [{"payload": [None, "null", None]}]
    assert any("ARRAY['null','\"null\"',NULL]" in statement for statement in database.commands)


def test_custom_json_serializer_is_frozen_once_across_lost_acknowledgement_retry(monkeypatch):
    database = json_target(monkeypatch, "JSONB")
    original_rows = deepcopy(database.target.rows)
    database.lost_insert_acknowledgements = 1
    calls = []

    def changing_dumps(value):
        calls.append(value)
        if len(calls) > 1:
            message = "Custom JSON serializer was replayed."
            raise AssertionError(message)
        return '{"certified":1}'

    frame = pd.DataFrame(
        {"payload": pd.Series([Json({"source": 1}, dumps=changing_dumps)], dtype=object)}
    )
    original_frame = frame.copy(deep=True)

    assert load(frame, table_schema={"payload": "JSONB"}, write_mode="append", retry_cnt=2) == 1

    assert len(calls) == 1
    assert database.target.rows == [
        *original_rows,
        {"payload": {"certified": 1}},
        {"payload": {"certified": 1}},
    ]
    assert database.serialized_rows == [('{"certified":1}',), ('{"certified":1}',)]
    assert database.sql_null_rows == [(False,), (False,)]
    pd.testing.assert_frame_equal(frame, original_frame)


def test_failing_custom_json_serializer_rejects_before_clearing_seed_rows(monkeypatch):
    database = json_target(monkeypatch, "JSONB")
    original_rows = deepcopy(database.target.rows)
    calls = []

    def invalid_dumps(value):
        calls.append(value)
        message = "Deterministic custom JSON serialization failed."
        raise ValueError(message)

    frame = pd.DataFrame(
        {"payload": pd.Series([Json({"source": 1}, dumps=invalid_dumps)], dtype=object)}
    )

    with pytest.raises(ValueError, match="JSON"):
        load(frame, table_schema={"payload": "JSONB"}, write_mode="truncate_insert")

    assert len(calls) == 1
    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert database.bound_rows == []


@pytest.mark.parametrize("sql_null", [False, True], ids=["json-null-document", "SQL-NULL"])
def test_json_null_document_and_sql_null_obey_existing_not_null_constraint(monkeypatch, sql_null):
    database = json_target(monkeypatch, "JSONB")
    database.target.not_null.add("payload")
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([None if sql_null else Json(None)], dtype=object)})

    if sql_null:
        with pytest.raises(ValueError, match="NOT NULL"):
            load(frame, table_schema={"payload": "JSONB"}, write_mode="truncate_insert")
        assert database.target.rows == original_rows
        assert database.live_mutations == []
        assert database.bound_rows == []
    else:
        assert load(frame, table_schema={"payload": "JSONB"}, write_mode="truncate_insert") == 1
        assert database.target.rows == [{"payload": None}]
        assert database.serialized_rows == [("null",)]
        assert database.sql_null_rows == [(False,)]


@pytest.mark.parametrize(
    ("native_type", "explicit_type"),
    [
        ("JSON", "pg_catalog.json"),
        ("JSONB", "Pg_Catalog.JSONB"),
        ("JSON", '"pg_catalog"."json"'),
        ("JSONB", '"pg_catalog"."jsonb"'),
    ],
)
def test_qualified_builtin_json_schema_keeps_native_container_semantics(
    monkeypatch, native_type, explicit_type
):
    database = json_target(monkeypatch, native_type)
    frame = pd.DataFrame({"payload": pd.Series([{"x": 1}, None], dtype=object)})

    assert load(frame, table_schema={"payload": explicit_type}, write_mode="truncate_insert") == 2

    assert database.target.schema == {"payload": native_type}
    assert database.target.rows == [{"payload": {"x": 1}}, {"payload": None}]
    assert database.sql_null_rows == [(False,), (True,)]


@pytest.mark.parametrize("explicit_type", ['"pg_catalog"."JSONB"', "other_schema.jsonb"])
def test_nonbuiltin_json_named_type_is_not_treated_as_builtin_before_mutation(
    monkeypatch, explicit_type
):
    database = json_target(monkeypatch, "JSONB")
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([{"x": 1}], dtype=object)})

    with pytest.raises(ValueError, match=r"authoritative|certify"):
        load(frame, table_schema={"payload": explicit_type}, write_mode="truncate_insert")

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert database.bound_rows == []
