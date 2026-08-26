from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.ab_utils import api as api_module
from analytics_toolkit.ab_utils import bootstrap as bootstrap_module
from analytics_toolkit.ab_utils import cuped as cuped_module
from analytics_toolkit.ab_utils import outliers as outliers_module
from analytics_toolkit.ab_utils import ratio as ratio_module
from analytics_toolkit.ab_utils import rows as rows_module
from analytics_toolkit.ab_utils import split as split_module
from analytics_toolkit.ab_utils import sql_bootstrap as sql_bootstrap_module
from analytics_toolkit.ab_utils import sql_native
from analytics_toolkit.ab_utils import stats as stats_module
from analytics_toolkit.ab_utils import validation as validation_module


def _ratio_source() -> pd.DataFrame:
    return pd.DataFrame({"num": [1.0, 2.0], "den": [2.0, 4.0]})


def _sql_source(
    *,
    backend: str = "gp",
    columns: list[str] | None = None,
) -> sql_native._SqlNativeSource:
    resolved_columns = columns or ["user_id", "group_name", "orders"]
    return sql_native._SqlNativeSource(
        backend=backend,
        source_sql='"mart"."ab_source"',
        sql_where=None,
        columns=resolved_columns,
        column_types=dict.fromkeys(resolved_columns, "double precision"),
    )


def _resolve_source_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "db_key": "analytics",
        "source": "mart.ab_source",
        "source_type": "table",
        "sql_where": None,
        "print_queries": False,
        "retry_cnt": 1,
        "timeout_increment": 1,
        "query_label": None,
    }
    kwargs.update(overrides)
    return kwargs


def _valid_sql_source_stats() -> dict[str, int]:
    return {
        "row_count": 2,
        "null_user_rows": 0,
        "duplicate_user_rows": 0,
        "null_group_rows": 0,
        "control_rows": 1,
        "non_control_group_count": 1,
    }


def _mean_group_stats() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "metric_name": "orders",
                "group_name": "control",
                "metric_value": 1.0,
                "variance_value": 1.0,
                "n": 4,
                "outliers_cutoff": 5.0,
                "outliers_n": 0,
            },
            {
                "metric_name": "orders",
                "group_name": "test",
                "metric_value": 2.0,
                "variance_value": 1.0,
                "n": 4,
                "outliers_cutoff": 5.0,
                "outliers_n": 0,
            },
        ]
    )


__all__ = [
    "Any",
    "ProcessPoolExecutor",
    "SimpleNamespace",
    "ThreadPoolExecutor",
    "_mean_group_stats",
    "_ratio_source",
    "_resolve_source_kwargs",
    "_sql_source",
    "_valid_sql_source_stats",
    "api_module",
    "bootstrap_module",
    "cuped_module",
    "math",
    "np",
    "outliers_module",
    "pd",
    "pytest",
    "ratio_module",
    "rows_module",
    "split_module",
    "sql_bootstrap_module",
    "sql_native",
    "stats_module",
    "validation_module",
]
