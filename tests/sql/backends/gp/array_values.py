from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.gp import (
    format_gp_information_schema_type,
    insert,
    metadata_types,
)
from psycopg2.extras import Json

from tests.sql._support.cross_area import RoutingDbapiConnection, table_info_module
from tests.sql._support.gp_arrays import AdaptedConnection
from tests.sql._support.load_table import load_df_module


def test_public_gp_table_info_retains_exact_array_element_typmods_and_declared_rank(monkeypatch):
    def resolve(statement, params):
        if statement == "SELECT to_regclass(%s)":
            return [("sandbox.arrays",)]
        assert "pg_catalog.format_type(a.atttypid, a.atttypmod)" in statement
        assert "a.attndims - 1" in statement
        assert params == ("sandbox", "arrays")
        return [
            ("amount", "numeric(38,9)[][]"),
            ("ids", "uuid[]"),
            ("names", "character varying(40)[]"),
            ("times", "timestamp(6) with time zone[]"),
            ("domain", '"custom.schema"."MixedCase"[]'),
        ]

    connection = RoutingDbapiConnection(resolve)
    monkeypatch.setattr(table_info_module, "get_sql_connection", lambda _alias: connection)
    result = sql.table_info("gp", "sandbox.arrays")
    assert result.columns == {
        "amount": "NUMERIC(38, 9)[][]",
        "ids": "UUID[]",
        "names": "VARCHAR(40)[]",
        "times": "TIMESTAMP(6) WITH TIME ZONE[]",
        "domain": '"custom.schema"."MixedCase"[]',
    }
    assert connection.close_calls == 1


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("integer[]", "INTEGER[]"),
        ("numeric(30,9)[][]", "NUMERIC(30, 9)[][]"),
        ("timestamp(3) without time zone[]", "TIMESTAMP(3)[]"),
        ("time(6) with time zone[]", "TIME(6) WITH TIME ZONE[]"),
        ("text", "TEXT"),
        ("bpchar", "BPCHAR"),
        ("bpchar[]", "BPCHAR[]"),
        ("bpchar(5)[]", "CHARACTER(5)[]"),
    ],
)
def test_gp_catalog_type_canonicalization_preserves_exact_native_shape(native, expected):
    assert metadata_types.canonical_catalog_type(native) == expected


@pytest.mark.parametrize(
    ("udt", "precision", "scale", "expected"),
    [
        ("_int4", None, None, "INTEGER[]"),
        ("_numeric", 18, 4, "NUMERIC(18, 4)[]"),
        ("_uuid", None, None, "UUID[]"),
        ("_bpchar", None, None, "BPCHAR[]"),
        ("_CaseType", None, None, '"CaseType"[]'),
    ],
)
def test_exported_gp_information_schema_array_formatter_remains_valid(
    udt, precision, scale, expected
):

    assert format_gp_information_schema_type("ARRAY", udt, precision, scale) == expected


@pytest.mark.parametrize("udt", [None, "_", "integer"])
def test_gp_array_metadata_without_an_element_type_is_rejected(udt):
    with pytest.raises(ValueError, match="catalog element type"):
        metadata_types.format_gp_information_schema_type("ARRAY", udt, None, None)


@pytest.mark.parametrize(
    ("oid", "native"),
    [
        (1000, "boolean[]"),
        (1001, "bytea[]"),
        (1005, "smallint[]"),
        (1007, "integer[]"),
        (1009, "text[]"),
        (1014, "bpchar[]"),
        (1015, "character varying[]"),
        (1016, "bigint[]"),
        (1021, "real[]"),
        (1022, "double precision[]"),
        (1115, "timestamp[]"),
        (1182, "date[]"),
        (1183, "time[]"),
        (1185, "timestamp with time zone[]"),
        (1231, "numeric[]"),
        (199, "json[]"),
        (2951, "uuid[]"),
        (3807, "jsonb[]"),
    ],
)
def test_gp_query_array_oids_have_native_types_instead_of_unknown_integer_codes(oid, native):
    adapter = get_backend("gp")
    cursor = SimpleNamespace(
        execute=lambda _sql: None,
        description=[("values", oid, None, None, None, None, None)],
        close=lambda: None,
    )
    columns = adapter.inspect_source_query_schema(
        SimpleNamespace(cursor=lambda: cursor), "SELECT values FROM source"
    )
    assert columns[0].native_type == native
    assert adapter.map_same_backend_source_type_to_target(columns[0]) == native


def test_gp_numeric_array_query_metadata_retains_available_precision_and_scale():
    assert get_backend("gp").type_code_name(1231, 30, 9) == "numeric(30,9)[]"


