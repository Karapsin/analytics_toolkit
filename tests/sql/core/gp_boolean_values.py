"""Greenplum Boolean text follows its input function, not Python truthiness."""

from __future__ import annotations

import datetime as dt
import importlib
from copy import deepcopy
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    validate_profile,
)


@pytest.fixture
def boolean_policy():
    # Tests can collect for the public RED before the approved helper is implemented.
    return importlib.import_module("analytics_toolkit.sql.backends.gp.boolean_values")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("t", True),
        ("tr", True),
        ("tru", True),
        ("true", True),
        ("y", True),
        ("ye", True),
        ("yes", True),
        ("on", True),
        ("1", True),
        ("f", False),
        ("fa", False),
        ("fal", False),
        ("fals", False),
        ("false", False),
        ("n", False),
        ("no", False),
        ("of", False),
        ("off", False),
        ("0", False),
        ("  FaLsE\t", False),
        ("\r\nYe\v\f", True),
        (" OF ", False),
    ],
)
def test_gp_boolean_text_accepts_native_unique_prefixes_ascii_case_and_whitespace(
    boolean_policy, value, expected
):
    result = boolean_policy.boolean_value(value)

    assert type(result) is bool
    assert result is expected
    assert boolean_policy.normalize_authoritative_boolean(value, "BOOLEAN") is expected


@pytest.mark.parametrize("value", [False, True, np.bool_(False), np.bool_(True)])
def test_gp_boolean_native_boolean_identity_remains_boolean(boolean_policy, value):
    result = boolean_policy.boolean_value(value)

    assert type(result) is bool
    assert result is bool(value)


@pytest.mark.parametrize("value", [None, pd.NA])
def test_gp_boolean_null_assignment_remains_null(boolean_policy, value):
    assert boolean_policy.normalize_authoritative_boolean(value, "BOOLEAN") is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "o",
        "ONN",
        "falsehood",
        "01",
        "t r",
        "fal\u017fe",
        "\u00a0true",
        "private-invalid-boolean",
    ],
)
def test_gp_boolean_invalid_or_ambiguous_text_rejects_privately(boolean_policy, value):
    with pytest.raises(boolean_policy.GreenplumBooleanValueError, match="BOOLEAN") as error:
        boolean_policy.boolean_value(value)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert repr(value) not in str(error.value)
    assert "CAST the source expression AS BOOLEAN in from_sql" in str(error.value)


@pytest.mark.parametrize("value", [0, 1, np.int64(1), 1.0, Decimal(1), b"false"])
def test_gp_boolean_does_not_invent_numeric_or_binary_assignment_casts(boolean_policy, value):
    with pytest.raises(boolean_policy.GreenplumBooleanValueError):
        boolean_policy.boolean_value(value)


@pytest.mark.parametrize("native", ["BOOL", "BOOLEAN", "pg_catalog.bool", 'pg_catalog."bool"'])
def test_gp_boolean_type_identity_uses_only_supported_builtins(boolean_policy, native):
    assert boolean_policy.is_boolean_type(native) is True
    assert boolean_policy.normalize_authoritative_boolean("false", native) is False
    profile = ColumnProfile()
    profile.add("false")

    with pytest.raises(ValueError, match="authoritative boolean schema"):
        validate_profile(profile, native, "gp")

    assert declared_domain_fits("VARCHAR", native, "gp", source_backend="gp") is False


@pytest.mark.parametrize("native", ["public.bool", '"Boolean"', '"public"."bool"'])
def test_gp_boolean_domains_and_quoted_user_types_keep_server_assignment(boolean_policy, native):
    value = "false"

    assert boolean_policy.is_boolean_type(native) is False
    assert boolean_policy.contains_boolean_type(native) is False
    assert boolean_policy.normalize_authoritative_boolean(value, native) is value


def test_gp_boolean_builtin_arrays_convert_nullable_leaves_without_mutating_input(boolean_policy):
    values = [["TRUE", None], ["f", "on"]]
    before = deepcopy(values)

    converted = boolean_policy.normalize_authoritative_boolean(values, "BOOLEAN[][]")

    assert converted == [[True, None], [False, True]]
    assert converted is not values
    assert values == before


def test_gp_boolean_dispatch_retains_date_and_nonboolean_assignment_views(boolean_policy):
    values = [0, 1, None]

    assert boolean_policy.normalize_authoritative_boolean(values, None) is values
    assert boolean_policy.normalize_authoritative_boolean(values, "BIGINT[]") is values
    converted = boolean_policy.normalize_authoritative_boolean("2024-02-29", "DATE")
    assert converted == dt.date(2024, 2, 29)
