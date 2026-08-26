from __future__ import annotations

import math
import re
import warnings
from types import SimpleNamespace
from typing import Any

import analytics_toolkit.ab_utils.metrics as metrics_module
import analytics_toolkit.ab_utils.parallel as parallel_module
import pandas as pd
import pytest
from analytics_toolkit import ab_utils
from analytics_toolkit.ab_utils import sql_native
from analytics_toolkit.ab_utils.sql_bootstrap import (
    _build_sql_native_bootstrap_query,
    _plan_sql_native_bootstrap_batches,
    _reduce_sql_native_bootstrap_batches,
)


def _metric_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4, 5, 6, 7, 8],
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


def _table_info() -> SimpleNamespace:
    return SimpleNamespace(
        backend="gp",
        exists=True,
        table="mart.ab_source",
        resolved_table=None,
        columns={
            "user_id": "integer",
            "group_name": "text",
            "orders": "double precision",
            "clicks": "double precision",
            "views": "double precision",
        },
    )


def _validation_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "row_count": 8,
                "null_user_rows": 0,
                "null_group_rows": 0,
                "duplicate_user_rows": 0,
                "control_rows": 4,
                "non_control_group_count": 1,
            }
        ]
    )


def _group_frame() -> pd.DataFrame:
    return pd.DataFrame({"group_name": ["control", "test"]})


def _base_stats_from_expected(expected: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, row in expected.iterrows():
        rows.append(
            {
                "metric_name": row["metric_name"],
                "metric_type": row["metric_type"],
                "group_name": row["group_2"],
                "n": row["n_group_2"],
                "metric_value": row["metric_group_2"],
                "variance_value": row["variance_group_2"],
                "outliers_cutoff": row["outliers_cutoff"],
                "outliers_n": row["outliers_n_group_2"],
            }
        )
        rows.append(
            {
                "metric_name": row["metric_name"],
                "metric_type": row["metric_type"],
                "group_name": row["group_1"],
                "n": row["n_group_1"],
                "metric_value": row["metric_group_1"],
                "variance_value": row["variance_group_1"],
                "outliers_cutoff": row["outliers_cutoff"],
                "outliers_n": row["outliers_n_group_1"],
            }
        )
    return pd.DataFrame(rows)


def _install_sql_native_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_stats: pd.DataFrame,
    bootstrap_stats: pd.DataFrame | None = None,
    metadata_frame: pd.DataFrame | None = None,
) -> list[str]:
    queries: list[str] = []
    monkeypatch.setattr(
        sql_native.sql_facade, "table_info", lambda *_args, **_kwargs: _table_info()
    )

    def fake_connection_config(db_key: str) -> SimpleNamespace:
        return SimpleNamespace(connection_key=db_key, backend="gp")

    monkeypatch.setattr(sql_native, "get_connection_config", fake_connection_config)

    def fake_read(**kwargs: Any) -> pd.DataFrame:
        query = kwargs["query"]
        queries.append(query)
        if "analytics_toolkit_ab_sql_native_bootstrap" in query:
            return bootstrap_stats if bootstrap_stats is not None else pd.DataFrame()
        if "WHERE 1 = 0" in query:
            return metadata_frame if metadata_frame is not None else pd.DataFrame()
        if "duplicate_user_rows" in query:
            return _validation_frame()
        if query.lstrip().startswith("SELECT DISTINCT"):
            return _group_frame()
        return base_stats

    monkeypatch.setattr(sql_native, "_read_sql_native_query", fake_read)
    return queries


def _install_sql_backed_dataframe_fakes(
    monkeypatch: pytest.MonkeyPatch,
    df: pd.DataFrame,
) -> list[list[dict[str, Any]]]:
    task_batches: list[list[dict[str, Any]]] = []

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        assert kwargs["concurrency"] == 1
        assert kwargs["fail_fast"] is True
        assert kwargs["progress"] is False
        task_batches.append(tasks)
        return {str(task["name"]): df.copy() for task in tasks}

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    return task_batches


def _compact_bootstrap_summary(**overrides: object) -> pd.DataFrame:
    row: dict[str, object] = {
        "metric_name": "orders",
        "group_1": "test",
        "group_2": "control",
        "requested_resamples": 2,
        "valid_family_resamples": 2,
        "valid_delta_resamples": 2,
        "delta_mean": 1.0,
        "delta_m2": 0.5,
        "max_t_exceedances": 1,
    }
    row.update(overrides)
    return pd.DataFrame([row])


__all__ = [
    "Any",
    "SimpleNamespace",
    "_base_stats_from_expected",
    "_build_sql_native_bootstrap_query",
    "_compact_bootstrap_summary",
    "_group_frame",
    "_install_sql_backed_dataframe_fakes",
    "_install_sql_native_fakes",
    "_metric_df",
    "_plan_sql_native_bootstrap_batches",
    "_reduce_sql_native_bootstrap_batches",
    "_table_info",
    "_validation_frame",
    "ab_utils",
    "math",
    "metrics_module",
    "parallel_module",
    "pd",
    "pytest",
    "re",
    "sql_native",
    "warnings",
]
