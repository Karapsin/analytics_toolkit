"""Bounded catalog identity is distinct from SQL grammar aliases and domains."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.gp.metadata_types import format_gp_information_schema_type
from analytics_toolkit.sql.backends.gp.type_identity import (
    canonical_builtin_type,
    validate_builtin_profile,
)
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.type_resolution import ResolvedColumn, validate_profile


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bool", "BOOLEAN"),
        ("BOOLEAN", "BOOLEAN"),
        ("int2", "SMALLINT"),
        ("smallint", "SMALLINT"),
        ("INT4", "INTEGER"),
        ("int", "INTEGER"),
        ("integer", "INTEGER"),
        ("int8", "BIGINT"),
        ("bigint", "BIGINT"),
        ("float4", "REAL"),
        ("real", "REAL"),
        ("float8", "DOUBLE PRECISION"),
        ("double   precision", "DOUBLE PRECISION"),
        ("float", "DOUBLE PRECISION"),
        ("FLOAT(1)", "REAL"),
        ("float(24)", "REAL"),
        ("float(25)", "DOUBLE PRECISION"),
        ("float(53)", "DOUBLE PRECISION"),
        ("numeric", "NUMERIC"),
        ("decimal", "NUMERIC"),
        ("dec(5,2)", "NUMERIC(5, 2)"),
        ("numeric(5)", "NUMERIC(5, 0)"),
        ("decimal(5,2)", "NUMERIC(5, 2)"),
        ("NUMERIC(1000,1000)", "NUMERIC(1000, 1000)"),
        ("numeric(0005,+02)", "NUMERIC(5, 2)"),
        ("CHAR", "CHARACTER(1)"),
        ("character", "CHARACTER(1)"),
        ("char(5)", "CHARACTER(5)"),
        ("BPCHAR", "VARCHAR"),
        ("bpchar(5)", "CHARACTER(5)"),
        ("varchar", "VARCHAR"),
        ("character varying", "VARCHAR"),
        ("character varying(5)", "VARCHAR(5)"),
        ("char varying(5)", "VARCHAR(5)"),
        ("nchar", "CHARACTER(1)"),
        ("nchar(3)", "CHARACTER(3)"),
        ("nchar varying", "VARCHAR"),
        ("national char", "CHARACTER(1)"),
        ("national character(3)", "CHARACTER(3)"),
        ("national char varying", "VARCHAR"),
        ("national character varying(3)", "VARCHAR(3)"),
        ("varchar(10485760)", "VARCHAR(10485760)"),
        ("text", "TEXT"),
        ("date", "DATE"),
        ("time", "TIME"),
        ("time(6) without time zone", "TIME(6)"),
        ("time(3) with time zone", "TIME(3) WITH TIME ZONE"),
        ("timetz(3)", "TIME(3) WITH TIME ZONE"),
        ("timestamp", "TIMESTAMP"),
        ("timestamp(3) without time zone", "TIMESTAMP(3)"),
        ("timestamp(6) with time zone", "TIMESTAMP(6) WITH TIME ZONE"),
        ("timestamptz(3)", "TIMESTAMP(3) WITH TIME ZONE"),
        ("interval", "INTERVAL"),
        ("interval(3)", "INTERVAL(3)"),
        ("interval day to second(3)", "INTERVAL DAY TO SECOND(3)"),
        ("interval(3) day to second", "INTERVAL DAY TO SECOND(3)"),
        ("interval(3) day", "INTERVAL(3) DAY"),
        ("bytea", "BYTEA"),
        ("UUID", "UUID"),
        ("json", "JSON"),
        ("jsonb", "JSONB"),
        ("int4 [ ] [ ]", "INTEGER[][]"),
        ("NUMERIC(5,2)[][]", "NUMERIC(5, 2)[][]"),
        ("_int4", "INTEGER[]"),
        ("pg_catalog._numeric(5)", "NUMERIC(5, 0)[]"),
        ("pg_catalog._bpchar", "VARCHAR[]"),
        ("pg_catalog._bpchar(5)", "VARCHAR(5)[]"),
        ("pg_catalog._char", '"char"[]'),
    ],
)
def test_recognized_builtin_domains_are_canonical_and_idempotent(raw, expected):
    assert canonical_builtin_type(raw) == expected
    assert canonical_builtin_type(expected) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("bool", "BOOLEAN"),
        ("int2", "SMALLINT"),
        ("int4", "INTEGER"),
        ("int8", "BIGINT"),
        ("float4", "REAL"),
        ("float8", "DOUBLE PRECISION"),
        ("numeric", "NUMERIC"),
        ("bpchar", "VARCHAR"),
        ("varchar", "VARCHAR"),
        ("text", "TEXT"),
        ("date", "DATE"),
        ("timestamp", "TIMESTAMP"),
        ("timestamptz", "TIMESTAMP WITH TIME ZONE"),
        ("time", "TIME"),
        ("timetz", "TIME WITH TIME ZONE"),
        ("interval", "INTERVAL"),
        ("bytea", "BYTEA"),
        ("uuid", "UUID"),
        ("json", "JSON"),
        ("jsonb", "JSONB"),
        ("char", '"char"'),
        ("_int4", "INTEGER[]"),
    ],
)
@pytest.mark.parametrize("form", ["qualified", "quoted", "quoted_qualified", "folded_schema"])
def test_only_actual_lowercase_catalog_identities_are_recognized(name, expected, form):
    raw = {
        "qualified": f"pg_catalog.{name}",
        "quoted": f'"{name}"',
        "quoted_qualified": f'"pg_catalog" . "{name}"',
        "folded_schema": f"PG_CATALOG.{name.upper()}",
    }[form]
    assert canonical_builtin_type(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "other.int4",
        '"PG_CATALOG".int4',
        '"pg_catalog"."INT4"',
        '"integer"',
        '"boolean"',
        '"decimal"',
        '"character"',
        '"CHAR"',
        '"pg_catalog.int4"',
        "pg_catalog.integer",
        "pg_catalog.boolean",
        "pg_catalog.decimal",
        "pg_catalog.dec",
        '"dec"',
        "pg_catalog.real",
        "pg_catalog.double precision",
        "pg_catalog.uuid PRECISION",
        '"uuid" VARYING',
        "pg_catalog.SELECT",
        "pg_catalog.JOIN",
        "other._int4",
        "_arbitrary_custom_type",
        "uuid AS alias",
        "int4; DROP TABLE other",
        "numeric(5) WITH extra",
        "custom(5,2)",
        "",
        "_unknown",
        "pg_catalog._unknown",
        "custom.domain.int4",
        "pg_catalog.interval(3)",
        '"interval"(3)',
        '"pg_catalog"."interval"(32767,3)',
        "_interval(3)",
        "int4 -- comment",
        "int4 /* comment */",
    ],
)
def test_custom_names_aliases_and_non_declarations_are_not_guessed(raw):
    assert canonical_builtin_type(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "numeric()",
        "numeric(0)",
        "numeric(-1)",
        "numeric(1001)",
        "numeric(5,-1)",
        "numeric(5,6)",
        "numeric(1,0,0)",
        "numeric(1.5)",
        "numeric(private_value)",
        "numeric(5)(2)",
        "char(0)",
        "varchar(-1)",
        "varchar(10485761)",
        "varchar(1,2)",
        "integer(5)",
        "date(6)",
        "bool(1)",
        "uuid(1)",
        "json(1)",
        "float(0)",
        "float(54)",
        "float(1,2)",
        "float(+3)",
        "char(+3)",
        "time(+3)",
        "timestamp(+3)",
        "interval(+3)",
        "interval second(+3)",
        "real(3)",
        "time(1,2)",
        "pg_catalog.time(1,2)",
        "interval(1,2)",
        "interval month to year",
        "interval day to month",
        "interval day(3)",
        "interval(3) day to second(2)",
        "timetz with time zone",
        '"time" with time zone',
        "pg_catalog.timestamp without time zone",
        "pg_catalog._int4[]",
    ],
)
def test_invalid_typmods_of_known_types_fail_without_payload_values(raw):
    with pytest.raises(ValueError, match=r"Greenplum built-in type") as caught:
        canonical_builtin_type(raw)
    assert "private_value" not in str(caught.value)


@pytest.mark.parametrize(
    "fields",
    [
        "YEAR",
        "MONTH",
        "DAY",
        "HOUR",
        "MINUTE",
        "SECOND",
        "YEAR TO MONTH",
        "DAY TO HOUR",
        "DAY TO MINUTE",
        "DAY TO SECOND",
        "HOUR TO MINUTE",
        "HOUR TO SECOND",
        "MINUTE TO SECOND",
    ],
)
def test_interval_fields_preserve_the_native_field_contract(fields):
    assert canonical_builtin_type("interval " + fields.lower()) == "INTERVAL " + fields


@pytest.mark.parametrize("value", [None, "", "A", "\n", "\x7f"])
def test_internal_catalog_character_accepts_only_supported_single_byte_values(value):
    profile = ColumnProfile()
    profile.add(value)
    assert validate_builtin_profile(profile, '"char"') is True


@pytest.mark.parametrize("value", ["é", "ab", "\x00", "\ud800", b"A", 1])
def test_internal_character_rejects_incompatible_bytes_and_types(value):
    profile = ColumnProfile()
    profile.add(value)
    with pytest.raises(ValueError, match="internal byte character"):
        validate_builtin_profile(profile, '"char"')


@pytest.mark.parametrize("native", ["CHAR", "CHARACTER(1)", "VARCHAR", "custom.domain"])
def test_internal_character_policy_does_not_claim_other_native_types(native):
    assert validate_builtin_profile(ColumnProfile(), native) is False


def test_legacy_character_metadata_without_width_does_not_invent_char_one():
    # The compatibility formatter has no character_maximum_length argument.
    # A catalog lookup can recover a real width; this incomplete observation
    # must retain BPCHAR identity rather than assert SQL CHAR's default of one.
    native = format_gp_information_schema_type("character", "bpchar", None, None)
    value = "wider than one"
    profile = ColumnProfile(native_type=native, source_backend="gp")
    profile.add(value)

    assert native == "BPCHAR"
    assert canonical_builtin_type(native) == "VARCHAR"
    validate_profile(profile, native, "gp")
    assert ResolvedColumn(native, target_backend="gp").convert(value) == value
