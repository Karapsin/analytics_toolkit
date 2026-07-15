from __future__ import annotations

# ruff: noqa: I001

import datetime as dt
import os
import uuid
from decimal import Decimal
from typing import Any

import pandas as pd

from .identity import resource_name

BACKENDS = ("gp", "trino", "ch")


def backend_enabled(backend: str) -> bool:
    return backend != "gp" or os.environ.get("SQL_INTEGRATION_GP") == "1"


def backend_alias(backend: str, *, target: bool = False) -> str:
    if backend == "gp":
        return "gp_target" if target else "gp_source"
    if backend == "trino":
        return "trino_target_values" if target else "trino_source_values"
    return "ch_target" if target else "ch_source"


def integration_table(backend: str, purpose: str) -> str:
    run_id = os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8])
    test_id = os.environ.get("SQL_INTEGRATION_TEST_ID", "manual")
    token = resource_name(run_id, test_id, purpose)
    if backend == "gp":
        return f"public.{token}"
    if backend == "trino":
        return f"iceberg.integration.{token}"
    return f"integration.{token}"


def table_options(backend: str, *, only_shard: bool = False) -> dict[str, Any]:
    if backend == "gp":
        return {"gp_distributed_by_key": "row_id"}
    if backend == "trino":
        return {"partition_by": ["event_date"]}
    return {
        "partition_by": ["event_date"],
        "order_by": ["row_id"],
        "ch_engine": "MergeTree",
        "ch_cluster": "integration_cluster",
        "ch_only_shard": only_shard,
    }


def canonical_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4, 5, 6],
            "flag": [True, False, None, True, False, None],
            "signed_value": [-9, 0, 17, None, -1, 8],
            "decimal_value": [
                Decimal("-12.3400"),
                Decimal("0.0000"),
                Decimal("98.7654"),
                None,
                Decimal("1.2000"),
                Decimal("-0.0001"),
            ],
            "float_value": [-2.5, 0.0, 3.25, None, 1.5, -0.125],
            "unicode_text": ["Latin", "Кириллица", "漢字", "😀", "", None],
            "event_date": [dt.date(2026, 1, day) for day in range(1, 7)],
            "event_ts": [
                pd.Timestamp(f"2026-01-0{day} 03:04:05.123456", tz="UTC") for day in range(1, 7)
            ],
            "nullable_ts": [
                pd.Timestamp("2026-02-01 00:00:00.000001", tz="UTC"),
                None,
                pd.Timestamp("2026-02-03 00:00:00.999999", tz="UTC"),
                None,
                pd.Timestamp("2026-02-05 12:30:00.500000", tz="UTC"),
                None,
            ],
            "uuid_value": [uuid.UUID(int=index) for index in range(1, 7)],
            "json_value": [
                '{"b":2,"a":1}',
                '[3,{"x":"я"}]',
                "{}",
                "[]",
                '{"emoji":"😀"}',
                '{"n":null}',
            ],
            "all_null_text": [None] * 6,
        }
    )


def canonical_schema(backend: str) -> dict[str, str]:
    common = {
        "row_id": "BIGINT",
        "flag": "BOOLEAN",
        "signed_value": "BIGINT",
        "decimal_value": "DECIMAL(18,4)",
        "float_value": "DOUBLE",
        "unicode_text": "VARCHAR",
        "event_date": "DATE",
        "event_ts": "TIMESTAMP(6) WITH TIME ZONE",
        "nullable_ts": "TIMESTAMP(6) WITH TIME ZONE",
        "uuid_value": "UUID",
        "json_value": "VARCHAR",
        "all_null_text": "VARCHAR",
    }
    if backend == "gp":
        return {
            **common,
            "decimal_value": "NUMERIC(18,4)",
            "float_value": "DOUBLE PRECISION",
            "unicode_text": "TEXT",
            "event_ts": "TIMESTAMPTZ",
            "nullable_ts": "TIMESTAMPTZ",
            "json_value": "JSONB",
            "all_null_text": "TEXT",
        }
    if backend == "ch":
        return {
            "row_id": "Int64",
            "flag": "Nullable(Bool)",
            "signed_value": "Nullable(Int64)",
            "decimal_value": "Nullable(Decimal(18,4))",
            "float_value": "Nullable(Float64)",
            "unicode_text": "Nullable(String)",
            "event_date": "Date",
            "event_ts": "DateTime64(6, 'UTC')",
            "nullable_ts": "Nullable(DateTime64(6, 'UTC'))",
            "uuid_value": "UUID",
            "json_value": "String",
            "all_null_text": "Nullable(String)",
        }
    return common
