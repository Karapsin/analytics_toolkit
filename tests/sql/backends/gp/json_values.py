"""Certify immutable JSON documents with the actual psycopg2 adaptation path."""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.json_values import (
    JsonDocument,
    convert_json_value,
    is_json_type,
    validate_json_profile,
)
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from psycopg2.extensions import adapt
from psycopg2.extras import Json

from tests.sql._support.gp_arrays import AdaptedConnection


class _Logical(Enum):
    NULL = None
    TEXT = "text"
    INTEGER = 3


@pytest.mark.parametrize("native", ["JSON", "jsonb", " JSONB [] ", "JSON[][]"])
def test_gp_json_native_type_recognition_is_narrow(native):
    assert is_json_type(native)
    assert not is_json_type("VARCHAR")
    assert not is_json_type("JSONISH[]")


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("pg_catalog.json", {"x": 1}),
        ("PG_CATALOG.JSONB", {"x": 1}),
        ('"pg_catalog"."jsonb"', {"x": 1}),
        ('"jsonb"', {"x": 1}),
        ("pg_catalog.json[]", [{"x": 1}]),
        ('"pg_catalog"."jsonb"[][]', [[{"x": 1}]]),
    ],
)
def test_gp_qualified_builtin_json_types_use_the_actual_driver(native, value):
    assert is_json_type(native)
    converted = convert_json_value(value, native)
    connection = AdaptedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        "sandbox.documents",
        ["payload"],
        [[converted]],
        target_column_types={"payload": native},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert b'{"x": 1}' in connection.target.statements[0]
    assert connection.committed


@pytest.mark.parametrize(
    "native",
    [
        "other.jsonb",
        '"PG_CATALOG".jsonb',
        'pg_catalog."JSONB"',
        "pg_catalog.jsonb(3)",
        "pg_catalog.jsonb extra",
        "db.pg_catalog.jsonb",
        'pg_catalog."jsonb.extra"',
        "pg_catalog.",
        "pg_catalog.jsonb()",
        "pg_catalog.jsonb; other.jsonb",
    ],
)
def test_gp_json_qualified_recognition_preserves_identity_and_rejects_other_types(native):
    assert not is_json_type(native)


@pytest.mark.parametrize("native", ["JSON", "JSONB"])
def test_gp_json_structures_keep_default_dump_and_nested_normalization(native):
    identifier = UUID("12345678-1234-5678-1234-567812345678")
    source = {
        "x": 1,
        "values": (True, pd.NA, float("nan"), identifier, _Logical.TEXT, _Logical.INTEGER),
    }
    expected = json.dumps({"x": 1, "values": [True, None, None, str(identifier), "text", 3]})
    document = convert_json_value(source, native)
    assert isinstance(document, JsonDocument)
    assert document == expected
    assert adapt(document).getquoted() == adapt(expected).getquoted()
    assert source["values"][1] is pd.NA
    with pytest.raises(AttributeError):
        document.extra = "mutable"


@pytest.mark.parametrize("native", ["JSON", "JSONB"])
@pytest.mark.parametrize("document", ['  {"x":1,"x":2} \n', '"hello"', "null", "1.20e2"])
def test_gp_raw_json_documents_preserve_exact_text(native, document):
    frozen = convert_json_value(document, native)
    assert frozen == document
    assert convert_json_value(frozen, native, decoded=True) == document


@pytest.mark.parametrize("value", [None, pd.NA, pd.NaT, float("nan"), _Logical.NULL])
def test_gp_json_sql_nulls_remain_distinct_from_encoded_json_null(value):
    assert convert_json_value(value, "JSONB") is None
    assert convert_json_value(value, "JSONB[]") is None
    assert convert_json_value(Json(None), "JSONB") == "null"


@pytest.mark.parametrize("value", [(1, 2), 1, True, Decimal(1), date(2025, 1, 1), b"{}", {1}])
def test_gp_json_unsupported_top_level_values_fail_before_driver_adaptation(value):
    with pytest.raises(ValueError, match="authoritative Greenplum JSON serialization"):
        convert_json_value(value, "JSONB")


@pytest.mark.parametrize(
    "value",
    [
        {"x": {1}},
        {"x": Decimal("1.2")},
        {"x": date(2025, 1, 1)},
        {"x": float("inf")},
        "NaN",
        "Infinity",
        "-Infinity",
        '{"x":}',
        "[] trailing",
        '"\x00"',
        '"\ud800"',
    ],
)
def test_gp_json_invalid_serialization_is_sanitized(value):
    with pytest.raises(ValueError, match="Greenplum JSON serialization") as caught:
        convert_json_value(value, "JSON")
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "document",
    ['"\\u0000"', '"\\ud800"', '{"x":"\\u0000","x":1}', "1e131072", "1e-16384"],
)
def test_gp_json_to_jsonb_revalidates_frozen_documents_without_changing_them(document):
    frozen = convert_json_value(document, "JSON")
    assert frozen == document
    with pytest.raises(ValueError, match="authoritative Greenplum JSON serialization"):
        convert_json_value(frozen, "JSONB")
    with pytest.raises(ValueError, match="authoritative Greenplum JSON serialization"):
        convert_json_value([frozen], "JSONB[]")
    assert frozen == document


def test_gp_json_keeps_valid_numeric_tokens_beyond_python_decimal_exponent_domain():
    document = '{"value": 1e999999999999999999999}'
    assert convert_json_value(document, "JSON") == document
    with pytest.raises(ValueError, match="JSONB"):
        convert_json_value(document, "JSONB")


