from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.trino import insert
from analytics_toolkit.sql.core.exact_values import ExactTemporal, OrderedMap, OrderedMapPairs

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_parameters import DriverParameterConnection, render_parameter

ROW = (
    'ROW("event id" UUID, "at" TIMESTAMP(9) WITH TIME ZONE, '
    "amount DECIMAL(29,9), flags ARRAY(BIGINT))"
)
AMOUNT = Decimal("12345678901234567890.123456789")
IDENTIFIER = UUID("12345678-1234-5678-9abc-123456789abc")


def _named_row():
    return {
        "flags": [np.int64(2**53 + 1), pd.NA],
        "amount": AMOUNT,
        "at": pd.Timestamp("2024-01-01T03:04:05.123456789+03:00"),
        "event id": IDENTIFIER,
    }


def test_native_row_mapping_follows_declared_field_order_and_real_driver_format():
    normalized = insert.normalize_value(_named_row(), ROW)
    assert normalized == (
        IDENTIFIER,
        "2024-01-01 00:04:05.123456789 UTC",
        AMOUNT,
        [2**53 + 1, None],
    )
    assert (
        render_parameter(normalized) == "ROW(UUID '12345678-1234-5678-9abc-123456789abc',"
        "'2024-01-01 00:04:05.123456789 UTC',"
        "DECIMAL '12345678901234567890.123456789',ARRAY[9007199254740993,NULL])"
    )


def test_native_nested_array_map_row_values_use_the_installed_driver_contract():
    native = f"MAP(UUID, ARRAY({ROW}))"
    value = OrderedMapPairs(((IDENTIFIER, [None, _named_row(), pd.NA]),))
    normalized = insert.normalize_value(value, native)
    assert list(normalized) == [IDENTIFIER]
    assert normalized[IDENTIFIER][0] is None
    assert normalized[IDENTIFIER][1][2] == AMOUNT
    rendered = render_parameter(normalized)
    assert rendered.startswith("MAP(ARRAY[UUID '")
    assert "ARRAY[NULL,ROW(UUID '" in rendered
    assert "DECIMAL '12345678901234567890.123456789'" in rendered
    assert rendered.endswith(",NULL]])")


@pytest.mark.parametrize(
    "value",
    [
        OrderedMapPairs((("same", 1), ("same", 2))),
        OrderedMap(((None, 1),)),
        OrderedMapPairs(((pd.NA, 1),)),
        OrderedMapPairs((("1", 1), (1, 2))),
    ],
)
def test_invalid_native_map_keys_are_rejected_before_driver_submission(value):
    connection = DriverParameterConnection()
    with pytest.raises(ValueError, match="keys"):
        insert.insert_rows(
            get_backend("trino"),
            connection,
            "sandbox.target",
            ["value"],
            [(value,)],
            target_column_types={"value": "MAP(VARCHAR, BIGINT)"},
            trino_insert_chunk_size=1,
        )
    assert not connection.statements
    assert connection.cursor_close_calls == 1


@pytest.mark.parametrize("mapping", [{"wrong": 1}, {"event id": IDENTIFIER}])
def test_row_mapping_missing_or_extra_fields_is_rejected_without_losing_values(mapping):
    with pytest.raises(ValueError, match="nested schema"):
        insert.normalize_value(mapping, ROW)


@pytest.mark.parametrize("precision", [7, 8, 9, 12])
@pytest.mark.parametrize("aware", [False, True])
def test_high_precision_timestamp_binding_uses_exact_text_with_typed_cast(precision, aware):
    stamp = (
        pd.Timestamp("2024-01-01T03:04:05.123456789+03:00")
        if aware
        else pd.Timestamp("2024-01-01T03:04:05.123456789")
    )
    native = f"TIMESTAMP({precision})" + (" WITH TIME ZONE" if aware else "")
    connection = DriverParameterConnection()
    insert.insert_rows(
        get_backend("trino"),
        connection,
        "sandbox.target",
        ["at"],
        [(stamp,)],
        target_column_types={"at": native},
        trino_insert_chunk_size=1,
    )
    statement, params, rendered = connection.statements[0]
    digits = "123456789"[:precision].ljust(precision, "0")
    expected = f"2024-01-01 {'00' if aware else '03'}:04:05.{digits}" + (" UTC" if aware else "")
    assert params == [expected]
    assert rendered == [f"'{expected}'"]
    assert f"CAST(? AS {native})" in statement
    assert ".123456789" in insert.literal(
        stamp, "TIMESTAMP(9)" + (" WITH TIME ZONE" if aware else "")
    )


