"""Shared native dispatch keeps exact backend conversions and legacy fallback domains."""

from __future__ import annotations

from decimal import Decimal

import pytest
from analytics_toolkit.sql.backends.ch.numeric_text import ClickHouseNumericTextValueError
from analytics_toolkit.sql.backends.gp.numeric_text import GreenplumNumericTextValueError
from analytics_toolkit.sql.backends.native_values import convert_native_value
from analytics_toolkit.sql.backends.trino.numeric_text import TrinoNumericTextValueError
from analytics_toolkit.sql.core.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    validate_profile,
)

_EXACT = "12345678901234567890.123456789"
_CONVERSIONS = [
    ("gp", "REAL", "-2.5", -2.5),
    ("trino", "REAL", "-2.5", -2.5),
    ("ch", "Float32", "-2.5", -2.5),
    ("gp", "NUMERIC(38,9)", _EXACT, Decimal(_EXACT)),
    ("trino", "DECIMAL(38,9)", _EXACT, Decimal(_EXACT)),
    ("ch", "Decimal(38,9)", _EXACT, Decimal(_EXACT)),
]


@pytest.mark.parametrize(("backend", "native", "value", "expected"), _CONVERSIONS)
def test_native_authoritative_values_dispatch_exact_text_through_registered_backend(
    backend, native, value, expected
):
    resolution = ResolvedColumn(native, target_backend=backend)

    handled, converted = convert_native_value(value, resolution)

    assert handled is True
    assert type(converted) is type(expected)
    assert converted == expected
    assert resolution.convert(value) == expected
    assert resolution.convert(converted) == expected
    assert resolution.convert(None) is None
    if isinstance(expected, Decimal):
        assert converted.as_tuple() == expected.as_tuple()
        assert str(converted) == _EXACT
    assert resolution.target_type == native


@pytest.mark.parametrize(
    ("backend", "native", "error", "message"),
    [
        (
            "gp",
            "DOUBLE PRECISION",
            GreenplumNumericTextValueError,
            r"^Greenplum numeric input requires finite ASCII decimal text",
        ),
        (
            "trino",
            "DOUBLE",
            TrinoNumericTextValueError,
            r"^Trino numeric text requires a finite native numeric value",
        ),
        (
            "ch",
            "Float64",
            ClickHouseNumericTextValueError,
            r"^ClickHouse numeric text requires a finite value",
        ),
    ],
)
def test_native_authoritative_values_dispatch_never_exposes_invalid_text(
    backend, native, error, message
):
    secret = "private-authoritative-numeric-value"
    resolution = ResolvedColumn(native, target_backend=backend)

    with pytest.raises(error, match=message) as caught:
        convert_native_value(secret, resolution)

    assert secret not in str(caught.value)
    assert "from_sql" in str(caught.value)
    assert caught.value.analytics_toolkit_sql_retry_safe is False
    assert resolution.target_type == native


@pytest.mark.parametrize(
    ("native", "value"),
    [("BIGINT", 2**53 + 1), ("NUMERIC(38,9)", Decimal(_EXACT))],
)
def test_native_authoritative_values_backendless_compatibility_uses_shared_exact_domains(
    native, value
):
    resolution = ResolvedColumn(native)
    profile = ColumnProfile()
    profile.add(value)

    validate_profile(profile, native, resolution.target_backend)

    assert resolution.target_backend == ""
    converted = resolution.convert(value)
    assert type(converted) is type(value)
    assert converted == value
    assert profile.count == 1
    assert profile.null_count == 0


def test_native_authoritative_values_backendless_profile_keeps_legacy_text_rejection():
    profile = ColumnProfile()
    secret = "private-untagged-native-value"
    profile.add(secret)
    resolution = ResolvedColumn("BIGINT")

    with pytest.raises(ValueError, match="authoritative integer") as caught:
        validate_profile(profile, resolution.target_type, resolution.target_backend)

    assert secret not in str(caught.value)
    assert profile.families == {"string"}
    assert profile.count == 1
