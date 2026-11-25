"""Authoritative integer casts preserve Trino's distinct conversion routes."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal, localcontext

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.trino.integer_values import (
    TrinoIntegerValueError,
    contains_integer_type,
    convert_integer_value,
    integer_text_value,
    integer_value,
    is_integer_type,
    normalize_authoritative_integer,
    validate_integer_type_profile,
)
from analytics_toolkit.sql.backends.trino.key_identity import scalar_key_identity
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    validate_profile,
)


@pytest.mark.parametrize("native", ["DOUBLE", "VARCHAR", "ARRAY(BIGINT)"])
def test_trino_sql_text_integer_cast_requires_an_integer_destination(native):
    with pytest.raises(TrinoIntegerValueError, match="native integer type") as failure:
        integer_text_value("42", native)

    assert failure.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize("value", [None, 42, Decimal(42), b"42"])
def test_trino_sql_text_integer_cast_does_not_coerce_nontext_input(value):
    with pytest.raises(TrinoIntegerValueError, match="authoritative integer schema") as failure:
        integer_text_value(value, "BIGINT")

    assert failure.value.analytics_toolkit_sql_retry_safe is False
    assert "from_sql" in str(failure.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("42", 42),
        (" +00042 ", 42),
        ("-42", -42),
        ("1_000", 1000),
        ("\u00a042\u00a0", 42),
        ("\U0001d7dc\U0001d7de", 46),
        (b"42", 42),
        (False, 0),
        (np.bool_(True), 1),
        (np.int64(-42), -42),
        (1.75, 1),
        (-1.75, -1),
        (Decimal("1.999999999999999999999999999999999999999999"), 1),
        (Decimal("-1.999999999999999999999999999999999999999999"), -1),
        (Decimal("1E-1000"), 0),
        (-(2**63), -(2**63)),
        (2**63 - 1, 2**63 - 1),
        (str(-(2**63)), -(2**63)),
        (str(2**63 - 1), 2**63 - 1),
    ],
)
def test_trino_bigint_retains_historical_python_int_conversion(value, expected):
    result = integer_value(value, "BIGINT")

    assert type(result) is int
    assert result == expected
    assert normalize_authoritative_integer(value, "BIGINT") == expected


def test_trino_bigint_decimal_conversion_is_exact_and_context_independent():
    class ExactDecimal(Decimal):
        def __float__(self):
            pytest.fail("BIGINT conversion used binary float")

    value = ExactDecimal("9223372036854775807.999999999999999999999999999999999999999")
    before = value.as_tuple()
    with localcontext() as context:
        context.prec = 2
        assert integer_value(value, "BIGINT") == 2**63 - 1

    assert value.as_tuple() == before


@pytest.mark.parametrize("value", ["1_000", "\u00a042\u00a0", "\U0001d7dc\U0001d7de"])
def test_trino_bigint_sql_text_cast_uses_native_grammar_instead_of_python_int(value):
    assert integer_value(value, "BIGINT") == int(value)

    with pytest.raises(TrinoIntegerValueError, match="integer range") as error:
        integer_text_value(value, "BIGINT")

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert value not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\x00\x1f +00042 \n", 42),
        ("\u0664\u0662", 42),
        (str(-(2**63)), -(2**63)),
        (str(2**63 - 1), 2**63 - 1),
    ],
)
def test_trino_native_bigint_text_cast_retains_exact_bounds_and_java_trim(value, expected):
    assert integer_text_value(value, "BIGINT") == expected


@pytest.mark.parametrize("value", [str(-(2**63) - 1), str(2**63), "1.0", "private-invalid"])
def test_trino_native_bigint_text_cast_rejects_bad_or_overflowing_values_privately(value):
    with pytest.raises(TrinoIntegerValueError, match="integer range") as error:
        integer_text_value(value, "BIGINT")

    assert value not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("native", "bits"),
    [("TINYINT", 8), ("SMALLINT", 16), ("INTEGER", 32), ("INT", 32)],
)
def test_trino_sibling_integer_casts_preserve_exact_native_widths(native, bits):
    minimum, maximum = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    for value in (minimum, maximum, str(minimum), "+" + str(maximum)):
        assert integer_value(value, native) == int(value)
    for value in (minimum - 1, maximum + 1, str(minimum - 1), str(maximum + 1)):
        with pytest.raises(TrinoIntegerValueError, match="integer range"):
            integer_value(value, native)


@pytest.mark.parametrize("native", ["TINYINT", "SMALLINT", "INTEGER", "INT"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\x00\x1f\t +00042 \r\n\x00", 42),
        ("-0", 0),
        ("+0", 0),
        ("\u0664\u0662", 42),
        ("0" * 5000 + "42", 42),
        (True, 1),
        (np.bool_(False), 0),
        (np.int16(42), 42),
    ],
)
def test_trino_sibling_text_matches_java_trim_and_decimal_digit_grammar(native, value, expected):
    result = integer_value(value, native)

    assert type(result) is int
    assert result == expected
    assert normalize_authoritative_integer(value, native) == expected


@pytest.mark.parametrize("native", ["TINYINT", "SMALLINT", "INTEGER", "INT"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "+",
        "-",
        "1_0",
        "1.0",
        "1e1",
        "0x10",
        "1 0",
        "\u00a042",
        "42\u00a0",
        "\U0001d7dc\U0001d7de",
        b"42",
        1.75,
        Decimal("1.75"),
    ],
)
def test_trino_sibling_integer_casts_do_not_borrow_python_bigint_parser(native, value):
    with pytest.raises(TrinoIntegerValueError, match="integer range") as error:
        integer_value(value, native)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "value",
    ["private-invalid-token", "42.5", 2**63, -(2**63) - 1, float("inf"), Decimal("Infinity")],
)
def test_trino_bigint_invalid_or_out_of_range_values_are_private_and_nonretryable(value):
    with pytest.raises(TrinoIntegerValueError, match="integer range") as error:
        integer_value(value, "BIGINT")

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert str(value) not in str(error.value)
    assert error.value.__cause__ is None
    assert "CAST the source expression" in str(error.value)
    assert "in from_sql" in str(error.value)


@pytest.mark.parametrize("native", ["BIGINT", "TINYINT", "SMALLINT", "INTEGER", "INT"])
@pytest.mark.parametrize("value", [None, pd.NA, float("nan"), Decimal("NaN")])
def test_trino_integer_assignment_retains_existing_null_policy(native, value):
    assert normalize_authoritative_integer(value, native) is None


@pytest.mark.parametrize("native", ["INTEGER", "SMALLINT", "TINYINT"])
def test_trino_sibling_profile_view_retains_existing_dataframe_integral_real_seam(native):
    value = np.float64(42.0)

    assert normalize_authoritative_integer(value, native) is value
    handled, converted = convert_integer_value(value, native)
    assert handled is False
    assert converted is value
    with pytest.raises(TrinoIntegerValueError, match="integer range"):
        integer_value(value, native)


@pytest.mark.parametrize(
    "native", ["BIGINT", "bigint", "  InTeGeR  ", "INT", "SMALLINT", "TINYINT"]
)
def test_trino_integer_type_checks_do_not_admit_raw_text_profiles_or_domain_proofs(native):
    assert is_integer_type(native) is True
    assert validate_integer_type_profile(native) is False
    profile = ColumnProfile()
    profile.add("42")

    with pytest.raises(ValueError, match="authoritative integer schema"):
        validate_profile(profile, native, "trino")

    assert declared_domain_fits("VARCHAR", native, "trino", source_backend="trino") is False


@pytest.mark.parametrize(
    "native",
    [
        "BIGINT(3)",
        "INTEGER()",
        "Nullable(SMALLINT)",
        "ARRAY(BIGINT(1))",
        "ROW(first BIGINT, second INTEGER(2))",
    ],
)
def test_trino_integer_known_invalid_declarations_reject_even_with_empty_input(native):
    with pytest.raises(TrinoIntegerValueError, match="unparameterized native integer"):
        validate_integer_type_profile(native)


def test_trino_integer_dispatch_preserves_unrelated_types_and_existing_boolean_date_casts():
    values = ["42", None]

    assert is_integer_type("int8") is False
    assert contains_integer_type("ARRAY(VARCHAR)") is False
    assert normalize_authoritative_integer(values, None) is values
    assert normalize_authoritative_integer(values, "ARRAY(VARCHAR)") is values
    handled, converted = convert_integer_value(values, "ARRAY(VARCHAR)")
    assert handled is False
    assert converted is values
    assert normalize_authoritative_integer("false", "BOOLEAN") is False
    assert normalize_authoritative_integer("2024-02-29", "DATE") == dt.date(2024, 2, 29)

    with pytest.raises(TrinoIntegerValueError, match="unparameterized native integer"):
        integer_value(42, "DOUBLE")


def test_trino_integer_recursive_casts_preserve_raw_caller_values_and_other_type_hooks():
    values = [("42", "false", "2024-02-29"), (None, None, None)]
    before = deepcopy(values)

    result = normalize_authoritative_integer(
        values, "ARRAY(ROW(id BIGINT, flag BOOLEAN, day DATE))"
    )

    assert result == [(42, False, dt.date(2024, 2, 29)), (None, None, None)]
    assert result is not values
    assert values == before


def test_trino_integer_converted_map_keys_reject_collisions_before_dict_construction():
    values = {"01": "one", "1": "another"}
    before = list(values.items())

    with pytest.raises(ValueError, match="map keys are not unique"):
        normalize_authoritative_integer(values, "MAP(BIGINT, VARCHAR)")

    assert list(values.items()) == before
    assert len(values) == 2


@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [("BIGINT", "1_000", 1000), ("BIGINT", -1.75, -1), ("SMALLINT", "\x00 +42\x1f", 42)],
)
def test_trino_integer_parameters_and_key_identities_use_the_same_native_cast(
    native, value, expected
):
    assert convert_integer_value(value, native) == (True, expected)
    assert normalize_value(value, native) == expected
    assert scalar_key_identity(value, native) == (True, expected)


@pytest.mark.parametrize("native", ["TINYINT", "SMALLINT", "INTEGER", "INT"])
def test_trino_sibling_key_identity_does_not_change_nontext_integral_float_compatibility(native):
    value = np.float64(42.0)

    assert scalar_key_identity(value, native) == (False, None)