@pytest.mark.parametrize(
    "value",
    [
        ExactTemporal(-1, 9, "UTC"),
        pd.Timestamp("1969-12-31T23:59:59.999999999Z"),
        np.datetime64("1969-12-31T23:59:59.999999999"),
    ],
)
def test_exact_negative_epoch_timestamp_values_do_not_pass_through_microseconds(value):
    normalized = insert.normalize_value(value, "TIMESTAMP(9) WITH TIME ZONE")
    assert normalized.startswith("1969-12-31 23:59:59.999999999")
    assert "999999999" in render_parameter(normalized)
    assert "999999999" in insert.build_values_tuple(
        ["at"], [value], {"at": "TIMESTAMP(9) WITH TIME ZONE"}
    )


def test_legacy_sql_literal_paths_preserve_precise_nested_timestamps():
    stamp = pd.Timestamp("2024-01-01T00:00:00.123456789")
    assert insert.literal(stamp, None) == "TIMESTAMP '2024-01-01 00:00:00.123456789'"
    rendered = insert.build_values_tuple(["values"], [[(stamp, AMOUNT)]], None)
    assert (
        "ARRAY[ROW(TIMESTAMP '2024-01-01 00:00:00.123456789', "
        "DECIMAL '12345678901234567890.123456789')]" in rendered
    )
    with pytest.raises(ValueError, match="explicit TIMESTAMP precision"):
        insert.normalize_value(stamp, None)


def test_explicit_lower_precision_remains_deliberate_and_driver_safe():
    stamp = pd.Timestamp("2024-01-01T03:04:05.123456789+03:00")
    value = insert.normalize_value(stamp, "TIMESTAMP(3) WITH TIME ZONE")
    assert value == dt.datetime(2024, 1, 1, 0, 4, 5, 123000, tzinfo=dt.timezone.utc)
    assert render_parameter(value) == "TIMESTAMP '2024-01-01 00:04:05.123000 UTC'"
    assert (
        insert.literal(stamp, "TIMESTAMP(3) WITH TIME ZONE")
        == "TIMESTAMP '2024-01-01 00:04:05.123 UTC'"
    )


def test_public_load_uses_real_recursive_binding_for_authoritative_nested_schema(monkeypatch):
    native_map = f"MAP(UUID, ARRAY({ROW}))"
    schema = {"row": ROW, "mapping": native_map, "at": "TIMESTAMP(9) WITH TIME ZONE"}
    value = _named_row()
    frame = pd.DataFrame(
        {
            "row": pd.Series([pd.NA, value, None], dtype=object),
            "mapping": pd.Series(
                [None, OrderedMapPairs(((IDENTIFIER, [value, None]),)), pd.NA], dtype=object
            ),
            "at": pd.Series([None, value["at"], pd.NaT], dtype=object),
        }
    )
    connection = DriverParameterConnection()
    opened = []

    def open_connection(_alias):
        client = DriverParameterConnection()
        client.statements = connection.statements
        opened.append(client)
        return client

    monkeypatch.setattr(load_df_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        load_df_module, "get_table_column_types", lambda *_args, **_kwargs: dict(schema)
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_args, **_kwargs: None)
    result = sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="append",
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
    )
    assert result == 3
    assert len(connection.statements) == 2
    assert connection.statements[0][1][:3] == [None, None, None]
    assert connection.statements[0][1][3][0] == IDENTIFIER
    assert connection.statements[0][1][3][2] == AMOUNT
    assert connection.statements[0][1][5] == "2024-01-01 00:04:05.123456789 UTC"
    assert connection.statements[1][1] == [None, None, None]
    assert opened
    assert all(client.close_calls == 1 for client in opened)
    assert frame.iloc[1, 0]["at"].nanosecond == 789


def test_inferred_nested_rows_are_normalized_using_the_same_native_contract():
    adapter = get_backend("trino")
    values = pd.Series([(IDENTIFIER, [2**53 + 1, None], AMOUNT), None], dtype=object)
    native = adapter.infer_dataframe_column_type(values)
    assert native.startswith("ROW(")
    normalized = insert.normalize_value(values.iloc[0], native)
    assert normalized == (IDENTIFIER, [2**53 + 1, None], AMOUNT)
    assert "DECIMAL '12345678901234567890.123456789'" in render_parameter(normalized)


