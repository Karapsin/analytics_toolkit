"""Disposable native-value checks for ordinary Enum dataframe cells."""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    integration_table,
)
from tests.sql.integration._support.identity import query_label
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_WIDE = 2**53 + 1


class _Number(Enum):
    POSITIVE = _WIDE
    NEGATIVE = -_WIDE
    SQL_NULL = None


class _Amount(Enum):
    LARGE = Decimal("12345678901234.1234")
    SMALL = Decimal("-0.0001")
    ZERO = Decimal("0.0000")


class _Day(Enum):
    START = date(2026, 1, 1)
    EARLY = date(1900, 1, 1)
    END = date(2026, 2, 1)


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _schema(backend: str) -> dict[str, str]:
    if backend == "ch":
        return {
            "row_id": "Int64",
            "value": "Nullable(Int64)",
            "amount": "Nullable(Decimal(18,4))",
            "event_date": "Nullable(Date32)",
            "items": "Array(Nullable(Int64))",
        }
    return {
        "row_id": "BIGINT",
        "value": "BIGINT",
        "amount": "NUMERIC(18,4)" if backend == "gp" else "DECIMAL(18,4)",
        "event_date": "DATE",
        "items": "BIGINT[]" if backend == "gp" else "ARRAY(BIGINT)",
    }


def _options(backend: str) -> dict[str, Any]:
    if backend == "gp":
        return {"gp_distributed_by_key": "row_id"}
    if backend == "ch":
        return {
            "ch_engine": "MergeTree",
            "ch_distributed_table": False,
            "ch_shard_on_cluster": "integration_cluster",
            "order_by": "row_id",
        }
    return {}


def _enum_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4],
            "value": pd.Series(
                [_Number.POSITIVE, None, _Number.NEGATIVE, _Number.SQL_NULL], dtype=object
            ),
            "amount": pd.Series([_Amount.LARGE, None, _Amount.SMALL, _Amount.ZERO], dtype=object),
            "event_date": pd.Series([_Day.START, None, _Day.EARLY, _Day.END], dtype=object),
            "items": pd.Series(
                [
                    [_Number.POSITIVE, _Number.NEGATIVE],
                    [_Number.SQL_NULL, _Number.POSITIVE],
                    [],
                    [None],
                ],
                dtype=object,
            ),
        }
    )


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"audit.load.enum_values.{backend}", backend) for backend in BACKENDS],
)
def test_ordinary_enum_cells_load_as_exact_native_values_after_truncation(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    table = resource_registry.table(
        alias,
        integration_table(backend, "enum_native"),
        ch_cluster="integration_cluster" if backend == "ch" else None,
    )
    options = _options(backend)
    seed = pd.DataFrame(
        {
            "row_id": [99],
            "value": [0],
            "amount": [Decimal("1.0000")],
            "event_date": [date(2026, 1, 1)],
            "items": [[7, 8]],
        }
    )
    assert (
        sql.load_df(
            alias,
            table,
            seed,
            table_schema=_schema(backend),
            write_mode="replace",
            retry_cnt=1,
            query_label=_label("enum-native-seed"),
            **options,
        )
        == 1
    )
    incoming = _enum_frame()
    assert (
        sql.load_df(
            alias,
            table,
            incoming,
            write_mode="truncate_insert",
            retry_cnt=1,
            query_label=_label("enum-native-load"),
            **options,
        )
        == 4
    )
    assert incoming.iloc[0]["value"] is _Number.POSITIVE
    assert incoming.iloc[0]["amount"] is _Amount.LARGE
    assert incoming.iloc[0]["event_date"] is _Day.START
    assert incoming.iloc[0]["items"][0] is _Number.POSITIVE

    actual = sql.read(
        alias,
        f"SELECT row_id, value, amount, event_date, items FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("enum-native-check"),
    )
    assert actual["row_id"].tolist() == [1, 2, 3, 4]
    assert actual.iloc[0]["value"] == _WIDE
    assert pd.isna(actual.iloc[1]["value"])
    assert actual.iloc[2]["value"] == -_WIDE
    assert pd.isna(actual.iloc[3]["value"])
    assert actual.iloc[0]["amount"] == Decimal("12345678901234.1234")
    assert pd.isna(actual.iloc[1]["amount"])
    assert actual.iloc[2]["amount"] == Decimal("-0.0001")
    assert actual.iloc[3]["amount"] == Decimal("0.0000")
    assert pd.Timestamp(actual.iloc[0]["event_date"]).date() == date(2026, 1, 1)
    assert pd.isna(actual.iloc[1]["event_date"])
    assert pd.Timestamp(actual.iloc[2]["event_date"]).date() == date(1900, 1, 1)
    assert pd.Timestamp(actual.iloc[3]["event_date"]).date() == date(2026, 2, 1)
    assert list(actual.iloc[0]["items"]) == [_WIDE, -_WIDE]
    assert list(actual.iloc[1]["items"]) == [None, _WIDE]
    assert list(actual.iloc[2]["items"]) == []
    assert list(actual.iloc[3]["items"]) == [None]
