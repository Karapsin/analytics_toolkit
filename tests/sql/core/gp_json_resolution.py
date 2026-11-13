"""Greenplum JSON documents retain their source and transport identities."""

import json
from enum import Enum

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.gp.json_values import JsonDocument
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    resolve_profile,
    validate_profile,
)
from analytics_toolkit.sql.dml.load.json_values import prepare_json_dataframe
from analytics_toolkit.sql.dml.load.type_validation import (
    _validated_dataframe_profiles,
    convert_dataframe_schema,
    validate_dataframe_schema,
)
from psycopg2.extras import Json


@pytest.mark.parametrize("native", ["JSON", "JSONB"])
@pytest.mark.parametrize("value", ["hello", "1", "true", "null", 2**63 + 1, False, 1.25])
def test_native_gp_json_scalar_sources_preserve_decoded_logical_identity(native, value):
    profile = ColumnProfile(native_type=native, source_backend="gp")
    profile.add(value)
    resolved = resolve_profile(
        profile,
        "gp",
        source_type=native,
        source_backend="gp",
        authoritative_type=native,
    )
    encoded = resolved.convert(value)
    assert isinstance(encoded, JsonDocument)
    assert json.loads(encoded) == value
    assert type(json.loads(encoded)) is type(value)
    assert validate_converted_rows([(value,)], [resolved], backend="gp")[0].count == 1


@pytest.mark.parametrize("native", ["JSON", "JSONB"])
def test_dataframe_raw_json_text_has_document_semantics_and_no_generic_text_envelope(native):
    frame = pd.DataFrame({"payload": ['  {"x": 1, "x": 2}  ', "1", '"1"', None]})
    assert validate_dataframe_schema(frame, {"payload": native}, "gp") is None
    converted, _metadata = convert_dataframe_schema(frame, {"payload": native}, "gp")
    assert converted["payload"].tolist() == frame["payload"].tolist()
    assert all(isinstance(value, JsonDocument) for value in converted["payload"][:-1])
    assert json.loads(converted.iloc[1, 0]) == 1
    assert json.loads(converted.iloc[2, 0]) == "1"
    profiles = _validated_dataframe_profiles(frame, {"payload": native}, "gp")
    assert profiles["payload"].null_count == 1


def test_non_json_source_metadata_does_not_turn_document_text_into_a_json_string():
    resolved = ResolvedColumn("JSONB", source_type="TEXT", source_backend="gp", target_backend="gp")
    assert json.loads(resolved.convert("1")) == 1


def test_json_array_documents_are_distinct_from_native_outer_dimensions():
    value = [{"values": [1, None]}, ["a", None], "text", None, Json(None)]
    frame = pd.DataFrame({"payload": [value, None]})
    profiles = _validated_dataframe_profiles(frame, {"payload": "JSONB[]"}, "gp")
    converted, _metadata = convert_dataframe_schema(frame, {"payload": "JSONB[]"}, "gp")
    documents = converted.iloc[0, 0]
    assert [None if item is None else json.loads(item) for item in documents] == [
        {"values": [1, None]},
        ["a", None],
        "text",
        None,
        None,
    ]
    assert documents[3] is None
    assert isinstance(documents[4], JsonDocument)
    assert documents[4] == "null"
    assert profiles["payload"].null_count == 1
    assert frame.iloc[0, 0] is value


def test_prepared_json_sql_dimensions_survive_collapsed_target_type_metadata():
    value = [[{"x": 1}, "one"], [None, [2, 3]]]
    frame = pd.DataFrame({"payload": [value]})
    prepared = prepare_json_dataframe(frame, {"payload": "JSONB[][]"}, "gp")
    assert prepared is not None
    profile = _validated_dataframe_profiles(prepared, {"payload": "JSONB[]"}, "gp")
    converted, _metadata = convert_dataframe_schema(prepared, {"payload": "JSONB[]"}, "gp")
    assert [
        [None if document is None else json.loads(document) for document in row]
        for row in converted.iloc[0, 0]
    ] == [[{"x": 1}, "one"], [None, [2, 3]]]
    assert profile["payload"].null_count == 0
    assert frame.iloc[0, 0] is value


def test_json_document_null_and_logical_sql_null_have_distinct_profiles():
    class Missing(Enum):
        NULL = None

    frame = pd.DataFrame({"payload": pd.Series([None, Missing.NULL, Json(None)], dtype=object)})
    prepared = prepare_json_dataframe(frame, {"payload": "JSONB"}, "gp")
    assert prepared is not None
    assert prepared["payload"].tolist() == [None, None, "null"]
    assert (
        _validated_dataframe_profiles(prepared, {"payload": "JSONB"}, "gp")["payload"].null_count
        == 2
    )
    assert frame.iloc[1, 0] is Missing.NULL


@pytest.mark.parametrize("native", ["JSON", "JSONB"])
def test_prepared_json_frame_keeps_custom_serializer_frozen(native):
    calls = []

    def dumps(_value):
        calls.append(True)
        if len(calls) != 1:
            pytest.fail("A certified custom JSON serializer must never run again.")
        return '{"frozen": true}'

    original = Json({"mutable": "input"}, dumps=dumps)
    frame = pd.DataFrame({"payload": [original], "id": pd.Series([1], dtype="int64")})
    frame.index = pd.Index([7], name="source_row")
    prepared = prepare_json_dataframe(frame, {"payload": native, "id": "BIGINT"}, "gp")
    assert prepared is not None
    for _attempt in range(2):
        validate_dataframe_schema(prepared, {"payload": native, "id": "BIGINT"}, "gp")
        converted, _metadata = convert_dataframe_schema(
            prepared, {"payload": native, "id": "BIGINT"}, "gp"
        )
        assert converted.iloc[0, 0] == '{"frozen": true}'
    assert calls == [True]
    assert frame.iloc[0, 0] is original
    assert prepared["id"].dtype == frame["id"].dtype
    assert prepared.index.equals(frame.index)


@pytest.mark.parametrize(
    ("backend", "schema"),
    [("trino", {"payload": "JSON"}), ("gp", {"payload": "TEXT"}), ("gp", None)],
)
def test_json_preparation_does_not_change_unrelated_native_paths(backend, schema):
    frame = pd.DataFrame({"payload": [{"unchanged": True}]})
    assert prepare_json_dataframe(frame, schema, backend) is None


@pytest.mark.parametrize(("backend", "native"), [("gp", "JSON_LIKE"), ("trino", "JSONB")])
def test_json_registration_does_not_whitelist_unknown_or_other_backend_types(backend, native):
    profile = ColumnProfile()
    profile.add({"x": 1})
    with pytest.raises(ValueError, match="Cannot certify"):
        validate_profile(profile, native, backend)


def test_invalid_raw_json_is_rejected_by_conversion_before_certification():
    frame = pd.DataFrame({"payload": ["hello"]})
    with pytest.raises(ValueError, match="JSON serialization"):
        validate_dataframe_schema(frame, {"payload": "JSONB"}, "gp")
    assert ResolvedColumn("TEXT", target_backend="gp").convert("hello") == "hello"
