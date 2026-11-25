"""Greenplum transfer predicates retain timezone-aware key instants."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core.exact_values import ExactTemporal


class _NoOffset(tzinfo):
    def utcoffset(self, value):
        return None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            datetime(2025, 1, 1, 3, tzinfo=timezone(timedelta(hours=3))),
            "2025-01-01 03:00:00+03:00",
        ),
        (datetime(2025, 1, 1, tzinfo=timezone.utc), "2025-01-01 00:00:00+00:00"),
        (
            datetime(2024, 12, 31, 19, tzinfo=timezone(-timedelta(hours=5))),
            "2024-12-31 19:00:00-05:00",
        ),
        (
            pd.Timestamp("2025-01-01T03:00:00.123456+03:00"),
            "2025-01-01 03:00:00.123456+03:00",
        ),
        (
            ExactTemporal(1735689600123456, 6, "Europe/Moscow"),
            "2025-01-01 00:00:00.123456+00:00",
        ),
    ],
)
def test_aware_transfer_keys_have_explicit_timezone_type(value, expected):
    assert get_backend_adapter("gp").render_transfer_literal(value) == (
        f"TIMESTAMP WITH TIME ZONE '{expected}'"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            datetime(2025, 1, 1, 3, tzinfo=None),  # noqa: DTZ001 - naive-key contract
            "2025-01-01 03:00:00",
        ),
        (pd.Timestamp("2025-01-01T03:00:00.123456"), "2025-01-01 03:00:00.123456"),
        (datetime(2025, 1, 1, 3, tzinfo=_NoOffset()), "2025-01-01 03:00:00"),
        (ExactTemporal(1735689600123456, 6), "2025-01-01 00:00:00.123456"),
        (
            ExactTemporal(1735689600123456, 6, "Europe/Moscow", offset_naive=True),
            "2025-01-01 03:00:00.123456",
        ),
    ],
)
def test_naive_transfer_keys_keep_timestamp_type_without_inventing_timezone(value, expected):
    assert get_backend_adapter("gp").render_transfer_literal(value) == f"TIMESTAMP '{expected}'"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (date(2025, 1, 1), "DATE '2025-01-01'"),
        ("O'Reilly", "'O''Reilly'"),
        (42, "42"),
        (Decimal("1.234"), "1.234"),
    ],
)
def test_non_timestamp_transfer_keys_keep_shared_literal_contract(value, expected):
    assert get_backend_adapter("gp").render_transfer_literal(value) == expected


def test_invalid_transfer_key_preserves_shared_validation():
    with pytest.raises(ValueError, match="transfer_key_values supports only"):
        get_backend_adapter("gp").render_transfer_literal(object())