@pytest.mark.parametrize(("oid", "native"), [(18, '"char"'), (1042, "bpchar")])
def test_gp_character_query_oids_retain_distinct_unknown_width_identities(oid, native):
    assert get_backend("gp").type_code_name(oid, None, None) == native


def test_gp_array_insert_uses_actual_psycopg_adaptation_and_authoritative_type_casts():
    adapter = get_backend("gp")
    connection = AdaptedConnection()
    decimal = Decimal("12345678901234567890.123456789")
    identifier = UUID("12345678-1234-5678-9abc-123456789abc")
    frame = pd.DataFrame(
        {
            "amounts": pd.Series([[[decimal, pd.NA], [None, Decimal("0.0001")]]], dtype=object),
            "ids": pd.Series([[identifier, pd.NA]], dtype=object),
        }
    )
    adapter.insert_dataframe_batch(
        connection,
        '"sandbox"."arrays"',
        frame,
        target_column_types={"amounts": "NUMERIC(30,9)[][]", "ids": "UUID[]"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    statement = connection.target.statements[0]
    assert (
        b"CAST(ARRAY[ARRAY[12345678901234567890.123456789,NULL],"
        b"ARRAY[NULL,0.0001]] AS NUMERIC(30,9)[][])" in statement
    )
    assert b"CAST(ARRAY['12345678-1234-5678-9abc-123456789abc',NULL] AS UUID[])" in statement
    assert connection.committed
    assert connection.target.closed
    assert frame.iloc[0, 0][0][1] is pd.NA


def test_gp_nested_batch_normalization_keeps_exact_values_without_dataframe_inference():
    identifier = UUID(int=1)
    decimal = Decimal("12345678901234567890.123456789")
    frame = pd.DataFrame(
        {"values": pd.Series([[(identifier, pd.NA), [decimal, pd.NaT]], None], dtype=object)}
    )
    normalized = get_backend("gp").normalize_insert_batch(frame)
    assert normalized.iloc[0, 0] == [(str(identifier), None), [decimal, None]]
    assert normalized.iloc[1, 0] is None
    assert normalized.dtypes.iloc[0].name == "object"
    assert insert.normalize_insert_rows(object(), [[(), [], {"value": pd.NA}]])[0][2].adapted == {
        "value": None
    }


def test_gp_array_cast_template_escapes_percent_in_catalog_type_identifiers():
    assert (
        insert.array_parameter_template(["id", "values"], {"id": "BIGINT", "values": '"Type%s"[]'})
        == '(%s, CAST(%s AS "Type%%s"[]))'
    )
    assert insert.array_parameter_template(["id"], {"id": "BIGINT"}) is None


def test_gp_catalog_empty_metadata_and_query_failure_close_the_cursor():
    adapter = get_backend("gp")
    closed = []
    cursor = SimpleNamespace(
        execute=lambda *_args: None, fetchall=list, close=lambda: closed.append(True)
    )
    assert (
        adapter.get_table_column_types(
            SimpleNamespace(cursor=lambda: cursor), "sandbox.empty", connection_key="gp"
        )
        == {}
    )
    assert closed == [True]
    failure = ValueError("catalog failed")

    def execute(*_args):
        raise failure

    def close():
        closed.append(True)
        message = "close failed"
        raise OSError(message)

    cursor.execute = execute
    cursor.close = close
    with pytest.raises(ValueError, match="catalog failed") as caught:
        adapter.get_table_column_types(
            SimpleNamespace(cursor=lambda: cursor), "sandbox.empty", connection_key="gp"
        )
    assert caught.value is failure
    assert closed == [True, True]


def test_gp_null_detection_keeps_non_scalar_arrays_and_failed_custom_probes_native(monkeypatch):

    assert insert._is_null_like(np.array([1, 2])) is False
    message = "not a null-aware scalar"

    def incompatible(_value):
        raise TypeError(message)

    monkeypatch.setattr(insert.pd, "isna", incompatible)
    assert insert._is_null_like(object()) is False
    assert insert.array_parameter_template([], {"id": "BIGINT[]"}) is None


def test_gp_json_array_elements_use_actual_psycopg_json_adapters_without_reencoding_scalar_json():
    adapter = get_backend("gp")
    connection = AdaptedConnection()
    adapter.insert_rows_batch(
        connection,
        "sandbox.json_arrays",
        ["values", "matrix", "scalar"],
        [[[{"x": 1}, pd.NA], [[{"y": 2}], [None]], [{"z": 3}, None]]],
        target_column_types={"values": "JSONB[]", "matrix": "JSON[][]", "scalar": "JSONB"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    statement = connection.target.statements[0]
    assert b"CAST(ARRAY['{\"x\": 1}',NULL] AS JSONB[])" in statement
    assert b"CAST(ARRAY[ARRAY['{\"y\": 2}'],'{NULL}'] AS JSON[][])" in statement
    assert b"'[{\"z\": 3}, null]'" in statement
    assert connection.committed


def test_gp_json_array_shape_validation_happens_before_cursor_open_and_preserves_wrappers():

    wrapped = Json({"x": 1})
    normalized = insert.normalize_json_columns(
        ["values"], [[[wrapped, None]], [None]], {"values": "JSON[]"}
    )
    assert normalized[0][0] == [wrapped, None]
    assert normalized[1][0] is None
    with pytest.raises(TypeError, match="array rank"):
        insert.normalize_json_columns(["values"], [[1]], {"values": "JSON[]"})


@pytest.mark.parametrize("precision", [0, 65535])
def test_gp_unbounded_numeric_array_metadata_does_not_publish_a_sentinel_typmod(precision):
    assert get_backend("gp").type_code_name(1231, precision, 0) == "numeric[]"


def test_gp_array_custom_domain_percents_are_safe_through_actual_psycopg_formatting():
    connection = AdaptedConnection()
    get_backend("gp").insert_rows_batch(
        connection,
        '"sandbox%"."arrays%s"',
        ["value%"],
        [[["literal%value"]]],
        target_column_types={"value%": '"types"."value%kind"[]'},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=1,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    statement = connection.target.statements[0]
    assert b'INSERT INTO "sandbox%"."arrays%s" ("value%")' in statement
    assert b'CAST(ARRAY[\'literal%value\'] AS "types"."value%kind"[])' in statement
    assert b"%%" not in statement


@pytest.mark.parametrize("close_failure", [False, True])
def test_gp_writable_catalog_keeps_default_columns_and_exact_array_types_when_closing(
    close_failure,
):
    adapter = get_backend("gp")
    statements = []
    closed = []

    def close():
        closed.append(True)
        if close_failure:
            message = "cursor cleanup failed"
            raise OSError(message)

    cursor = SimpleNamespace(
        execute=lambda statement, params: statements.append((statement, params)),
        fetchall=lambda: [
            ("id", "bigint"),
            ("default value", "text"),
            ("amounts", "numeric(30,9)[][]"),
        ],
        close=close,
    )
    result = adapter.get_writable_table_column_types(
        SimpleNamespace(cursor=lambda: cursor), '"schema.dot"."table.dot"', connection_key="gp"
    )
    assert result == {"id": "bigint", "default value": "text", "amounts": "numeric(30,9)[][]"}
    statement, params = statements[0]
    assert params == ("schema.dot", "table.dot")
    assert "format_type(a.atttypid, a.atttypmod)" in statement
    assert "a.attndims - 1" in statement
    assert "COALESCE(row_to_json(a)->>'attgenerated', '') = ''" in statement
    assert "COALESCE(row_to_json(a)->>'attidentity', '') <> 'a'" in statement
    assert "default_expr" not in statement
    assert "ORDER BY a.attnum" in statement
    assert closed == [True]


@pytest.mark.parametrize("close_failure", [False, True])
def test_public_gp_upsert_catalog_failure_stops_before_stage_ddl_or_target_mutation(
    close_failure, monkeypatch
):

    failure = ValueError("writable metadata failed")
    statements = []
    opened = []
    cursor_closes = []
    created = []

    def open_connection(_alias):
        connection = SimpleNamespace(close_calls=0)
        opened.append(connection)

        def execute(statement, _params):
            statements.append(statement)
            raise failure

        def close_cursor():
            cursor_closes.append(True)
            if close_failure:
                message = "cursor cleanup failed"
                raise OSError(message)

        def close_connection():
            connection.close_calls += 1

        connection.cursor = lambda: SimpleNamespace(execute=execute, close=close_cursor)
        connection.close = close_connection
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        load_df_module, "create_stage_table", lambda **kwargs: created.append(kwargs)
    )
    with pytest.raises(ValueError, match="writable metadata failed") as caught:
        sql.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1], "value": ["x"]}),
            write_mode="upsert",
            key_columns="id",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert caught.value is failure
    assert len(statements) == 1
    assert statements[0].startswith("SELECT a.attname")
    assert not created
    assert cursor_closes == [True]
    assert opened
    assert all(connection.close_calls == 1 for connection in opened)
