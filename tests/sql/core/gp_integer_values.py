"""GP integer text follows its decimal input grammar and declared width."""

from __future__ import annotations

import datetime as dt
import importlib
from copy import deepcopy
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

_INTEGER_TYPES = [
    ("SMALLINT", 16),
    ("int2", 16),
    ("pg_catalog.int2", 16),
    ('pg_catalog."int2"', 16),
    ("INTEGER", 32),
    ("int", 32),
    ("int4", 32),
    ("pg_catalog.int4", 32),
    ("BIGINT", 64),
    ("int8", 64),
    ("pg_catalog.int8", 64),
]


@pytest.fixture
def integer_policy():
    return importlib.import_module("analytics_toolkit.sql.backends.gp.integer_values")


@pytest.mark.parametrize("native", ["SMALLINT", "INTEGER", "BIGINT"])
@pytest.mark.parametrize("sign", ["", "+", "-"])
def test_gp_significant_digit_overflow_rejects_without_exposing_large_input(
    integer_policy, native, sign
):
    value = sign + "9" * 5000

    with pytest.raises(integer_policy.GreenplumIntegerValueError) as failure:
        integer_policy.integer_text_value(value, native)

    assert failure.value.analytics_toolkit_sql_retry_safe is False
    assert value not in str(failure.value)
    assert "from_sql" in str(failure.value)


@pytest.mark.parametrize(("native", "width"), _INTEGER_TYPES)
def test_gp_integer_aliases_accept_both_exact_width_boundaries(integer_policy, native, width):
    assert integer_policy.is_integer_type(native) is True
    assert integer_policy.contains_integer_type(native) is True
    for value in (-(2 ** (width - 1)), 2 ** (width - 1) - 1):
        result = integer_policy.integer_text_value(str(value), native)

        assert type(result) is int
        assert result == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0),
        ("-0", 0),
        ("+0", 0),
        ("00012", 12),
        ("+00012", 12),
        ("-00012", -12),
        (" \t\r\n\v\f+0012 \t\r\n\v\f", 12),
        ("0" * 5000 + "42", 42),
        ("-" + "0" * 5000 + "42", -42),
        ("+" + "0" * 5000, 0),
    ],
)
def test_gp_integer_text_accepts_ascii_sign_whitespace_and_unbounded_zero_prefix(
    integer_policy, value, expected
):
    assert integer_policy.integer_text_value(value, "BIGINT") == expected
    assert integer_policy.normalize_authoritative_integer(value, "BIGINT") == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        " \t\r\n\v\f",
        "+",
        "-",
        "1.0",
        "1e2",
        "1_000",
        "0x10",
        "1 2",
        "+ 1",
        "--1",
        "\u0661",
        "\uff11",
        "\u00a01",
        "1\u2003",
        "1\x00",
        "private-invalid-integer",
    ],
)
def test_gp_integer_invalid_text_rejects_privately_without_retry(integer_policy, value):
    with pytest.raises(integer_policy.GreenplumIntegerValueError) as error:
        integer_policy.integer_text_value(value, "INTEGER")

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert repr(value) not in str(error.value)
    assert "from_sql" in str(error.value)
    assert "CAST" in str(error.value)


@pytest.mark.parametrize(("native", "width"), _INTEGER_TYPES)
@pytest.mark.parametrize("direction", [-1, 1])
def test_gp_integer_text_rejects_one_step_past_declared_width(
    integer_policy, native, width, direction
):
    value = -(2 ** (width - 1)) - 1 if direction < 0 else 2 ** (width - 1)

    with pytest.raises(integer_policy.GreenplumIntegerValueError):
        integer_policy.integer_text_value(str(value), native)


@pytest.mark.parametrize("value", [True, 1, np.int64(1), 1.5, b"1"])
def test_gp_integer_dispatch_keeps_nontext_values_for_existing_assignment(integer_policy, value):
    # This checks dispatch identity, without claiming those values bind as GP integers.
    assert integer_policy.integer_value(value, "BIGINT") is value
    assert integer_policy.normalize_authoritative_integer(value, "BIGINT") is value


@pytest.mark.parametrize("value", [Decimal("42.000"), Decimal(2**63 - 1)])
def test_gp_integer_dispatch_converts_exact_integral_decimal_without_float(integer_policy, value):
    converted = integer_policy.integer_value(value, "BIGINT")
    assert type(converted) is int
    assert converted == int(value)
    assert integer_policy.normalize_authoritative_integer(value, "BIGINT") == converted


@pytest.mark.parametrize("value", [Decimal("1.2"), Decimal(2**63)])
def test_gp_integer_dispatch_rejects_fractional_or_out_of_range_decimal(integer_policy, value):
    before = value.as_tuple()
    for converter in (integer_policy.integer_value, integer_policy.normalize_authoritative_integer):
        with pytest.raises(integer_policy.GreenplumIntegerValueError):
            converter(value, "BIGINT")
    assert value.as_tuple() == before


@pytest.mark.parametrize("value", [None, pd.NA, pd.NaT])
def test_gp_integer_null_assignment_normalizes_only_the_authoritative_view(integer_policy, value):
    assert integer_policy.integer_value(value, "BIGINT") is value
    assert integer_policy.normalize_authoritative_integer(value, "BIGINT") is None


@pytest.mark.parametrize("native", ["SMALLINT[][]", "pg_catalog._int4", "int8[]"])
def test_gp_integer_builtin_arrays_convert_leaves_without_mutating_input(integer_policy, native):
    values = [["+001", None], ["-002", "0"]] if native.endswith("[][]") else ["+001", None, "-002"]
    before = deepcopy(values)
    expected = [[1, None], [-2, 0]] if native.endswith("[][]") else [1, None, -2]

    assert integer_policy.is_integer_type(native) is False
    assert integer_policy.contains_integer_type(native) is True
    result = integer_policy.normalize_authoritative_integer(values, native)

    assert result == expected
    assert result is not values
    assert values == before


@pytest.mark.parametrize(
    "native", [None, "TEXT", "NUMERIC", "public.int8", '"Integer"', '"public"."int4"']
)
def test_gp_integer_dispatch_does_not_parse_other_types_or_user_domains(integer_policy, native):
    value = "001"

    assert integer_policy.is_integer_type(native) is False
    if native is not None:
        assert integer_policy.contains_integer_type(native) is False
    assert integer_policy.normalize_authoritative_integer(value, native) is value


def test_gp_integer_dispatch_retains_builtin_boolean_and_date_views(integer_policy):
    assert integer_policy.normalize_authoritative_integer("false", "BOOLEAN") is False
    assert integer_policy.normalize_authoritative_integer("2024-02-29", "DATE") == dt.date(
        2024, 2, 29
    )
