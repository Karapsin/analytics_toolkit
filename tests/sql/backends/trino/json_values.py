"""Authoritative JSON retains Trino's native parameter-cast semantics."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal
from enum import Enum
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.native_values import validate_transport_value
from analytics_toolkit.sql.backends.trino.json_values import (
    is_json_type,
    validate_json_profile,
)
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql._support.trino_binary_buffers import use_actual_formatter
from tests.sql._support.trino_parameters import render_parameter


def certify(value, native="JSON"):
    profile = ColumnProfile(native_type=native, source_backend="trino", temporal_type=native)
    profile.add(value)
    validate_profile(profile, native, "trino")
    resolution = ResolvedColumn(native, target_backend="trino")
    validate_converted_rows([(value,)], [resolution], backend="trino")
    return normalize_value(resolution.convert(value), native)


class Choice(Enum):
    TEXT = "not JSON"
    INTEGER = 7


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        ("not JSON", "'not JSON'"),
        ('{"a":1}', "'{\"a\":1}'"),
        ("null", "'null'"),
        ("", "''"),
        ("? ' 雪", "'? '' 雪'"),
        (None, "NULL"),
        (Decimal("NaN"), "NULL"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (2**63 - 1, str(2**63 - 1)),
        (-(2**63), str(-(2**63))),
        (Decimal("12345678901234567890.123456789"), "DECIMAL '12345678901234567890.123456789'"),
        (Decimal("1E+5"), "DECIMAL '100000'"),
        (Decimal("0.0001"), "DECIMAL '0.0001'"),
        (1.25, "DOUBLE '1.25'"),
        (float("inf"), "infinity()"),
        (dt.date(1900, 1, 1), "DATE '1900-01-01'"),
        (dt.datetime(2024, 1, 1, 12, 34, 56, 123456), "TIMESTAMP '2024-01-01 12:34:56.123456'"),  # noqa: DTZ001 -- native timestamp is naive.
        (Choice.TEXT, "'not JSON'"),
        (Choice.INTEGER, "7"),
        (np.int64(7), "7"),
        (np.bool_(True), "true"),
    ],
)
@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_json_casts_certify_native_scalar_parameters_without_parsing(monkeypatch, sdk, value, wire):
    use_actual_formatter(monkeypatch, sdk)
    result = certify(value)

    assert render_parameter(result) == wire


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        ([], "ARRAY[]"),
        ({}, "MAP(ARRAY[], ARRAY[])"),
        ([None, 1, 2], "ARRAY[NULL,1,2]"),
        (["not JSON", "{broken"], "ARRAY['not JSON','{broken']"),
        ({"item": [1, None, 2]}, "MAP(ARRAY['item'], ARRAY[ARRAY[1,NULL,2]])"),
        ({1: "one", 2: "two"}, "MAP(ARRAY[1,2], ARRAY['one','two'])"),
        (("value", 7), "ROW('value',7)"),
        (np.array([1, 2]), "ARRAY[1,2]"),
        ([0, Decimal("1E-38")], "ARRAY[0,DECIMAL '0.00000000000000000000000000000000000001']"),
        ([Decimal("0.5"), 0.5], "ARRAY[DECIMAL '0.5',DOUBLE '0.5']"),
        ([2**60, 0.5], f"ARRAY[{2**60},DOUBLE '0.5']"),
        ([float("nan"), 1], "ARRAY[nan(),1]"),
    ],
)
@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_json_casts_keep_native_container_parameters_and_common_types(
    monkeypatch, sdk, value, wire
):
    use_actual_formatter(monkeypatch, sdk)
    original = deepcopy(value)
    result = certify(value)

    assert render_parameter(result) == wire
    if isinstance(value, (list, tuple, dict)):
        assert value == original


@pytest.mark.parametrize(
    "value",
    [
        b"not JSON text",
        UUID("12345678-1234-5678-9abc-123456789abc"),
        dt.time(12, 34, 56),
        dt.timedelta(seconds=1),
        dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        pd.Timestamp("2024-01-01 00:00:00.123456789"),
        Decimal("1E+38"),
        Decimal("1E-39"),
        Decimal("Infinity"),
        2**63,
        -(2**63) - 1,
        [1, "one"],
        [True, 1],
        [1, Decimal("1E-38")],
        [2**53 + 1, 0.5],
        [Decimal("0.1"), 0.5],
        {None: "value"},
        {dt.date(2024, 1, 1): "value"},
        {"one": 1, "two": "two"},
        (),
        [Choice.TEXT],
        [np.int64(1)],
        [Decimal("NaN")],
        [pd.NA],
        "private\ud800value",
    ],
)
def test_json_casts_reject_incompatible_native_parameters_before_transport(value):
    with pytest.raises(ValueError, match="authoritative Trino JSON") as error:
        certify(value)

    assert "private" not in str(error.value)


@pytest.mark.parametrize("values", [[], [None], [None, None]])
def test_json_casts_allow_empty_and_all_sql_null_profiles(values):
    profile = ColumnProfile(native_type="JSON", source_backend="trino")
    for value in values:
        profile.add(value)

    assert validate_json_profile(profile, "JSON")
    assert validate_converted_rows(
        [(value,) for value in values],
        [ResolvedColumn("JSON", target_backend="trino")],
        backend="trino",
    )[0].null_count == len(values)


@pytest.mark.parametrize("value", [None, []])
def test_json_casts_invalid_type_arguments_reject_without_non_null_data(value):
    with pytest.raises(ValueError, match="does not accept type arguments"):
        certify(value, "JSON(1)")


def test_json_casts_certify_json_leaves_in_resolved_native_schema():
    native = "ROW(payload JSON, items ARRAY(JSON), objects MAP(VARCHAR, JSON))"
    value = ("not JSON", ["one", "two"], {"item": [1, 2]})

    validate_transport_value(value, native, "trino")
    with pytest.raises(ValueError, match="authoritative Trino JSON"):
        validate_transport_value(("value", [b"invalid"], {}), native, "trino")
    with pytest.raises(ValueError, match="authoritative Trino JSON"):
        validate_transport_value(("value", [1, "one"], {}), native, "trino")


@pytest.mark.parametrize("native", ["VARCHAR", "JSONB", '"JSON"'])
def test_json_casts_do_not_claim_other_native_type_families(native):
    assert not is_json_type(native)
    assert not validate_json_profile(ColumnProfile(), native)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    ("value", "native", "wire"),
    [
        (Decimal("1E+5"), None, "DECIMAL '100000'"),
        (Decimal("1E-38"), None, "DECIMAL '0.00000000000000000000000000000000000001'"),
        (Decimal("1E+5"), "DECIMAL(6,0)", "DECIMAL '100000'"),
        (Decimal("1E-38"), "DECIMAL(38,38)", "DECIMAL '0.00000000000000000000000000000000000001'"),
    ],
)
def test_json_casts_decimal_transport_is_native_fixed_point_across_actual_sdks(
    monkeypatch, sdk, value, native, wire
):
    use_actual_formatter(monkeypatch, sdk)
    original = value.as_tuple()

    converted = normalize_value(value, native)

    assert isinstance(converted, Decimal)
    assert converted == value
    assert converted.as_tuple() == original
    assert value.as_tuple() == original
    assert render_parameter(converted) == wire


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize("native", [None, "JSON", "ARRAY(DECIMAL(38,38))"])
def test_json_casts_nested_decimal_transport_preserves_native_values_without_mutation(
    monkeypatch, sdk, native
):
    use_actual_formatter(monkeypatch, sdk)
    decimal = Decimal("1E-38")
    value = [decimal]

    converted = normalize_value(value, native)

    assert value == [Decimal("1E-38")]
    assert value[0] is decimal
    assert isinstance(converted[0], Decimal)
    assert converted[0].as_tuple() == decimal.as_tuple()
    assert (
        render_parameter(converted) == "ARRAY[DECIMAL '0.00000000000000000000000000000000000001']"
    )


@pytest.mark.parametrize("native", [None, "JSON", "DECIMAL(29,9)"])
def test_json_casts_already_fixed_native_decimal_retains_its_original_class_and_identity(native):
    value = Decimal("12345678901234567890.123456789")

    converted = normalize_value(value, native)

    assert converted is value
    assert type(converted) is Decimal


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_json_casts_declared_row_dictionary_preserves_json_string_and_sql_null(monkeypatch, sdk):
    use_actual_formatter(monkeypatch, sdk)
    native = "ROW(payload JSON, label VARCHAR)"
    value = {"payload": "not JSON", "label": None}
    original = value.copy()

    validate_transport_value(value, native, "trino")
    converted = normalize_value(value, native)

    assert converted == ("not JSON", None)
    assert render_parameter(converted) == "ROW('not JSON',NULL)"
    assert value == original


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_json_casts_transport_accepts_numpy_microsecond_calendar_without_precision_loss(
    monkeypatch, sdk
):
    use_actual_formatter(monkeypatch, sdk)
    value = np.datetime64("1970-01-01T00:00:00.123456", "us")

    validate_transport_value(value, "JSON", "trino")
    converted = normalize_value(value, "JSON")

    assert render_parameter(converted) == "TIMESTAMP '1970-01-01 00:00:00.123456'"
    assert value == np.datetime64("1970-01-01T00:00:00.123456", "us")


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_json_casts_microsecond_aligned_exact_timestamp_keeps_its_native_calendar(monkeypatch, sdk):
    use_actual_formatter(monkeypatch, sdk)
    value = ExactTemporal(123456000000, 12, wall_time=True)

    converted = certify(value)

    assert render_parameter(converted) == "TIMESTAMP '1970-01-01 00:00:00.123456'"
    assert value == ExactTemporal(123456000000, 12, wall_time=True)


@pytest.mark.parametrize(
    "value",
    [ExactTemporal(123456000000, 12, "UTC"), ExactTemporal(123456789012, 12, wall_time=True)],
    ids=["timezone-aware", "sub-microsecond"],
)
def test_json_casts_reject_exact_timestamps_without_a_supported_lossless_native_cast(value):
    with pytest.raises(ValueError, match="authoritative Trino JSON"):
        certify(value)


def test_json_casts_transport_rejects_null_map_keys_inside_a_declared_json_field():
    with pytest.raises(ValueError, match="authoritative Trino JSON") as error:
        validate_transport_value({"payload": {None: "private value"}}, "ROW(payload JSON)", "trino")

    assert "private value" not in str(error.value)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_json_casts_date_and_naive_timestamp_array_keep_the_native_common_type(monkeypatch, sdk):
    use_actual_formatter(monkeypatch, sdk)
    value = [
        dt.date(2024, 1, 1),
        dt.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=dt.timezone.utc).replace(tzinfo=None),
    ]
    original = value.copy()

    converted = certify(value)

    assert (
        render_parameter(converted)
        == "ARRAY[DATE '2024-01-01',TIMESTAMP '2024-01-02 03:04:05.123456']"
    )
    assert value == original
