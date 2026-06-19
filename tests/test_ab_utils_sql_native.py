from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

import analytics_toolkit.ab_utils as ab_utils
import analytics_toolkit.ab_utils.metrics as metrics_module
import analytics_toolkit.ab_utils.parallel as parallel_module
import analytics_toolkit.ab_utils.sql_native as sql_native


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
                "n": row["n0"],
                "metric_value": row["metric_control"],
                "variance_value": row["variance_control"],
                "outliers_cutoff": row["outliers_cutoff"],
                "outliers_n": row["outliers_n_control"],
            }
        )
        rows.append(
            {
                "metric_name": row["metric_name"],
                "metric_type": row["metric_type"],
                "group_name": row["group_1"],
                "n": row["n1"],
                "metric_value": row["metric_test"],
                "variance_value": row["variance_test"],
                "outliers_cutoff": row["outliers_cutoff"],
                "outliers_n": row["outliers_n_test"],
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
    monkeypatch.setattr(sql_native.sql_facade, "table_info", lambda *_args, **_kwargs: _table_info())

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


def test_compute_test_metrics_sql_native_is_exported() -> None:
    assert ab_utils.compute_test_metrics_sql_native is sql_native.compute_test_metrics_sql_native
    assert metrics_module.compute_test_metrics_sql_native is sql_native.compute_test_metrics_sql_native


def test_compute_test_metrics_sql_native_finalizes_compact_sql_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratio_metrics = [{"name": "ctr", "numerator": "clicks", "denominator": "views"}]
    expected = ab_utils.compute_test_metrics(
        _metric_df(),
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )
    queries = _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any("duplicate_user_rows" in query for query in queries)
    assert not any("SELECT * FROM mart.ab_source" in query for query in queries)


def test_compute_test_metrics_sql_native_uses_bootstrap_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    bootstrap = pd.DataFrame(
        [
            {
                "metric_name": "orders",
                "group_1": "test",
                "group_2": "control",
                "se_bootstrap": 0.25,
                "bootstrap_adj_p": 0.125,
            }
        ]
    )
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
        bootstrap_stats=bootstrap,
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        metric_columns=["orders"],
        test_vs_test=False,
        outliers_quantile=1,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=10,
    )

    assert result.loc[0, "s.e. bootstrap"] == pytest.approx(0.25)
    assert result.loc[0, "bootstrap_adj_p"] == pytest.approx(0.125)


def test_compute_test_metrics_sql_native_accepts_raw_sql_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratio_metrics: list[dict[str, object]] = []
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    metadata = pd.DataFrame(columns=["user_id", "group_name", "orders"])
    queries = _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
        metadata_frame=metadata,
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "select user_id, group_name, orders from mart.ab_source",
        source_type="sql",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any("WHERE 1 = 0" in query for query in queries)


def test_compute_test_metrics_sql_native_task_mapping_adds_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        {
            "segment_a": {
                "source": "mart.ab_source",
                "metric_columns": ["orders"],
                "labels": {"segment": "a"},
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
    )

    assert isinstance(result, dict)
    assert result["segment_a"]["segment"].tolist() == ["a"]
    pd.testing.assert_frame_equal(
        result["segment_a"].drop(columns=["segment"]),
        expected,
    )


def test_metric_entrypoints_three_way_parity_single_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _metric_df()
    ratio_metrics = [
        {
            "name": "ctr_agg",
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "views",
            "level": "user",
        },
    ]
    expected = ab_utils.compute_test_metrics(
        df,
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )
    _install_sql_backed_dataframe_fakes(monkeypatch, df)
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    sql_backed_result = ab_utils.compute_metrics_from_sql(
        {"one": {"sql": "select * from mart.ab_source", "test_vs_test": False}},
        db_key="analytics",
        ratio_metrics=ratio_metrics,
        outliers_quantile=1,
        concurrency=1,
        progress=False,
    )
    sql_native_result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        metric_columns=["orders", "clicks", "views"],
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(sql_backed_result["one"], expected)
    pd.testing.assert_frame_equal(sql_native_result, expected)
    assert list(sql_backed_result["one"].columns) == list(expected.columns)
    assert list(sql_native_result.columns) == list(expected.columns)


def test_metric_entrypoints_three_way_parity_task_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _metric_df()
    ratio_metrics = [
        {
            "name": "ctr_agg",
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "views",
            "level": "user",
        },
    ]
    expected_tasks = ab_utils.compute_test_metrics(
        {
            "segment_a": {
                "df": df,
                "labels": {"segment": "a"},
                "ratio_metrics": ratio_metrics,
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
        concurrency=1,
        progress=False,
    )
    _install_sql_backed_dataframe_fakes(monkeypatch, df)
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(
            expected_tasks["segment_a"].drop(columns=["segment"])
        ),
    )

    sql_backed_tasks = ab_utils.compute_metrics_from_sql(
        {
            "segment_a": {
                "sql": "select * from mart.ab_source",
                "labels": {"segment": "a"},
                "test_vs_test": False,
            }
        },
        db_key="analytics",
        ratio_metrics=ratio_metrics,
        outliers_quantile=1,
        concurrency=1,
        progress=False,
    )
    sql_native_tasks = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        {
            "segment_a": {
                "source": "mart.ab_source",
                "metric_columns": ["orders", "clicks", "views"],
                "labels": {"segment": "a"},
                "ratio_metrics": ratio_metrics,
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
        concurrency=1,
        progress=False,
    )

    pd.testing.assert_frame_equal(sql_backed_tasks["segment_a"], expected_tasks["segment_a"])
    pd.testing.assert_frame_equal(sql_native_tasks["segment_a"], expected_tasks["segment_a"])
    assert list(sql_backed_tasks["segment_a"].columns) == list(
        expected_tasks["segment_a"].columns
    )
    assert list(sql_native_tasks["segment_a"].columns) == list(
        expected_tasks["segment_a"].columns
    )
