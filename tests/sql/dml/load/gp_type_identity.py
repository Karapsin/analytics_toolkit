"""Recognized GP built-ins retain explicit alias and catalog identity support."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.gp.native_assignment import NativeAssignmentError
from sqlglot import exp

from tests.sql._support.load_encoded_values import configure_load


@pytest.mark.parametrize(
    "case",
    [
        ("BPCHAR(5)", "CHARACTER(5)", "abc", "abc"),
        ("pg_catalog.int4", "INTEGER", 7, 7),
        ("pg_catalog.numeric(10,2)", "NUMERIC(10,2)", Decimal("1.23"), Decimal("1.23")),
        ("pg_catalog.char", '"char"', "A", "A"),
        ('"char"', '"char"', "A", "A"),
        ("BPCHAR", "BPCHAR", "longer than one", "longer than one"),
        ("CHAR", "CHARACTER(1)", "é", "é"),
        ("pg_catalog.varchar(5)", "VARCHAR(5)", "é🙂", "é🙂"),
        ("pg_catalog.bool", "BOOLEAN", True, True),
        ("pg_catalog.float8", "DOUBLE PRECISION", 1.25, 1.25),
        ("NUMERIC", "NUMERIC", 1.25, Decimal("1.25")),
        ("NUMERIC(5)", "NUMERIC(5,0)", Decimal(12345), Decimal(12345)),
        ("pg_catalog.bytea", "BYTEA", b"\x00\xff", b"\x00\xff"),
        ("pg_catalog.date", "DATE", dt.date(1900, 1, 1), dt.date(1900, 1, 1)),
        (
            "pg_catalog.timestamp(6)",
            "TIMESTAMP(6)",
            dt.datetime.fromisoformat("2025-01-02T03:04:05.123456"),
            dt.datetime.fromisoformat("2025-01-02T03:04:05.123456"),
        ),
        ("pg_catalog.int4[]", "INTEGER[]", [1, None, 2], [1, None, 2]),
        ("pg_catalog._int4", "INTEGER[]", [1, None, 2], [1, None, 2]),
        ("pg_catalog.int4[][]", "INTEGER[][]", [[1, 2], [3, 4]], [[1, 2], [3, 4]]),
        ('pg_catalog."char"[]', '"char"[]', ["A", None, "B"], ["A", None, "B"]),
        (
            '"pg_catalog"."uuid"',
            "UUID",
            UUID("12345678-1234-5678-1234-567812345678"),
            "12345678-1234-5678-1234-567812345678",
        ),
    ],
)
@pytest.mark.parametrize("exists", [False, True], ids=["new", "existing"])
def test_explicit_mapped_builtin_identity_loads_with_real_psycopg_adaptation(
    monkeypatch, write_sql_connections, case, exists
):
    explicit, catalog_type, value, bound = case
    connection = configure_load(
        monkeypatch, write_sql_connections, exists=exists, schema={"value": catalog_type}
    )
    frame = pd.DataFrame({"value": pd.Series([None, value, None], dtype=object)})
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            frame,
            append=exists,
            table_schema={"value": explicit},
            retry_cnt=1,
            timeout_increment=0,
        )
        == 3
    )

    assert connection.bound_rows == [(None,), (bound,), (None,)]
    assert any(statement.lstrip().upper().startswith("INSERT") for statement in connection.executed)
    if not exists:
        assert any(f'"value" {explicit}' in statement for statement in connection.executed)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize(
    ("native_type", "value"),
    [
        ("NUMERIC(5)", Decimal(123456)),
        ("NUMERIC(5)", Decimal("123.1")),
        ("CHAR", "ab"),
        ("CHARACTER", "ab"),
    ],
)
@pytest.mark.parametrize("exists", [False, True])
def test_builtin_typmod_defaults_reject_overflow_before_any_write(
    monkeypatch, write_sql_connections, native_type, value, exists
):
    connection = configure_load(
        monkeypatch, write_sql_connections, exists=exists, schema={"value": native_type}
    )
    with pytest.raises(ValueError, match="authoritative"):
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series([value], dtype=object)}),
            table_schema={"value": native_type},
            write_mode="truncate_insert" if exists else "replace",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert connection.bound_rows == []
    assert connection.executed == []


@pytest.mark.parametrize("value", [Decimal("123.0"), 123.0])
def test_numeric_default_scale_accepts_exact_zero_fractional_alignment(
    monkeypatch, write_sql_connections, value
):
    connection = configure_load(
        monkeypatch, write_sql_connections, exists=True, schema={"value": "NUMERIC(5,0)"}
    )
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series([None, value, None], dtype=object)}),
            table_schema={"value": "NUMERIC(5)"},
            append=True,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 3
    )
    assert connection.bound_rows == [(None,), (Decimal(123),), (None,)]


@pytest.mark.parametrize("value", ["é", "ab", "\x00"])
def test_internal_character_byte_domain_rejects_before_clearing_existing_target(
    monkeypatch, write_sql_connections, value
):
    connection = configure_load(
        monkeypatch, write_sql_connections, exists=True, schema={"value": '"char"'}
    )
    with pytest.raises(ValueError, match="internal byte character"):
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": [value]}),
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert connection.bound_rows == []
    assert connection.executed == []


@pytest.mark.parametrize("native_type", ["NUMERIC(0)", "NUMERIC(5,6)", "CHAR(0)"])
@pytest.mark.parametrize("values", [[], [None]])
def test_invalid_known_typmods_do_not_pass_empty_or_all_null_preflight(
    monkeypatch, write_sql_connections, native_type, values
):
    connection = configure_load(
        monkeypatch, write_sql_connections, exists=False, schema={"value": native_type}
    )
    with pytest.raises(ValueError, match="Greenplum built-in type"):
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series(values, dtype=object)}),
            table_schema={"value": native_type},
            retry_cnt=1,
            timeout_increment=0,
        )
    assert connection.bound_rows == []
    assert connection.executed == []


@pytest.mark.parametrize(
    "native_type", ['"pg_catalog"."INT4"', "other.int4", '"integer"', "pg_catalog.integer"]
)
def test_unknown_or_distinct_quoted_type_never_gets_a_builtin_compatibility_bypass(
    monkeypatch, write_sql_connections, native_type
):
    connection = configure_load(
        monkeypatch,
        write_sql_connections,
        exists=True,
        schema={"value": "INTEGER"},
        missing_types=(native_type,),
    )
    resources = deepcopy(connection.table_rows)
    with pytest.raises(NativeAssignmentError, match="SQLSTATE 42704") as failure:
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": [7]}),
            table_schema={"value": native_type},
            append=True,
            retry_cnt=1,
            timeout_increment=0,
        )
    assert connection.bound_rows == []
    assert failure.value.sqlstate == "42704"
    assert failure.value.analytics_toolkit_sql_retry_safe is False
    assert connection.rejected_type_declarations == [native_type]
    assert connection.table_rows == resources
    statements = connection.catalog_statements
    assert len(statements) == len(connection.executed)
    private_creates = [statement for statement in statements if isinstance(statement, exp.Create)]
    assert len(private_creates) == 1
    assert private_creates[0].this.this.name != "encoded_target"
    assert all(
        not isinstance(
            statement, (exp.Insert, exp.Update, exp.Delete, exp.TruncateTable, exp.Drop, exp.Alter)
        )
        for statement in statements
    )


@pytest.mark.parametrize(
    "case",
    [
        ("DEC(5,2)", "NUMERIC(5,2)", Decimal("1.25")),
        ("CHAR VARYING(5)", "VARCHAR(5)", "abc"),
        ("NCHAR(2)", "CHARACTER(2)", "é🙂"),
        ("NATIONAL CHARACTER VARYING(5)", "VARCHAR(5)", "é🙂"),
    ],
)
def test_sql_grammar_aliases_keep_existing_numeric_and_character_domains(
    monkeypatch, write_sql_connections, case
):
    native_type, catalog_type, value = case
    connection = configure_load(
        monkeypatch, write_sql_connections, exists=True, schema={"value": catalog_type}
    )
    assert (
        sql.load_df(
            "encoded_gp",
            "public.encoded_target",
            pd.DataFrame({"value": pd.Series([value], dtype=object)}),
            table_schema={"value": native_type},
            append=True,
            retry_cnt=1,
            timeout_increment=0,
        )
        == 1
    )
    assert connection.bound_rows == [(value,)]