def test_untyped_native_containers_normalize_recursively_for_the_actual_driver():
    value = OrderedMapPairs((("row", (IDENTIFIER, [np.bool_(True), np.float32(1.5), pd.NA])),))
    normalized = insert.normalize_value(value, None)
    assert normalized == {"row": (IDENTIFIER, [True, 1.5, None])}
    assert (
        render_parameter(normalized)
        == "MAP(ARRAY['row'], ARRAY[ROW(UUID '12345678-1234-5678-9abc-123456789abc',"
        "ARRAY[true,DOUBLE '1.5',NULL])])"
    )
    assert insert.normalize_value(np.array([IDENTIFIER, pd.NA], dtype=object), None) == [
        IDENTIFIER,
        None,
    ]
    assert insert.normalize_value({"x": [1, None]}, "VARCHAR") == '{"x":[1,null]}'


@pytest.mark.parametrize(
    "value",
    [
        OrderedMapPairs(((None, 1),)),
        OrderedMapPairs((("same", 1), ("same", 2))),
        OrderedMapPairs(((("nested",), 1), (("nested",), 2))),
        OrderedMapPairs((([], 1),)),
    ],
)
def test_untyped_maps_validate_existing_profile_key_invariants(value):
    with pytest.raises(ValueError, match="keys"):
        insert.normalize_value(value, None)
    with pytest.raises(ValueError, match="keys"):
        insert.literal(value, None)


@pytest.mark.parametrize(
    "value",
    [
        ExactTemporal(-1, 9, "UTC"),
        np.datetime64("1969-12-31T23:59:59.999999999"),
        pd.Timestamp("1969-12-31T23:59:59.999999999"),
    ],
)
def test_date_and_microsecond_native_parameters_use_exact_intentional_conversions(value):
    date_value = insert.normalize_value(value, "DATE")
    assert date_value == dt.date(1969, 12, 31)
    assert render_parameter(date_value) == "DATE '1969-12-31'"
    micros = insert.normalize_value(value, "TIMESTAMP(6)")
    assert micros.microsecond == 999999
    assert insert.literal(value, "DATE") == "DATE '1969-12-31'"


def test_microsecond_aligned_exact_temporals_remain_valid_without_extra_type_inputs():
    for value in [ExactTemporal(1, 6), ExactTemporal(1000, 9)]:
        normalized = insert.normalize_value(value, None)
        assert normalized.isoformat() == "1970-01-01T00:00:00.000001"
        assert normalized.tzinfo is None
    assert insert.normalize_value(pd.Timestamp("2024-01-01T00:00:00.000001"), None).microsecond == 1


def test_typed_legacy_literals_render_nested_rows_maps_and_exact_temporal_values():
    values = OrderedMapPairs(((IDENTIFIER, [_named_row(), None]),))
    native = f"MAP(UUID, ARRAY({ROW}))"
    rendered = insert.literal(values, native)
    assert rendered.startswith("CAST(MAP(ARRAY[UUID '")
    assert "TIMESTAMP '2024-01-01 00:04:05.123456789 UTC'" in rendered
    assert "DECIMAL '12345678901234567890.123456789'" in rendered
    assert rendered.endswith(f" AS {native})")
    positional = insert.literal((IDENTIFIER, [1, None]), 'ROW("id" UUID, "counts" ARRAY(BIGINT))')
    assert "ROW(UUID '" in positional
    assert "ARRAY[1, NULL]" in positional


@pytest.mark.parametrize(
    ("value", "native", "expected"),
    [
        (memoryview(b"\x00\xff"), "VARBINARY", "X'00ff'"),
        ([1, None], "JSON", "CAST(ARRAY[1, NULL] AS JSON)"),
        ((1, None), "JSON", "CAST(ROW(1, NULL) AS JSON)"),
        ({"x": 1}, None, "MAP(ARRAY['x'], ARRAY[1])"),
        (np.array([1, 2]), None, "ARRAY[1, 2]"),
    ],
)
def test_legacy_literals_preserve_native_container_and_binary_parameter_semantics(
    value, native, expected
):
    assert insert.literal(value, native) == expected


def test_untyped_enum_parameters_follow_their_native_underlying_value():

    class State(Enum):
        READY = "ready"

    assert insert.normalize_value(State.READY, None) == "ready"
    assert render_parameter(insert.normalize_value(State.READY, None)) == "'ready'"


def test_numpy_timestamp_lower_precision_is_applied_before_driver_rendering():
    value = np.datetime64("2024-01-01T00:00:00.123999999")
    normalized = insert.normalize_value(value, "TIMESTAMP(3)")
    assert normalized.microsecond == 123000
    assert render_parameter(normalized) == "TIMESTAMP '2024-01-01 00:00:00.123000'"


def test_map_key_collisions_from_the_actual_driver_conversion_are_not_silently_deduplicated():
    value = OrderedMapPairs((((1,), 10), ("(1,)", 20)))
    with pytest.raises(ValueError, match="keys"):
        insert.normalize_value(value, "MAP(VARCHAR, BIGINT)")
