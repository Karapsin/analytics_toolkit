"""Public dataframe inference must try exact native mixed-family candidates."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize(
    ("values", "types"),
    [
        ([None, 1, 1.5], {"gp": "DOUBLE PRECISION", "trino": "DOUBLE", "ch": "Nullable(Float64)"}),
        (
            [2**53 + 1, 0.5, None],
            {"gp": "NUMERIC(17, 1)", "trino": "DECIMAL(17, 1)", "ch": "Nullable(Decimal(17, 1))"},
        ),
        (
            [Decimal("1.25"), 0.5],
            {"gp": "NUMERIC(3, 2)", "trino": "DECIMAL(3, 2)", "ch": "Decimal(3, 2)"},
        ),
        (
            [dt.date(2025, 1, 1), dt.datetime.fromisoformat("2025-01-02T03:04:05"), None],
            {"gp": "TIMESTAMP", "trino": "TIMESTAMP(6)", "ch": "Nullable(DateTime64(6))"},
        ),
    ],
    ids=[
        "small-integer-float",
        "exact-large-integer-float",
        "exact-decimal-float",
        "date-midnight-and-datetime",
    ],
)
def test_public_create_selects_exact_native_mixed_family_schema(backend, values, types):
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    ddl = sql.create_table(
        backend, "sandbox.events", frame, only_generate_sql=True, ch_distributed_table=False
    )

    assert types[backend] in ddl
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("aware", [False, True])
def test_public_create_infers_native_trino_time(aware):
    zone = dt.timezone(dt.timedelta(hours=3)) if aware else None
    frame = pd.DataFrame(
        {"clock": pd.Series([None, dt.time(12, 34, 56, 123456, tzinfo=zone)], dtype=object)}
    )

    ddl = sql.create_table("trino", "sandbox.events", frame, only_generate_sql=True)

    assert "TIME(6)" + (" WITH TIME ZONE" if aware else "") in ddl


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_public_create_keeps_mixed_naive_and_aware_temporal_values_in_typed_text(backend):
    frame = pd.DataFrame(
        {
            "stamp": pd.Series(
                [dt.date(2025, 1, 1), dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc)], dtype=object
            )
        }
    )

    ddl = sql.create_table(
        backend, "sandbox.events", frame, only_generate_sql=True, ch_distributed_table=False
    )

    assert {"gp": "TEXT", "trino": "VARCHAR", "ch": "String"}[backend] in ddl
