"""Explicit Trino BOOLEAN casts are bounded native casts, not inferred truthiness."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.trino.boolean_values import (
    TrinoBooleanValueError,
    boolean_value,
    contains_boolean_type,
    is_boolean_type,
    normalize_authoritative_boolean,
    validate_boolean_type_profile,
)
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    validate_profile,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (False, False),
        (True, True),
        (np.bool_(False), False),
        (np.bool_(True), True),
        (0, False),
        (1, True),
        (-1, True),
        (2, True),
        (-(2**63), True),
        (2**63 - 1, True),
        (np.int64(0), False),
        (np.int64(-7), True),
    ],
)
def test_trino_boolean_native_signed_bigint_cast_matches_nonzero_operator(value, expected):
    result = boolean_value(value)

    assert type(result) is bool
    assert result is expected
    assert normalize_authoritative_boolean(value, "BOOLEAN") is expected


@pytest.mark.parametrize("value", [None, pd.NA])
def test_trino_boolean_null_assignment_view_remains_null(value):
    assert normalize_authoritative_boolean(value, "BOOLEAN") is None


@pytest.mark.parametrize(
    "value", [-(2**63) - 1, 2**63, np.uint64(2**64 - 1), 1.0, "false", b"0", Decimal(1)]
)
def test_trino_boolean_rejects_out_of_domain_or_unproven_local_casts_privately(value):
    with pytest.raises(TrinoBooleanValueError, match="signed BIGINT") as error:
        boolean_value(value)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert str(value) not in str(error.value)
    assert "CAST the source expression AS BOOLEAN in from_sql" in str(error.value)


@pytest.mark.parametrize("native", ["BOOLEAN", "boolean", "  BoOlEaN  "])
def test_trino_boolean_type_validation_does_not_admit_integer_profiles(native):
    assert is_boolean_type(native) is True
    assert validate_boolean_type_profile(native) is False
    profile = ColumnProfile()
    profile.add(1)

    with pytest.raises(ValueError, match="authoritative boolean schema"):
        validate_profile(profile, native, "trino")

    assert declared_domain_fits("BIGINT", native, "trino", source_backend="trino") is False


@pytest.mark.parametrize("native", ["BOOLEAN(1)", "BOOLEAN()", "Nullable(BOOLEAN)"])
def test_trino_boolean_parameterized_native_declarations_reject_empty_input(native):
    with pytest.raises(TrinoBooleanValueError, match="unparameterized BOOLEAN"):
        validate_profile(ColumnProfile(), native, "trino")


def test_trino_boolean_dispatch_preserves_unknown_aliases_and_nonboolean_views():
    values = [0, 1, None]

    assert is_boolean_type("BOOL") is False
    assert contains_boolean_type("ARRAY(BIGINT)") is False
    assert normalize_authoritative_boolean(values, None) is values
    assert normalize_authoritative_boolean(values, "ARRAY(BIGINT)") is values
    assert normalize_authoritative_boolean("2024-02-29", "DATE") == dt.date(2024, 2, 29)


def test_trino_boolean_recursive_row_view_preserves_date_dispatch_and_caller_values():
    values = [(2, "2024-02-29"), (None, None)]
    before = deepcopy(values)

    result = normalize_authoritative_boolean(values, "ARRAY(ROW(flag BOOLEAN, day DATE))")

    assert result == [(True, dt.date(2024, 2, 29)), (None, None)]
    assert result is not values
    assert values == before


def test_trino_boolean_map_keys_reject_nonzero_collisions_before_dict_construction():
    values = {1: "one", 2: "two"}
    before = list(values.items())

    with pytest.raises(ValueError, match="map keys are not unique"):
        normalize_authoritative_boolean(values, "MAP(BOOLEAN, VARCHAR)")

    assert list(values.items()) == before
    assert len(values) == 2
