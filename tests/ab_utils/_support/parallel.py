from __future__ import annotations

import importlib
import inspect
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from tests._support.concurrency import ConcurrencyProbe, SchedulingObservation, observe_executor_cap

ab_utils_module = importlib.import_module("analytics_toolkit.ab_utils")

metrics_module = importlib.import_module("analytics_toolkit.ab_utils.metrics")

parallel_module = importlib.import_module("analytics_toolkit.ab_utils.parallel")

if TYPE_CHECKING:
    from collections.abc import Callable


def patch_metric_runner(monkeypatch: pytest.MonkeyPatch, runner: Callable[..., Any]) -> None:
    """One patch location for behavior tests of dataframe task orchestration."""
    monkeypatch.setattr(parallel_module, "_compute_test_metrics_dataframe", runner)


def observe_metric_cap(
    monkeypatch: pytest.MonkeyPatch, probe: ConcurrencyProbe, *, calls: int
) -> SchedulingObservation:
    observation = observe_executor_cap(monkeypatch, parallel_module, probe, calls=calls)
    monkeypatch.setattr(
        parallel_module, "Semaphore", observation.semaphore_factory(parallel_module.Semaphore)
    )
    return observation


def bypass_metric_semaphores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Intentional test fault for the redundant AB soft semaphore limit."""
    original = parallel_module._build_concurrency_state

    def uncapped(**kwargs: Any) -> Any:
        return replace(original(**kwargs), semaphores=())

    monkeypatch.setattr(parallel_module, "_build_concurrency_state", uncapped)


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
    "bypass_metric_semaphores",
    "importlib",
    "inspect",
    "metrics_module",
    "observe_metric_cap",
    "parallel_module",
    "patch_metric_runner",
    "pd",
    "pytest",
    "threading",
    "time",
]