@pytest.mark.parametrize("document", ["0", "0.00", "1e131071", "1e-16383", '"\\ud83d\\ude00"'])
def test_gp_jsonb_accepts_numeric_and_unicode_domain_boundaries(document):
    assert convert_json_value(document, "JSONB") == document


@pytest.mark.parametrize("value", ["hello", "1", "null", 1, True, 1.25, [1, "x"], {"x": 1}])
def test_native_gp_json_source_values_use_decoded_document_semantics(value):
    assert convert_json_value(value, "JSONB", decoded=True) == json.dumps(value)


def test_gp_json_custom_serializer_is_frozen_once_and_remains_idempotent():
    calls = []

    def dumps(value):
        calls.append(value)
        return ' {"frozen":1} '

    source = Json({"mutable": []}, dumps=dumps)
    frozen = convert_json_value(source, "JSON")
    source.adapted["mutable"].append("later")
    assert convert_json_value(frozen, "JSONB") == ' {"frozen":1} '
    assert convert_json_value([frozen], "JSONB[]") == [' {"frozen":1} ']
    assert len(calls) == 1


def test_gp_json_custom_serializer_cannot_leak_private_failures_or_return_nontext():
    def broken(_value):
        message = "private-customer-value"
        raise RuntimeError(message)

    for serializer in (broken, lambda _value: None):
        with pytest.raises(
            ValueError, match="authoritative Greenplum JSON serialization"
        ) as caught:
            convert_json_value(Json({}, dumps=serializer), "JSONB")
        assert "private-customer-value" not in str(caught.value)
        assert caught.value.__suppress_context__


def test_gp_json_wrappers_keep_their_own_serializer_normalization_policy():
    with pytest.raises(ValueError, match="authoritative Greenplum JSON serialization"):
        convert_json_value(Json({"x": pd.NA}), "JSONB")
    assert convert_json_value({"x": pd.NA}, "JSONB") == '{"x": null}'


def test_gp_json_array_documents_keep_strings_json_null_and_sql_null_distinct():
    array = convert_json_value(["null", Json(None), None, {"x": 1}, [1, 2]], "JSONB[]")
    assert array == ['"null"', "null", None, '{"x": 1}', "[1, 2]"]
    assert all(isinstance(value, JsonDocument) for value in array if value is not None)
    connection = AdaptedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        "sandbox.documents",
        ["payload"],
        [[array]],
        target_column_types={"payload": "JSONB[]"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert (
        b"CAST(ARRAY['\"null\"','null',NULL,'{\"x\": 1}','[1, 2]'] AS JSONB[])"
        in connection.target.statements[0]
    )
    assert connection.committed


def test_gp_json_multidimensional_arrays_freeze_only_native_array_leaves():
    source = [[{"x": 1}, [1, 2]], [None, "text"]]
    result = convert_json_value(source, "JSON[][]")
    assert result == [['{"x": 1}', "[1, 2]"], [None, '"text"']]
    assert convert_json_value(result, "JSONB[][]") == result
    assert convert_json_value([], "JSONB[][]") == []
    assert convert_json_value([None], "JSONB[][]") == [None]
    assert convert_json_value([_Logical.NULL], "JSONB[]") == [None]


def test_gp_prepared_json_arrays_keep_native_dimensions_after_catalog_rank_erasure():
    matrix = [[{"x": 1}, None], [{"x": 2}, {"x": 3}]]
    frozen = convert_json_value(matrix, "JSONB[][]")
    converted = convert_json_value(frozen, "JSONB[]")
    assert converted == [['{"x": 1}', None], ['{"x": 2}', '{"x": 3}']]
    connection = AdaptedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        "sandbox.documents",
        ["payload"],
        [[converted]],
        target_column_types={"payload": "JSONB[]"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert (
        b"CAST(ARRAY[ARRAY['{\"x\": 1}',NULL],ARRAY['{\"x\": 2}','{\"x\": 3}']] AS JSONB[])"
        in connection.target.statements[0]
    )


@pytest.mark.parametrize(
    "value", [1, [[1], [1, 2]], [[{"x": 1}], [{"x": 2}, {"x": 3}]], [None, [1]]]
)
def test_gp_json_array_rank_and_ragged_shapes_reject_before_mutation(value):
    with pytest.raises(ValueError, match="authoritative Greenplum JSON serialization"):
        convert_json_value(value, "JSONB[][]")


def test_gp_json_array_dimension_limit_applies_to_actual_native_shape():
    source = {"x": 1}
    for _dimension in range(6):
        source = [source]
    assert convert_json_value(source, "JSONB" + "[]" * 6)
    with pytest.raises(ValueError, match="authoritative Greenplum JSON serialization"):
        convert_json_value([source], "JSONB" + "[]" * 7)
    assert convert_json_value([], "JSONB" + "[]" * 7) == []


def test_gp_json_profile_registration_never_certifies_unrelated_native_types():
    profile = ColumnProfile(families={"map", "array", "string"})
    assert validate_json_profile(profile, "JSONB")
    assert not validate_json_profile(profile, "UNSUPPORTED")
    assert validate_json_profile(ColumnProfile(families={"array"}), "JSON[]")
    for native, families in [("JSON", {"timestamp"}), ("JSON[]", {"map"})]:
        with pytest.raises(ValueError, match="authoritative Greenplum JSON values"):
            validate_json_profile(ColumnProfile(families=families), native)
    with pytest.raises(ValueError, match="JSON or JSONB destination"):
        convert_json_value({}, "VARCHAR")


def test_gp_json_document_certification_does_not_require_optional_driver_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2.extras", None)
    assert convert_json_value({"x": 1}, "JSONB") == '{"x": 1}'
