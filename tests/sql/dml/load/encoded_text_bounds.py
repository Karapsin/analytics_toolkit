"""Authoritative dataframe bounds apply to the actual encoded outgoing values."""

from __future__ import annotations

import json
from decimal import Decimal
from enum import Enum

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.load_encoded_values import configure_load


class _State(Enum):
    READY = "a"
    LONG = "abcdefghijk"


class _StructuredValue(Enum):
    EMPTY = []  # noqa: RUF012 - the enum intentionally wraps a logical array.


class _FloatValue(Enum):
    ONE = 1.0


def test_plain_string_enum_uses_its_logical_value_for_bounded_text(
    monkeypatch, write_sql_connections
):
    schema = {"value": "VARCHAR(1)"}
    connection = configure_load(monkeypatch, write_sql_connections, exists=False, schema=schema)
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series([_State.READY, None], dtype=object)}),
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )
    assert connection.bound_rows == [("a",), (None,)]


def test_float_enum_preserves_explicit_integral_float_conversion(
    monkeypatch, write_sql_connections
):
    schema = {"value": "INTEGER"}
    connection = configure_load(monkeypatch, write_sql_connections, exists=False, schema=schema)
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series([_FloatValue.ONE, None], dtype=object)}),
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )
    assert connection.bound_rows == [(1,), (None,)]


@pytest.mark.parametrize("exists", [False, True], ids=["new_target", "existing_truncate"])
@pytest.mark.parametrize("native_type", ["VARCHAR(10)", "CHAR(10)", "CHARACTER VARYING(10)"])
@pytest.mark.parametrize(
    "value", [_StructuredValue.EMPTY, _State.LONG], ids=["typed_json", "long_string"]
)
def test_encoded_dataframe_overflow_rejects_before_any_target_mutation(
    monkeypatch, write_sql_connections, exists, native_type, value
):
    schema = {"value": native_type}
    connection = configure_load(monkeypatch, write_sql_connections, exists=exists, schema=schema)
    with pytest.raises(ValueError, match="authoritative text length"):
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series(["a", value], dtype=object)}),
            table_schema=schema,
            write_mode="truncate_insert" if exists else "replace",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert connection.executed == []
    assert connection.bound_rows == []


def test_encoded_dataframe_exact_bound_keeps_sql_null_and_typed_text(
    monkeypatch, write_sql_connections
):
    expected = json.dumps(
        {"v": 1, "backend": "", "type": "", "value": ["array", []]},
        separators=(",", ":"),
    )
    schema = {"value": f"VARCHAR({len(expected)})"}
    connection = configure_load(monkeypatch, write_sql_connections, exists=False, schema=schema)
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series([None, _StructuredValue.EMPTY], dtype=object)}),
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )
    assert connection.bound_rows == [(None,), (expected,)]


@pytest.mark.parametrize("native_type", ["VARCHAR(2)", "CHARACTER(2)"])
def test_encoded_dataframe_character_bounds_count_unicode_characters(
    monkeypatch, write_sql_connections, native_type
):
    schema = {"value": native_type}
    connection = configure_load(monkeypatch, write_sql_connections, exists=False, schema=schema)
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": ["é🙂"]}),
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 1
    )
    assert connection.bound_rows == [("é🙂",)]


def test_encoded_dataframe_validation_preserves_explicit_timestamp_narrowing(
    monkeypatch, write_sql_connections
):
    schema = {"value": "TIMESTAMP(3)"}
    connection = configure_load(monkeypatch, write_sql_connections, exists=False, schema=schema)
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame(
                {
                    "value": pd.Series(
                        [pd.Timestamp("2025-01-01T00:00:00.123456789"), None], dtype=object
                    )
                }
            ),
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )
    assert connection.bound_rows == [(pd.Timestamp("2025-01-01T00:00:00.123"),), (None,)]


def test_encoded_dataframe_validation_preserves_native_exact_numbers(
    monkeypatch, write_sql_connections
):
    number = Decimal("12345678901234567890.123456789")
    integer = 2**53 + 1
    schema = {"amount": "NUMERIC(29,9)", "id": "BIGINT"}
    connection = configure_load(monkeypatch, write_sql_connections, exists=False, schema=schema)
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame(
                {
                    "amount": pd.Series([number, None], dtype=object),
                    "id": pd.Series([integer, None], dtype=object),
                }
            ),
            table_schema=schema,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )
    assert connection.bound_rows == [(number, integer), (None, None)]
