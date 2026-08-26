from __future__ import annotations

import importlib
import inspect
import threading
import time
from typing import Any

import pandas as pd
import pytest

ab_utils_module = importlib.import_module("analytics_toolkit.ab_utils")

metrics_module = importlib.import_module("analytics_toolkit.ab_utils.metrics")

parallel_module = importlib.import_module("analytics_toolkit.ab_utils.parallel")

async_sql_module = importlib.import_module("analytics_toolkit.sql.orchestration.async_sql")


def _build_metric_parity_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": [
                "control",
                "control",
                "control",
                "control",
                "test",
                "test",
                "test",
                "test",
            ],
            "orders": [1.0, 2.0, 3.0, 4.0, 2.0, 3.0, 4.0, 5.0],
            "clicks": [1.0, 2.0, 1.0, 3.0, 2.0, 3.0, 2.0, 4.0],
            "views": [10.0, 12.0, 8.0, 15.0, 11.0, 14.0, 9.0, 16.0],
        }
    )


__all__ = [
    "Any",
    "_build_metric_parity_df",
    "ab_utils_module",
    "async_sql_module",
    "importlib",
    "inspect",
    "metrics_module",
    "parallel_module",
    "pd",
    "pytest",
    "threading",
    "time",
]
