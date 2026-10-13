from __future__ import annotations

import importlib
import inspect
import threading
import time
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

ab_utils_module = importlib.import_module("analytics_toolkit.ab_utils")

metrics_module = importlib.import_module("analytics_toolkit.ab_utils.metrics")

parallel_module = importlib.import_module("analytics_toolkit.ab_utils.parallel")

if TYPE_CHECKING:
    from collections.abc import Callable


def patch_metric_runner(monkeypatch: pytest.MonkeyPatch, runner: Callable[..., Any]) -> None:
    """One patch location for behavior tests of dataframe task orchestration."""
    monkeypatch.setattr(parallel_module, "_compute_test_metrics_dataframe", runner)


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
    "patch_metric_runner",
    "pd",
    "pytest",
    "threading",
    "time",
]
