from __future__ import annotations

import inspect
import math
import threading
import time
from types import SimpleNamespace
from typing import Any
import warnings

import numpy as np
import pandas as pd
import pytest
from scipy.stats import ttest_ind

import analytics_toolkit.ab_utils as ab_utils
import analytics_toolkit.ab_utils.metrics as ab_metrics
import analytics_toolkit.ab_utils.planning as planning_module
from analytics_toolkit.ab_utils import (
    RatioMetricSpec,
    compute_mde,
    compute_mde_from_sql,
    compute_test_metrics,
)
from analytics_toolkit.ab_utils.metrics import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    _apply_outliers_to_agg_ratio_components,
    _apply_outliers_to_values,
    _build_comparisons,
    _build_metric_definitions,
    _build_metric_row,
    _build_outlier_contexts,
    _build_ratio_valid_mask,
    _compute_agg_ratio_diff_standard_error,
    _compute_agg_ratio_group_stats,
    _compute_agg_ratio_variance,
    _compute_cuped_statistics,
    _compute_cuped_statistics_from_frame,
    _compute_mde_from_standard_error,
    _compute_studentized_statistic,
    _compute_ttest_stat_and_p_value,
    _get_numeric_metric_series,
    _normalize_ratio_metrics,
    _prepare_cuped_context,
    _prepare_metric_context,
)


def test_compute_test_metrics_bootstrap_progress_defaults_to_false() -> None:
    signature = inspect.signature(compute_test_metrics)

    assert signature.parameters["bootstrap_progress"].default is False


def test_compute_mde_start_dt_is_required() -> None:
    assert inspect.signature(compute_mde).parameters["start_dt"].default is inspect._empty
    assert (
        inspect.signature(compute_mde_from_sql).parameters["start_dt"].default
        is inspect._empty
    )


def test_compute_mde_from_sql_concurrency_defaults_to_one() -> None:
    assert inspect.signature(compute_mde_from_sql).parameters["concurrency"].default == 1


def test_ab_metric_outlier_policy_defaults_to_non_zero_truncate() -> None:
    assert (
        inspect.signature(compute_test_metrics).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde_from_sql).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )


def _build_sample_metrics_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": list(range(1, 13)),
            "group_name": [
                "control",
                "control",
                "control",
                "control",
                "test_a",
                "test_a",
                "test_a",
                "test_a",
                "test_b",
                "test_b",
                "test_b",
                "test_b",
            ],
            "orders": [10, 12, 9, np.nan, 13, 15, 11, 14, 8, 10, 9, 11],
            "gmv": [100.0, 120.0, 95.0, 110.0, 130.0, 145.0, 118.0, 140.0, 92.0, 105.0, 99.0, 108.0],
            "clicks": [5, 3, 4, 2, 7, 5, 6, 8, 4, 5, 3, 4],
            "impressions": [10, 8, 0, 4, 14, 10, 12, 16, 8, 10, 6, 8],
        }
    )


def test_prepared_metric_context_matches_legacy_rows_and_cuped() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": [
                "control",
                "control",
                "control",
                "test_a",
                "test_a",
                "test_a",
                "test_b",
                "test_b",
                "test_b",
            ],
            "orders": [10, 11, 12, 13, 14, 15, 16, 17, 18],
            "clicks": [1, 2, 3, 4, 5, 6, 7, 8, 9],
            "impressions": [10, 10, 10, 10, 10, 10, 10, 10, 10],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": [
                "control",
                "control",
                "control",
                "test_a",
                "test_a",
                "test_a",
                "test_b",
                "test_b",
                "test_b",
            ],
            "orders": [8, 9, 11, 12, 13, 14, 15, 16, 17],
            "clicks": [1, 1, 2, 3, 4, 5, 6, 7, 8],
            "impressions": [9, 10, 10, 10, 10, 11, 10, 10, 10],
        }
    )
    ratio_metrics = [
        {"name": "ctr_user", "numerator": "clicks", "denominator": "impressions", "level": "user"},
        {"name": "ctr_agg", "numerator": "clicks", "denominator": "impressions", "level": "agg"},
    ]
    metric_columns = ["orders"]
    metric_definitions = _build_metric_definitions(
        metric_columns,
        _normalize_ratio_metrics(
            df,
            ratio_metrics,
            reserved_columns={"group_name", "user_id"},
        ),
    )
    group_values = df["group_name"].to_numpy()
    group_masks = {
        group_name: group_values == group_name
        for group_name in df["group_name"].drop_duplicates().tolist()
    }
    comparisons = _build_comparisons(
        df["group_name"].drop_duplicates().tolist(),
        "control",
        test_vs_test=True,
    )
    outlier_contexts = _build_outlier_contexts(
        df=df,
        metric_definitions=metric_definitions,
        outliers_quantile=0.9,
        outliers_policy="truncate",
    )
    pre_outlier_contexts = _build_outlier_contexts(
        df=pre_df,
        metric_definitions=metric_definitions,
        outliers_quantile=0.9,
        outliers_policy="truncate",
        allow_missing=True,
    )

    for metric_definition in metric_definitions:
        metric_key = str(metric_definition["metric_key"])
        prepared_metric_context = _prepare_metric_context(
            df=df,
            metric_definition=metric_definition,
            outlier_context=outlier_contexts[metric_key],
        )
        prepared_cuped_context = _prepare_cuped_context(
            df=df,
            pre_exp_metrics_df=pre_df,
            user_id_column="user_id",
            metric_definition=metric_definition,
            outlier_context=outlier_contexts[metric_key],
            pre_outlier_context=pre_outlier_contexts.get(metric_key),
        )

        for test_group, baseline_group in comparisons:
            legacy_row = _build_metric_row(
                df=df,
                group_column="group_name",
                baseline_group=baseline_group,
                test_group=test_group,
                metric_definition=metric_definition,
                mde_alpha=DEFAULT_ALPHA,
                mde_power=DEFAULT_POWER,
                outlier_context=outlier_contexts[metric_key],
            )
            prepared_row = _build_metric_row(
                df=df,
                group_column="group_name",
                baseline_group=baseline_group,
                test_group=test_group,
                metric_definition=metric_definition,
                mde_alpha=DEFAULT_ALPHA,
                mde_power=DEFAULT_POWER,
                outlier_context=outlier_contexts[metric_key],
                prepared_metric_context=prepared_metric_context,
                group_masks=group_masks,
            )
            pd.testing.assert_series_equal(
                pd.Series(prepared_row).sort_index(),
                pd.Series(legacy_row).sort_index(),
                check_dtype=False,
            )

            comparison_frame = df.loc[
                group_masks[test_group] | group_masks[baseline_group],
                ["user_id", "group_name"],
            ].copy()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=UserWarning)
                legacy_cuped = _compute_cuped_statistics(
                    df=df,
                    pre_exp_metrics_df=pre_df,
                    group_column="group_name",
                    user_id_column="user_id",
                    baseline_group=baseline_group,
                    test_group=test_group,
                    metric_definition=metric_definition,
                    outlier_context=outlier_contexts[metric_key],
                    pre_outlier_context=pre_outlier_contexts.get(metric_key),
                )
                prepared_cuped = _compute_cuped_statistics(
                    df=df,
                    pre_exp_metrics_df=pre_df,
                    group_column="group_name",
                    user_id_column="user_id",
                    baseline_group=baseline_group,
                    test_group=test_group,
                    metric_definition=metric_definition,
                    outlier_context=outlier_contexts[metric_key],
                    pre_outlier_context=pre_outlier_contexts.get(metric_key),
                    prepared_cuped_context={
                        **prepared_cuped_context,
                        "comparison_frame": comparison_frame,
                    },
                )
            assert prepared_cuped[0] == pytest.approx(legacy_cuped[0], nan_ok=True)
            assert prepared_cuped[1] == pytest.approx(legacy_cuped[1], nan_ok=True)


def _manual_cuped_statistics_from_frame(
    cuped_frame: pd.DataFrame,
    group_column: str,
    baseline_group: str,
    test_group: str,
) -> tuple[float, float]:
    metric_exp = cuped_frame["metric_exp"].astype(float)
    metric_pre = cuped_frame["metric_pre"].astype(float)
    theta = float(metric_exp.cov(metric_pre) / metric_pre.var(ddof=1))
    adjusted = metric_exp - theta * (metric_pre - float(metric_pre.mean()))
    baseline_values = adjusted[cuped_frame[group_column] == baseline_group]
    test_values = adjusted[cuped_frame[group_column] == test_group]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        p_value = float(
            ttest_ind(test_values, baseline_values, equal_var=False, nan_policy="omit").pvalue
        )
    standard_error = math.sqrt(
        float(baseline_values.var(ddof=1)) / int(baseline_values.shape[0])
        + float(test_values.var(ddof=1)) / int(test_values.shape[0])
    )
    return p_value, standard_error


def _single_metric_row(
    result: pd.DataFrame,
    metric_name: str,
    *,
    group_1: str | None = None,
    group_2: str | None = None,
) -> pd.Series:
    mask = result["metric_name"] == metric_name
    if group_1 is not None:
        mask &= result["group_1"] == group_1
    if group_2 is not None:
        mask &= result["group_2"] == group_2
    rows = result.loc[mask]
    assert rows.shape[0] == 1
    return rows.iloc[0]


def _assert_cuped_row_matches_frame(
    row: pd.Series,
    cuped_frame: pd.DataFrame,
    baseline_group: str,
    test_group: str,
) -> None:
    expected_p_value, expected_standard_error = _manual_cuped_statistics_from_frame(
        cuped_frame=cuped_frame,
        group_column="group_name",
        baseline_group=baseline_group,
        test_group=test_group,
    )
    expected_mde_abs = _compute_mde_from_standard_error(
        standard_error=expected_standard_error,
        alpha=DEFAULT_ALPHA,
        power=DEFAULT_POWER,
    )
    assert row["s.e. CUPED"] == pytest.approx(expected_standard_error)
    assert row["p-value CUPED"] == pytest.approx(expected_p_value)
    assert row["mde_abs CUPED"] == pytest.approx(expected_mde_abs)
    assert row["mde_relative CUPED"] == pytest.approx(
        expected_mde_abs / row["metric_control"]
    )


def _manual_cuped_adjusted_variance(
    metric_exp: pd.Series,
    metric_pre: pd.Series,
) -> float:
    theta = float(metric_exp.cov(metric_pre) / metric_pre.var(ddof=1))
    adjusted = metric_exp - theta * (metric_pre - float(metric_pre.mean()))
    return float(adjusted.var(ddof=1))


def _manual_agg_ratio_linearized_values(
    numerator: pd.Series,
    denominator: pd.Series,
) -> pd.Series:
    valid_mask = numerator.notna() & denominator.notna()
    ratio = float(numerator.loc[valid_mask].sum()) / float(denominator.loc[valid_mask].sum())
    values = pd.Series(np.nan, index=numerator.index, dtype=float)
    values.loc[valid_mask] = numerator.loc[valid_mask] - ratio * denominator.loc[valid_mask]
    return values


def _legacy_metric_test_statistic(
    df: pd.DataFrame,
    group_column: str,
    baseline_group: str,
    test_group: str,
    metric_definition: dict[str, object],
    outlier_context: dict[str, object] | None = None,
) -> float:
    if metric_definition["kind"] == "mean":
        metric_values = _get_numeric_metric_series(df, str(metric_definition["column"]))
        metric_values, _ = _apply_outliers_to_values(metric_values, outlier_context)
        baseline_values = metric_values[df[group_column] == baseline_group].dropna()
        test_values = metric_values[df[group_column] == test_group].dropna()
        statistic, _ = _compute_ttest_stat_and_p_value(baseline_values, test_values)
        return statistic

    ratio_spec = dict(metric_definition["ratio_spec"])
    numerator = _get_numeric_metric_series(df, ratio_spec["numerator"])
    denominator = _get_numeric_metric_series(df, ratio_spec["denominator"])
    valid_mask = _build_ratio_valid_mask(
        numerator=numerator,
        denominator=denominator,
        level=ratio_spec["level"],
    )

    if ratio_spec["level"] == "user":
        ratio_values = pd.Series(np.nan, index=df.index, dtype=float)
        ratio_values.loc[valid_mask] = numerator.loc[valid_mask] / denominator.loc[valid_mask]
        ratio_values, _ = _apply_outliers_to_values(ratio_values, outlier_context)
        baseline_values = ratio_values[df[group_column] == baseline_group].dropna()
        test_values = ratio_values[df[group_column] == test_group].dropna()
        statistic, _ = _compute_ttest_stat_and_p_value(baseline_values, test_values)
        return statistic

    numerator, denominator, _ = _apply_outliers_to_agg_ratio_components(
        numerator=numerator,
        denominator=denominator,
        outlier_context=outlier_context,
    )
    valid_mask = _build_ratio_valid_mask(
        numerator=numerator,
        denominator=denominator,
        level=ratio_spec["level"],
    )
    baseline_mask = (df[group_column] == baseline_group) & valid_mask
    test_mask = (df[group_column] == test_group) & valid_mask
    baseline_frame = pd.DataFrame(
        {"numerator": numerator[baseline_mask], "denominator": denominator[baseline_mask]}
    )
    test_frame = pd.DataFrame(
        {"numerator": numerator[test_mask], "denominator": denominator[test_mask]}
    )
    baseline_stats = _compute_agg_ratio_group_stats(baseline_frame)
    test_stats = _compute_agg_ratio_group_stats(test_frame)
    if math.isnan(test_stats["ratio"]) or math.isnan(baseline_stats["ratio"]):
        return math.nan

    delta_abs = test_stats["ratio"] - baseline_stats["ratio"]
    se_diff = _compute_agg_ratio_diff_standard_error(
        baseline_frame=baseline_frame,
        baseline_ratio=baseline_stats["ratio"],
        test_frame=test_frame,
        test_ratio=test_stats["ratio"],
    )
    return _compute_studentized_statistic(delta_abs, se_diff)


def _legacy_bootstrap_adjustment(
    df: pd.DataFrame,
    *,
    group: str,
    control: str,
    user_id: str,
    ratio_metrics: list[dict[str, object]] | None,
    test_vs_test: bool,
    resamples: int,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "truncate",
) -> pd.DataFrame:
    metric_columns = [column for column in df.columns if column not in {group, user_id}]
    ratio_specs = _normalize_ratio_metrics(df, ratio_metrics, reserved_columns={group, user_id})
    metric_definitions = _build_metric_definitions(metric_columns, ratio_specs)
    outlier_contexts = _build_outlier_contexts(
        df=df,
        metric_definitions=metric_definitions,
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )
    group_names = df[group].drop_duplicates().tolist()
    comparisons = _build_comparisons(group_names, control, test_vs_test=test_vs_test)
    include_groups = len(group_names) > 2

    family_max_statistics: dict[str, list[float]] = {
        str(metric_definition["metric_key"]): []
        for metric_definition in metric_definitions
    }
    rng = np.random.default_rng(0)

    for _ in range(resamples):
        sample_indices = rng.integers(0, len(df), size=len(df))
        bootstrap_df = df.iloc[sample_indices].reset_index(drop=True).copy()
        for metric_definition in metric_definitions:
            metric_key = str(metric_definition["metric_key"])
            comparison_statistics: list[float] = []
            for test_group, baseline_group in comparisons:
                statistic = _legacy_metric_test_statistic(
                    bootstrap_df,
                    group_column=group,
                    baseline_group=baseline_group,
                    test_group=test_group,
                    metric_definition=metric_definition,
                    outlier_context=outlier_contexts[str(metric_definition["metric_key"])],
                )
                if not math.isnan(statistic):
                    comparison_statistics.append(abs(statistic))
            family_max_statistics[metric_key].append(
                max(comparison_statistics) if comparison_statistics else math.nan
            )

    rows: list[dict[str, object]] = []
    for test_group, baseline_group in comparisons:
        for metric_definition in metric_definitions:
            metric_key = str(metric_definition["metric_key"])
            observed_stat = _legacy_metric_test_statistic(
                df,
                group_column=group,
                baseline_group=baseline_group,
                test_group=test_group,
                metric_definition=metric_definition,
                outlier_context=outlier_contexts[metric_key],
            )
            if math.isnan(observed_stat):
                adjusted_p = math.nan
            else:
                bootstrap_stats = [
                    value for value in family_max_statistics[metric_key] if not math.isnan(value)
                ]
                adjusted_p = (
                    sum(value >= abs(observed_stat) for value in bootstrap_stats) / len(bootstrap_stats)
                    if bootstrap_stats
                    else math.nan
                )

            row = {"metric_name": metric_key, "bootstrap_adj_p": adjusted_p}
            if include_groups:
                row["group_1"] = test_group
                row["group_2"] = baseline_group
            rows.append(row)

    columns = ["metric_name", "bootstrap_adj_p"]
    if include_groups:
        columns = ["group_1", "group_2", *columns]
    return pd.DataFrame(rows, columns=columns)


def test_compute_test_metrics_prints_progress_logs(capsys: pytest.CaptureFixture[str]) -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 11, 9, 12, 14, 15, 13, 16],
            "clicks": [5, 6, 4, 5, 8, 9, 7, 8],
            "impressions": [10, 12, 8, 10, 12, 14, 10, 12],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [8, 10, 6, 11, 12, 12, 10, 15],
            "clicks": [4, 5, 3, 4, 6, 7, 5, 6],
            "impressions": [9, 11, 8, 10, 11, 15, 9, 13],
        }
    )

    result = compute_test_metrics(
        df,
        ratio_metrics=[
            {"name": "ctr", "numerator": "clicks", "denominator": "impressions"},
        ],
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=3,
        bootstrap_progress=False,
        pre_exp_metrics_df=pre_df,
    )

    output = capsys.readouterr().out
    assert "compute_test_metrics: start rows=8 metric_columns=3" in output
    assert "ratio_metrics=True cuped=True bootstrap=True" in output
    assert "compute_test_metrics: setup complete groups=2 comparisons=1 metrics=4" in output
    assert "compute_test_metrics: building outlier contexts" in output
    assert "compute_test_metrics: outlier contexts complete" in output
    assert "compute_test_metrics: comparison test vs control" in output
    assert "compute_test_metrics: metric orders (mean)" in output
    assert "compute_test_metrics: metric ctr (ratio)" in output
    assert "compute_test_metrics: CUPED orders (mean)" in output
    assert (
        "compute_test_metrics: bootstrap adjustment start resamples=3 n_jobs=1"
        in output
    )
    assert "compute_test_metrics: bootstrap adjustment complete" in output
    assert f"compute_test_metrics: finish rows={len(result)}" in output


def test_compute_test_metrics_matches_legacy_bootstrap_adjustment_single_thread() -> None:
    df = _build_sample_metrics_df()
    ratio_metrics = [
        {"name": "ctr_user", "numerator": "clicks", "denominator": "impressions"},
        {"name": "ctr_agg", "numerator": "clicks", "denominator": "impressions", "level": "agg"},
    ]

    result = compute_test_metrics(
        df,
        ratio_metrics=ratio_metrics,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=40,
        bootstrap_random_state=0,
        bootstrap_n_jobs=1,
    )
    legacy = _legacy_bootstrap_adjustment(
        df,
        group="group_name",
        control="control",
        user_id="user_id",
        ratio_metrics=ratio_metrics,
        test_vs_test=True,
        resamples=40,
    )

    pd.testing.assert_series_equal(result["metric_name"], legacy["metric_name"])
    pd.testing.assert_series_equal(result["group_1"], legacy["group_1"])
    pd.testing.assert_series_equal(result["group_2"], legacy["group_2"])
    np.testing.assert_allclose(result["bootstrap_adj_p"], legacy["bootstrap_adj_p"], equal_nan=True)


def test_compute_test_metrics_adds_metric_control_and_metric_test_columns() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(df, test_vs_test=False)

    assert result.columns.tolist()[:14] == [
        "metric_type",
        "group_1",
        "group_2",
        "metric_name",
        "n0",
        "n1",
        "outliers_cutoff",
        "outliers_n_control",
        "outliers_n_test",
        "metric_control",
        "metric_test",
        "variance_control",
        "variance_test",
        "delta_abs",
    ]
    assert result.columns[result.columns.get_loc("mde_relative") + 1] == "s.e."
    assert result.columns[result.columns.get_loc("s.e.") + 1] == "p-value"

    orders_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "orders")
    ].iloc[0]
    orders_cutoff = float(df["orders"].quantile(0.999))
    assert orders_row["metric_type"] == "mean"
    assert orders_row["metric_control"] == pytest.approx((10 + 12 + 9) / 3)
    assert orders_row["metric_test"] == pytest.approx((13 + orders_cutoff + 11 + 14) / 4)
    assert orders_row["outliers_cutoff"] == pytest.approx(orders_cutoff)
    assert orders_row["outliers_n_control"] == 0
    assert orders_row["outliers_n_test"] == 1
    control_values = pd.Series([10, 12, 9], dtype=float)
    test_values = pd.Series([13, orders_cutoff, 11, 14], dtype=float)
    expected_control_variance = control_values.var(ddof=1)
    expected_test_variance = test_values.var(ddof=1)
    assert orders_row["variance_control"] == pytest.approx(expected_control_variance)
    assert orders_row["variance_test"] == pytest.approx(expected_test_variance)
    assert orders_row["s.e."] == pytest.approx(
        math.sqrt((expected_control_variance / 3) + (expected_test_variance / 4))
    )


def test_compute_test_metrics_uses_raw_relative_fields() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(df, test_vs_test=False)

    assert "delta_relative" in result.columns
    assert "mde_relative" in result.columns
    assert "mde_abs CUPED" not in result.columns
    assert "mde_relative CUPED" not in result.columns
    assert "uplift" not in result.columns
    assert "mde_percentage" not in result.columns

    orders_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "orders")
    ].iloc[0]
    orders_cutoff = float(df["orders"].quantile(0.999))
    expected_control = (10 + 12 + 9) / 3
    expected_test = (13 + orders_cutoff + 11 + 14) / 4
    expected_delta_abs = expected_test - expected_control
    assert orders_row["delta_relative"] == pytest.approx(expected_delta_abs / expected_control)


def test_compute_mde_estimates_mean_metric_from_user_day_window() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                ]
            ),
            "orders": [
                1.0,
                2.0,
                3.0,
                4.0,
                3.0,
                4.0,
                6.0,
                8.0,
                5.0,
                6.0,
                10.0,
                13.0,
            ],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        control_share=0.6,
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "orders")
    pre_values = pd.Series([3.0, 7.0, 11.0])
    user_values = pd.Series([7.0, 14.0, 23.0])
    expected_variance = float(user_values.var(ddof=1))
    expected_se = math.sqrt((expected_variance / 6) + (expected_variance / 4))
    expected_mde = _compute_mde_from_standard_error(
        standard_error=expected_se,
        alpha=DEFAULT_ALPHA,
        power=DEFAULT_POWER,
    )
    expected_cuped_variance = _manual_cuped_adjusted_variance(
        metric_exp=user_values,
        metric_pre=pre_values,
    )
    expected_cuped_se = math.sqrt(
        (expected_cuped_variance / 6) + (expected_cuped_variance / 4)
    )
    expected_cuped_mde = _compute_mde_from_standard_error(
        standard_error=expected_cuped_se,
        alpha=DEFAULT_ALPHA,
        power=DEFAULT_POWER,
    )
    assert list(result.columns) == [
        "metric_name",
        "avg",
        "var",
        "days",
        "pre_exp_days",
        "group_size",
        "control_share",
        "mde_abs",
        "mde_relative",
        "mde_abs_cuped",
        "mde_relative_cuped",
    ]
    assert row["avg"] == pytest.approx(float(user_values.mean()))
    assert row["var"] == pytest.approx(expected_variance)
    assert row["days"] == 2
    assert row["pre_exp_days"] == 2
    assert row["group_size"] == 10
    assert row["control_share"] == pytest.approx(0.6)
    assert row["mde_abs"] == pytest.approx(expected_mde)
    assert row["mde_relative"] == pytest.approx(expected_mde / float(user_values.mean()))
    assert row["mde_abs_cuped"] == pytest.approx(expected_cuped_mde)
    assert row["mde_relative_cuped"] == pytest.approx(
        expected_cuped_mde / float(user_values.mean())
    )


def test_compute_mde_accepts_explicit_list_grids_sorted_unique() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 2
            ),
            "orders": [1.0, 2.0, 3.0, 4.0, 3.0, 4.0, 5.0, 6.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[20, 10, 20],
        exp_days=[2, 1, 1],
        start_dt="2024-01-03",
    )

    assert result["days"].tolist() == [1, 1, 2, 2]
    assert result["group_size"].tolist() == [10, 20, 10, 20]


def test_compute_mde_accepts_min_max_step_grids() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2],
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-06",
                ]
                * 2
            ),
            "orders": [
                1.0,
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                7.0,
            ],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        min_group_size=10,
        max_group_size=25,
        group_size_step=10,
        min_days=1,
        max_days=3,
        days_step=2,
        start_dt="2024-01-04",
    )

    assert result["days"].tolist() == [1, 1, 3, 3]
    assert result["group_size"].tolist() == [10, 20, 10, 20]


def test_compute_mde_accepts_max_aggregation_for_mean_metric() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3
            ),
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["converted"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        max_agg_metrics=["converted"],
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "converted")
    expected_values = pd.Series([1.0, 0.0, 1.0])
    assert row["avg"] == pytest.approx(float(expected_values.mean()))
    assert row["var"] == pytest.approx(float(expected_values.var(ddof=1)))


def test_compute_mde_default_non_zero_truncate_keeps_sparse_positive_values() -> None:
    user_ids = list(range(1, 11))
    df = pd.DataFrame(
        {
            "user_id": user_ids + user_ids,
            "dt": pd.to_datetime(["2024-01-01"] * 10 + ["2024-01-02"] * 10),
            "revenue": [0.0] * 9 + [100.0] + [0.0] * 9 + [100.0],
        }
    )

    default_result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["revenue"],
        group_sizes=[10],
        exp_days=[1],
        start_dt="2024-01-02",
        outliers_quantile=0.8,
    )
    with pytest.warns(UserWarning, match="pre-experiment covariate variance"):
        truncate_result = compute_mde(
            df,
            user_id="user_id",
            metric_columns=["revenue"],
            group_sizes=[10],
            exp_days=[1],
            start_dt="2024-01-02",
            outliers_quantile=0.8,
            outliers_policy="truncate",
        )

    default_row = _single_metric_row(default_result, "revenue")
    truncate_row = _single_metric_row(truncate_result, "revenue")
    assert default_row["avg"] == pytest.approx(10.0)
    assert default_row["var"] > 0
    assert not math.isnan(float(default_row["mde_abs"]))
    assert truncate_row["avg"] == pytest.approx(0.0)
    assert truncate_row["var"] == pytest.approx(0.0)
    assert math.isnan(float(truncate_row["mde_abs"]))


def test_compute_mde_sum_aggregation_list_makes_other_metrics_use_max() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3
            ),
            "orders": [
                1.0,
                2.0,
                3.0,
                4.0,
                3.0,
                4.0,
                6.0,
                8.0,
                5.0,
                6.0,
                10.0,
                13.0,
            ],
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders", "converted"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        sum_agg_metrics=["orders"],
        outliers_quantile=1,
    )

    orders_row = _single_metric_row(result, "orders")
    converted_row = _single_metric_row(result, "converted")
    assert orders_row["avg"] == pytest.approx(float(pd.Series([7.0, 14.0, 23.0]).mean()))
    assert converted_row["avg"] == pytest.approx(float(pd.Series([1.0, 0.0, 1.0]).mean()))


def test_compute_mde_accepts_ratio_spec_dataclass_for_user_ratio() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3
            ),
            "clicks": [
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                2.0,
                2.0,
                5.0,
                5.0,
                3.0,
                3.0,
            ],
            "impressions": [10.0] * 12,
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="ctr_user",
                numerator="clicks",
                denominator="impressions",
                level="user",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "ctr_user")
    ratio_values = pd.Series([0.1, 0.2, 0.3])
    assert row["avg"] == pytest.approx(float(ratio_values.mean()))
    assert row["var"] == pytest.approx(float(ratio_values.var(ddof=1)))
    assert not math.isnan(float(row["mde_abs_cuped"]))
    assert not math.isnan(float(row["mde_relative_cuped"]))


def test_compute_mde_applies_aggregation_policy_to_user_ratio_components() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3
            ),
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            "visits": [10.0] * 12,
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="conversion_rate",
                numerator="converted",
                denominator="visits",
                level="user",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        max_agg_metrics=["converted"],
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "conversion_rate")
    ratio_values = pd.Series([1.0 / 20.0, 0.0, 1.0 / 20.0])
    assert row["avg"] == pytest.approx(float(ratio_values.mean()))
    assert row["var"] == pytest.approx(float(ratio_values.var(ddof=1)))


def test_compute_mde_computes_agg_ratio_delta_method_unit_variance() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3
            ),
            "clicks": [
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                2.0,
                2.0,
                5.0,
                5.0,
                3.0,
                3.0,
            ],
            "impressions": [
                10.0,
                10.0,
                5.0,
                5.0,
                10.0,
                10.0,
                5.0,
                5.0,
                10.0,
                10.0,
                15.0,
                15.0,
            ],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="ctr_agg",
                numerator="clicks",
                denominator="impressions",
                level="agg",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "ctr_agg")
    numerator = pd.Series([2.0, 4.0, 6.0])
    denominator = pd.Series([10.0, 10.0, 30.0])
    ratio = float(numerator.sum() / denominator.sum())
    centered = numerator - ratio * denominator
    expected_variance = float(centered.var(ddof=1)) / float(denominator.mean()) ** 2
    assert row["avg"] == pytest.approx(ratio)
    assert row["var"] == pytest.approx(expected_variance)
    assert not math.isnan(float(row["mde_abs_cuped"]))
    assert not math.isnan(float(row["mde_relative_cuped"]))


def test_compute_mde_applies_aggregation_policy_to_agg_ratio_components() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3
            ),
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            "visits": [10.0] * 12,
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="conversion_rate",
                numerator="converted",
                denominator="visits",
                level="agg",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        max_agg_metrics=["converted"],
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "conversion_rate")
    numerator = pd.Series([1.0, 0.0, 1.0])
    denominator = pd.Series([20.0, 20.0, 20.0])
    ratio = float(numerator.sum() / denominator.sum())
    centered = numerator - ratio * denominator
    expected_variance = float(centered.var(ddof=1)) / float(denominator.mean()) ** 2
    assert row["avg"] == pytest.approx(ratio)
    assert row["var"] == pytest.approx(expected_variance)


def test_compute_mde_defaults_to_first_historical_date_and_accepts_start_dt() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2] * 6,
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-05",
                    "2024-01-06",
                    "2024-01-06",
                ]
            ),
            "orders": [
                1.0,
                3.0,
                1.0,
                3.0,
                10.0,
                20.0,
                10.0,
                20.0,
                100.0,
                300.0,
                100.0,
                300.0,
            ],
        }
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE"):
        default_result = compute_mde(
            df,
            user_id="user_id",
                metric_columns=["orders"],
                group_sizes=[10],
                exp_days=[2],
                start_dt=None,
                outliers_quantile=1,
        )
    explicit_result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-05",
        outliers_quantile=1,
    )

    assert _single_metric_row(default_result, "orders")["avg"] == pytest.approx(4.0)
    assert _single_metric_row(explicit_result, "orders")["avg"] == pytest.approx(400.0)


def test_compute_mde_rejects_start_dt_outside_history() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2] * 6,
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-05",
                    "2024-01-06",
                    "2024-01-06",
                ]
            ),
            "orders": [
                1.0,
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                7.0,
                8.0,
                9.0,
                10.0,
                11.0,
                12.0,
            ],
        }
    )

    with pytest.raises(ValueError, match="before the first available historical date"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[2],
            start_dt="2023-12-31",
            outliers_quantile=1,
        )
    with pytest.raises(ValueError, match="exceeds the available historical span"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[2],
            start_dt="2024-01-06",
            outliers_quantile=1,
        )


def test_compute_mde_uses_explicit_pre_exp_days_for_cuped_window() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 2, 2, 2, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"] * 3),
            "orders": [1.0, 10.0, 10.0, 3.0, 20.0, 30.0, 6.0, 40.0, 60.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[12],
        exp_days=[2],
        start_dt="2024-01-02",
        pre_exp_days=1,
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "orders")
    expected_values = pd.Series([20.0, 50.0, 100.0])
    assert row["pre_exp_days"] == 1
    assert row["avg"] == pytest.approx(float(expected_values.mean()))
    assert not math.isnan(float(row["mde_abs_cuped"]))


def test_compute_mde_warns_and_returns_nan_when_cuped_window_is_unavailable() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 2, 2],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02"] * 2),
            "orders": [1.0, 2.0, 3.0, 4.0],
        }
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE"):
        result = compute_mde(
            df,
            user_id="user_id",
                metric_columns=["orders"],
                group_sizes=[10],
                exp_days=[2],
                start_dt=None,
                outliers_quantile=1,
        )

    row = _single_metric_row(result, "orders")
    assert row["avg"] == pytest.approx(5.0)
    assert math.isnan(float(row["mde_abs_cuped"]))
    assert math.isnan(float(row["mde_relative_cuped"]))


def test_compute_mde_defaults_user_id_argument() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 1, 2],
            "dt": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02"],
            "orders": [10.0, 12.0, 14.0, 18.0],
        }
    )

    result = compute_mde(
        df,
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[1],
        start_dt="2024-01-02",
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "orders")
    assert row["avg"] == pytest.approx(16.0)
    assert row["var"] == pytest.approx(8.0)


def test_compute_mde_from_sql_matches_dataframe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3
            ),
            "orders": [1.0, 2.0, 3.0, 4.0, 3.0, 4.0, 6.0, 8.0, 5.0, 6.0, 10.0, 13.0],
        }
    )
    expected = compute_mde(
        source_df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
    )
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    queries: list[str] = []

    def fake_table_info(db_key: str, table: str) -> SimpleNamespace:
        assert db_key == "analytics"
        assert table == "sandbox.events"
        return table_info

    def fake_read(
        db_key: str,
        query: str,
        **kwargs: object,
    ) -> pd.DataFrame:
        assert db_key == "analytics"
        assert kwargs["query_label"] == "mde"
        queries.append(query)
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [len(source_df)],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-04")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        assert kwargs["concurrency"] == 1
        frames: dict[str, pd.DataFrame] = {}
        for task in tasks:
            assert task["db_key"] == "analytics"
            assert task["query_label"] == "mde"
            query = task["query"]
            assert isinstance(query, str)
            queries.append(query)
            if 'CAST("dt" AS DATE) >= DATE \'2024-01-03\'' in query:
                frames[str(task["name"])] = pd.DataFrame(
                    {"user_id": [1, 2, 3], "orders": [7.0, 14.0, 23.0]}
                )
                continue
            if 'CAST("dt" AS DATE) >= DATE \'2024-01-01\'' in query:
                frames[str(task["name"])] = pd.DataFrame(
                    {"user_id": [1, 2, 3], "orders": [3.0, 7.0, 11.0]}
                )
                continue
            raise AssertionError(f"Unexpected aggregate query:\n{query}")
        return frames

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        fake_table_info,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.read",
        fake_read,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    result = compute_mde_from_sql(
        "analytics",
        "sandbox.events",
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
        query_label="mde",
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any('FROM "sandbox"."events"' in query for query in queries)


def test_compute_mde_from_sql_applies_where_to_validation_and_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    queries: list[str] = []

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del db_key, kwargs
        queries.append(query)
        assert "(country = 'US')" in query
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [4],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-02")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        del kwargs
        frames: dict[str, pd.DataFrame] = {}
        for task in tasks:
            query = task["query"]
            assert isinstance(query, str)
            queries.append(query)
            assert "(country = 'US')" in query
            frames[str(task["name"])] = pd.DataFrame(
                {"user_id": [1, 2], "orders": [10.0, 12.0]}
            )
        return frames

    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            sql_where="country = 'US'",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            outliers_quantile=1,
        )

    assert len(queries) == 3


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_compute_mde_from_sql_rejects_invalid_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            concurrency=concurrency,
        )


def test_compute_mde_from_sql_parallelizes_day_size_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [1, 1, 2, 2, 3, 3],
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-01",
                    "2024-01-02",
                ]
            ),
            "orders": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        expected = compute_mde(
            source_df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10, 20],
            exp_days=[1, 2],
            start_dt=None,
            outliers_quantile=1,
        )

    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    events: list[str] = []
    active_loads = 0
    max_active_loads = 0
    active_compute_tasks = 0
    max_active_compute_tasks = 0
    lock = threading.Lock()
    parallel_kwargs: dict[str, object] = {}
    compute_calls: list[tuple[int, int]] = []
    real_compute_task = planning_module._compute_sql_mde_day_size_rows

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del kwargs
        assert db_key == "analytics"
        if "COUNT(*) AS row_count" in query:
            with lock:
                events.append("validation:stats")
            return pd.DataFrame(
                {
                    "row_count": [len(source_df)],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-02")],
                }
            )
        if "duplicate_user_day_rows" in query:
            with lock:
                events.append("validation:duplicates")
            return pd.DataFrame({"duplicate_user_day_rows": [0]})

        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        nonlocal active_loads, max_active_loads
        task_list = list(tasks)
        with lock:
            assert events == ["validation:stats", "validation:duplicates"]
            events.append("parallel_sql")
        parallel_kwargs.update(kwargs)
        assert len(task_list) == 2

        def run_task(task: dict[str, object]) -> tuple[str, pd.DataFrame]:
            nonlocal active_loads, max_active_loads
            assert task["type"] == "read"
            assert task["db_key"] == "analytics"
            query = task["query"]
            assert isinstance(query, str)
            with lock:
                events.append("aggregate")
                active_loads += 1
                max_active_loads = max(max_active_loads, active_loads)
            try:
                time.sleep(0.02)
                if 'CAST("dt" AS DATE) < DATE \'2024-01-02\'' in query:
                    frame = pd.DataFrame(
                        {"user_id": [1, 2, 3], "orders": [1.0, 3.0, 5.0]}
                    )
                elif 'CAST("dt" AS DATE) < DATE \'2024-01-03\'' in query:
                    frame = pd.DataFrame(
                        {"user_id": [1, 2, 3], "orders": [3.0, 7.0, 11.0]}
                    )
                else:
                    raise AssertionError(f"Unexpected aggregate query:\n{query}")
                return str(task["name"]), frame
            finally:
                with lock:
                    active_loads -= 1

        with planning_module.ThreadPoolExecutor(
            max_workers=int(kwargs["concurrency"]),
        ) as executor:
            return dict(executor.map(run_task, task_list))

    def recording_compute_task(*args: object, **kwargs: object) -> object:
        nonlocal active_compute_tasks, max_active_compute_tasks
        with lock:
            assert "parallel_sql" in events
            active_compute_tasks += 1
            max_active_compute_tasks = max(
                max_active_compute_tasks,
                active_compute_tasks,
            )
            compute_calls.append((int(kwargs["days"]), int(kwargs["split"]["group_size"])))
        try:
            time.sleep(0.02)
            return real_compute_task(*args, **kwargs)
        finally:
            with lock:
                active_compute_tasks -= 1

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )
    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )
    monkeypatch.setattr(
        planning_module,
        "_compute_sql_mde_day_size_rows",
        recording_compute_task,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        result = compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10, 20],
            exp_days=[1, 2],
            start_dt=None,
            outliers_quantile=1,
            concurrency=2,
        )

    pd.testing.assert_frame_equal(result, expected)
    assert parallel_kwargs == {
        "concurrency": 2,
        "fail_fast": True,
        "progress": False,
        "hard_concurrency_cap": 10,
    }
    assert max_active_loads == 2
    assert events.count("aggregate") == 2
    assert max_active_compute_tasks == 2
    assert sorted(compute_calls) == [(1, 10), (1, 20), (2, 10), (2, 20)]
    cuped_warnings = [
        warning
        for warning in caught
        if "Could not compute CUPED MDE" in str(warning.message)
    ]
    assert len(cuped_warnings) == 2


def test_compute_mde_from_sql_parallel_load_raises_sql_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    parallel_kwargs: dict[str, object] = {}

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del kwargs
        assert db_key == "analytics"
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [2],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-01")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        parallel_kwargs.update(kwargs)
        return {
            str(task["name"]): pd.DataFrame(
                {"user_id": [1, 2], "orders": [1.0, 2.0]},
            )
            for task in tasks
        }

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )
    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            outliers_quantile=1,
            concurrency=11,
        )

    assert parallel_kwargs["concurrency"] == 11
    assert parallel_kwargs["hard_concurrency_cap"] == 11


def test_compute_mde_from_sql_rejects_missing_table_or_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_table = SimpleNamespace(
        exists=False,
        columns={},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: missing_table,
    )
    with pytest.raises(ValueError, match="does not exist"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )

    missing_column = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: missing_column,
    )
    with pytest.raises(ValueError, match="Missing metric column"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_from_sql_rejects_nulls_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )

    def fake_null_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del db_key, kwargs
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [4],
                    "null_user_rows": [1],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-02")],
                }
            )
        raise AssertionError("duplicate query should not run after null validation fails")

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.read",
        fake_null_read,
    )
    with pytest.raises(ValueError, match="must not contain missing values"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )

    def fake_duplicate_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del db_key, kwargs
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [4],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-02")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [1]})
        raise AssertionError("aggregate query should not run after duplicate validation fails")

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.read",
        fake_duplicate_read,
    )
    with pytest.raises(ValueError, match="unique user-day rows"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_rejects_invalid_grid_inputs() -> None:
    df = pd.DataFrame(
        {"user_id": [1, 2], "dt": ["2024-01-01", "2024-01-01"], "orders": [10.0, 12.0]}
    )

    with pytest.raises(ValueError, match="group_sizes cannot be combined"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            min_group_size=10,
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="Either group_sizes"):
        compute_mde(df, user_id="user_id", exp_days=[1], start_dt=None)
    with pytest.raises(ValueError, match="exp_days cannot be combined"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            min_days=1,
            start_dt=None,
        )
    with pytest.raises(ValueError, match="control_share"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            control_share=1.0,
        )
    with pytest.raises(ValueError, match="at least one control and one test user"):
        compute_mde(df, user_id="user_id", group_sizes=[1], exp_days=[1], start_dt=None)
    with pytest.raises(ValueError, match="pre_exp_days"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            pre_exp_days=0,
        )
    with pytest.raises(TypeError, match="pre_exp_days"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            pre_exp_days=True,
        )


def test_compute_mde_rejects_invalid_aggregation_policy_inputs() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2],
            "dt": ["2024-01-01", "2024-01-01"],
            "orders": [10.0, 12.0],
            "converted": [1.0, 0.0],
        }
    )

    with pytest.raises(ValueError, match="Only one of sum_agg_metrics or max_agg_metrics"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders", "converted"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            sum_agg_metrics=["orders"],
            max_agg_metrics=["converted"],
        )
    with pytest.raises(ValueError, match="max_agg_metrics must not contain duplicates"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders", "converted"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            max_agg_metrics=["converted", "converted"],
        )
    with pytest.raises(ValueError, match="unknown metric column"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            max_agg_metrics=["converted"],
        )


def test_compute_mde_rejects_invalid_user_day_grain() -> None:
    missing_user = pd.DataFrame(
        {"user_id": [1, None], "dt": ["2024-01-01", "2024-01-01"], "orders": [1.0, 2.0]}
    )
    missing_date = pd.DataFrame({"user_id": [1, 2], "dt": ["2024-01-01", None], "orders": [1.0, 2.0]})
    invalid_date = pd.DataFrame(
        {"user_id": [1, 2], "dt": ["2024-01-01", "not-a-date"], "orders": [1.0, 2.0]}
    )
    duplicate_user_day = pd.DataFrame(
        {
            "user_id": [1, 1],
            "dt": ["2024-01-01 01:00:00", "2024-01-01 23:00:00"],
            "orders": [1.0, 2.0],
        }
    )

    with pytest.raises(ValueError, match="must not contain missing values"):
        compute_mde(
            missing_user,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="must not contain missing values"):
        compute_mde(
            missing_date,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="datelike"):
        compute_mde(
            invalid_date,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="unique user-day rows"):
        compute_mde(
            duplicate_user_day,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_rejects_ratio_name_conflicting_with_mean_metric() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3],
            "dt": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "ctr": [0.1, 0.2, 0.3],
            "clicks": [1.0, 2.0, 3.0],
            "impressions": [10.0, 10.0, 10.0],
        }
    )

    with pytest.raises(ValueError, match="conflicts with a mean metric column"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            ratio_metrics=[
                RatioMetricSpec(
                    name="ctr",
                    numerator="clicks",
                    denominator="impressions",
                    level="user",
                )
            ],
        )


def test_mde_planning_options_is_no_longer_exported() -> None:
    assert not hasattr(ab_utils, "MdePlanningOptions")
    assert not hasattr(ab_metrics, "MdePlanningOptions")
    assert not hasattr(ab_utils, "compute_mde_only")
    assert not hasattr(ab_metrics, "compute_mde_only")
    assert hasattr(ab_utils, "compute_mde")
    assert hasattr(ab_metrics, "compute_mde")


def test_compute_test_metrics_accepts_ratio_spec_dataclass() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4, 5, 6],
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "clicks": [1, 2, 3, 2, 3, 4],
            "impressions": [10, 10, 10, 10, 10, 10],
        }
    )

    result = compute_test_metrics(
        df,
        ratio_metrics=[
            RatioMetricSpec(
                name="ctr_user",
                numerator="clicks",
                denominator="impressions",
                level="user",
            )
        ],
        test_vs_test=False,
    )

    assert "ctr_user" in set(result["metric_name"])


def test_ratio_metrics_default_to_agg_level() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(
        df,
        ratio_metrics=[{"name": "ctr", "numerator": "clicks", "denominator": "impressions"}],
        test_vs_test=False,
    )

    ratio_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "ctr")
    ].iloc[0]
    assert ratio_row["metric_type"] == "ratio"
    assert ratio_row["metric_control"] == pytest.approx((5 + 3 + 4 + 2) / (10 + 8 + 0 + 4))
    assert ratio_row["metric_test"] == pytest.approx((7 + 5 + 6 + 8) / (14 + 10 + 12 + 16))

    numerator = _get_numeric_metric_series(df, "clicks")
    denominator = _get_numeric_metric_series(df, "impressions")
    valid_mask = _build_ratio_valid_mask(numerator=numerator, denominator=denominator, level="agg")
    baseline_frame = pd.DataFrame(
        {
            "numerator": numerator[(df["group_name"] == "control") & valid_mask],
            "denominator": denominator[(df["group_name"] == "control") & valid_mask],
        }
    )
    test_frame = pd.DataFrame(
        {
            "numerator": numerator[(df["group_name"] == "test_a") & valid_mask],
            "denominator": denominator[(df["group_name"] == "test_a") & valid_mask],
        }
    )
    baseline_stats = _compute_agg_ratio_group_stats(baseline_frame)
    test_stats = _compute_agg_ratio_group_stats(test_frame)
    expected_control_variance = _compute_agg_ratio_variance(
        baseline_frame,
        baseline_stats["ratio"],
    )
    expected_test_variance = _compute_agg_ratio_variance(test_frame, test_stats["ratio"])
    assert ratio_row["variance_control"] == pytest.approx(expected_control_variance)
    assert ratio_row["variance_test"] == pytest.approx(expected_test_variance)
    assert ratio_row["s.e."] == pytest.approx(
        _compute_agg_ratio_diff_standard_error(
            baseline_frame=baseline_frame,
            baseline_ratio=baseline_stats["ratio"],
            test_frame=test_frame,
            test_ratio=test_stats["ratio"],
        )
    )


def test_compute_test_metrics_drop_outliers_updates_counts() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 7)),
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [1, 2, 100, 3, 4, 200],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.8,
        outliers_policy="drop",
    )

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    assert orders_row["outliers_cutoff"] == pytest.approx(float(df["orders"].quantile(0.8)))
    assert orders_row["outliers_n_control"] == 0
    assert orders_row["outliers_n_test"] == 1
    assert orders_row["n0"] == 3
    assert orders_row["n1"] == 2
    assert orders_row["metric_test"] == pytest.approx((3 + 4) / 2)


def test_compute_test_metrics_uses_global_outlier_cutoff_across_groups() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [1, 2, 100, 200],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.75,
    )

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    cutoff = float(df["orders"].quantile(0.75))
    assert cutoff == pytest.approx(125.0)
    assert orders_row["outliers_cutoff"] == pytest.approx(cutoff)
    assert orders_row["metric_test"] == pytest.approx((100 + cutoff) / 2)


def test_compute_test_metrics_default_non_zero_truncate_ignores_zeros_for_cutoff() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 11)),
            "group_name": ["control"] * 5 + ["test"] * 5,
            "orders": [0.0] * 9 + [100.0],
        }
    )

    default_result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.8,
    )
    truncate_result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.8,
        outliers_policy="truncate",
    )

    default_row = _single_metric_row(default_result, "orders")
    truncate_row = _single_metric_row(truncate_result, "orders")
    assert default_row["outliers_cutoff"] == pytest.approx(100.0)
    assert default_row["outliers_n_test"] == 0
    assert default_row["metric_test"] == pytest.approx(20.0)
    assert truncate_row["outliers_cutoff"] == pytest.approx(0.0)
    assert truncate_row["outliers_n_test"] == 1
    assert truncate_row["metric_test"] == pytest.approx(0.0)


def test_compute_test_metrics_accepts_non_zero_truncate_explicitly() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        outliers_policy="non_zero_truncate",
    )

    assert not result.empty


def test_compute_test_metrics_user_ratio_outliers_truncate_and_drop() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "clicks": [1, 2, 3, 100],
            "impressions": [10, 10, 10, 10],
        }
    )
    ratio_metrics = [
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "impressions",
            "level": "user",
        }
    ]

    truncate_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="truncate",
    )
    drop_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="drop",
    )

    cutoff = float(pd.Series([0.1, 0.2, 0.3, 10.0]).quantile(0.75))
    truncate_row = truncate_result[truncate_result["metric_name"] == "ctr_user"].iloc[0]
    drop_row = drop_result[drop_result["metric_name"] == "ctr_user"].iloc[0]
    assert truncate_row["outliers_cutoff"] == pytest.approx(cutoff)
    assert truncate_row["outliers_n_test"] == 1
    assert truncate_row["metric_test"] == pytest.approx((0.3 + cutoff) / 2)
    assert truncate_row["n1"] == 2
    assert drop_row["metric_test"] == pytest.approx(0.3)
    assert drop_row["n1"] == 1


def test_compute_test_metrics_user_ratio_default_non_zero_truncate() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 11)),
            "group_name": ["control"] * 5 + ["test"] * 5,
            "clicks": [0.0] * 9 + [100.0],
            "impressions": [10.0] * 10,
        }
    )
    ratio_metrics = [
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "impressions",
            "level": "user",
        }
    ]

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.8,
    )

    row = _single_metric_row(result, "ctr_user")
    assert row["outliers_cutoff"] == pytest.approx(10.0)
    assert row["outliers_n_test"] == 0
    assert row["metric_test"] == pytest.approx(2.0)


def test_compute_test_metrics_agg_ratio_outliers_drop_and_truncate() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "clicks": [1, 2, 3, 100],
            "impressions": [10, 10, 10, 10],
        }
    )
    ratio_metrics = [{"name": "ctr", "numerator": "clicks", "denominator": "impressions"}]

    truncate_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="truncate",
    )
    drop_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="drop",
    )

    cutoff = float(pd.Series([0.1, 0.2, 0.3, 10.0]).quantile(0.75))
    truncate_row = truncate_result[truncate_result["metric_name"] == "ctr"].iloc[0]
    drop_row = drop_result[drop_result["metric_name"] == "ctr"].iloc[0]
    assert truncate_row["outliers_cutoff"] == pytest.approx(cutoff)
    assert truncate_row["outliers_n_test"] == 1
    assert truncate_row["metric_test"] == pytest.approx((3 + cutoff * 10) / 20)
    assert truncate_row["n1"] == 2
    assert drop_row["metric_test"] == pytest.approx(3 / 10)
    assert drop_row["n1"] == 1


def test_compute_test_metrics_agg_ratio_default_non_zero_truncate() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 11)),
            "group_name": ["control"] * 5 + ["test"] * 5,
            "clicks": [0.0] * 9 + [100.0],
            "impressions": [10.0] * 10,
        }
    )
    ratio_metrics = [{"name": "ctr", "numerator": "clicks", "denominator": "impressions"}]

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.8,
    )

    row = _single_metric_row(result, "ctr")
    assert row["outliers_cutoff"] == pytest.approx(10.0)
    assert row["outliers_n_test"] == 0
    assert row["metric_test"] == pytest.approx(2.0)


@pytest.mark.filterwarnings(
    "ignore:Precision loss occurred in moment calculation:RuntimeWarning"
)
def test_compute_test_metrics_parallel_bootstrap_is_reproducible() -> None:
    df = _build_sample_metrics_df()

    first = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=2,
    )
    second = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=2,
    )

    pd.testing.assert_frame_equal(first, second)
    assert first.columns[first.columns.get_loc("p-value") + 1] == "s.e. bootstrap"
    assert first.columns[first.columns.get_loc("s.e. bootstrap") + 1] == "bootstrap_adj_p"
    orders_row = first[
        (first["group_1"] == "test_a")
        & (first["group_2"] == "control")
        & (first["metric_name"] == "orders")
    ].iloc[0]
    assert not math.isnan(float(orders_row["s.e. bootstrap"]))


def test_compute_test_metrics_accepts_bootstrap_progress() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=5,
        bootstrap_random_state=0,
        bootstrap_progress=True,
    )

    assert "s.e. bootstrap" in result.columns
    assert "bootstrap_adj_p" in result.columns


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    [
        ({"bootstrap_random_state": True}, TypeError, "bootstrap_random_state must be an integer or None"),
        ({"bootstrap_n_jobs": 0}, ValueError, "bootstrap_n_jobs must be positive"),
        ({"bootstrap_n_jobs": True}, TypeError, "bootstrap_n_jobs must be an integer"),
        ({"bootstrap_progress": 1}, TypeError, "bootstrap_progress must be a boolean"),
    ],
)
def test_compute_test_metrics_validates_bootstrap_parameters(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    df = _build_sample_metrics_df()

    with pytest.raises(error_type, match=message):
        compute_test_metrics(df, **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    [
        (
            {"outliers_quantile": 0},
            ValueError,
            "outliers_quantile must be greater than 0 and less than or equal to 1",
        ),
        ({"outliers_quantile": True}, TypeError, "outliers_quantile must be numeric"),
        ({"outliers_quantile": "0.9"}, TypeError, "outliers_quantile must be numeric"),
        (
            {"outliers_policy": "winsorize"},
            ValueError,
            "outliers_policy must be 'truncate', 'drop', or 'non_zero_truncate'",
        ),
        ({"outliers_policy": None}, TypeError, "outliers_policy must be a string"),
    ],
)
def test_compute_test_metrics_validates_outlier_parameters(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    df = _build_sample_metrics_df()

    with pytest.raises(error_type, match=message):
        compute_test_metrics(df, **kwargs)


def test_compute_test_metrics_accepts_outliers_quantile_one_without_truncation() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [10.0, 20.0, 30.0, 1000.0],
        }
    )

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        outliers_quantile=1,
    )

    orders_row = _single_metric_row(result, "orders")
    assert orders_row["outliers_cutoff"] == pytest.approx(1000.0)
    assert orders_row["outliers_n_control"] == 0
    assert orders_row["outliers_n_test"] == 0
    assert orders_row["metric_test"] == pytest.approx(515.0)


def test_compute_test_metrics_adds_cuped_p_value_for_mean_metrics() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 11, 9, 12, 14, 15, 13, 16],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [8, 10, 6, 11, 12, 12, 10, 15],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    assert result.columns[result.columns.get_loc("p-value") + 1] == "s.e. CUPED"
    assert result.columns[result.columns.get_loc("s.e. CUPED") + 1] == "p-value CUPED"
    assert result.columns[result.columns.get_loc("p-value CUPED") + 1] == "mde_abs CUPED"
    assert (
        result.columns[result.columns.get_loc("mde_abs CUPED") + 1]
        == "mde_relative CUPED"
    )
    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    expected_mde_abs = _compute_mde_from_standard_error(
        standard_error=orders_row["s.e. CUPED"],
        alpha=DEFAULT_ALPHA,
        power=DEFAULT_POWER,
    )
    assert not math.isnan(float(orders_row["s.e. CUPED"]))
    assert not math.isnan(float(orders_row["p-value CUPED"]))
    assert orders_row["mde_abs CUPED"] == pytest.approx(expected_mde_abs)
    assert orders_row["mde_relative CUPED"] == pytest.approx(
        expected_mde_abs / orders_row["metric_control"]
    )


def test_compute_cuped_statistics_from_frame_matches_manual_cuped_welch_ttest() -> None:
    cuped_frame = pd.DataFrame(
        {
            "group_name": ["control"] * 4 + ["test"] * 4,
            "metric_exp": [11.0, 14.0, 13.0, 17.0, 16.0, 19.0, 18.0, 23.0],
            "metric_pre": [4.0, 6.0, 5.0, 9.0, 6.0, 8.0, 7.0, 12.0],
        }
    )

    expected_p_value, expected_standard_error = _manual_cuped_statistics_from_frame(
        cuped_frame=cuped_frame,
        group_column="group_name",
        baseline_group="control",
        test_group="test",
    )
    p_value, standard_error, reason = _compute_cuped_statistics_from_frame(
        cuped_frame=cuped_frame,
        group_column="group_name",
        baseline_group="control",
        test_group="test",
    )

    assert reason is None
    assert standard_error == pytest.approx(expected_standard_error)
    assert p_value == pytest.approx(expected_p_value)


def test_compute_test_metrics_cuped_uses_transformed_values() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 7)),
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [1, 2, 3, 4, 5, 100],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 7)),
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [10, 1, 8, 3, 6, 200],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
        outliers_quantile=0.8,
        outliers_policy="truncate",
    )

    cuped_frame = pd.DataFrame(
        {
            "group_name": df["group_name"],
            "metric_exp": [1.0, 2.0, 3.0, 4.0, 5.0, 5.0],
            "metric_pre": [10.0, 1.0, 8.0, 3.0, 6.0, 10.0],
        }
    )
    expected_p_value, expected_standard_error, reason = _compute_cuped_statistics_from_frame(
        cuped_frame=cuped_frame,
        group_column="group_name",
        baseline_group="control",
        test_group="test",
    )
    assert reason is None

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    assert orders_row["outliers_cutoff"] == pytest.approx(5.0)
    assert orders_row["outliers_n_test"] == 1
    assert orders_row["s.e. CUPED"] == pytest.approx(expected_standard_error)
    assert orders_row["p-value CUPED"] == pytest.approx(expected_p_value)


def test_compute_test_metrics_adds_cuped_p_value_for_ratio_metrics() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [5, 6, 4, 5, 8, 9, 7, 8],
            "impressions": [10, 12, 8, 10, 12, 14, 10, 12],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [4, 5, 3, 4, 6, 7, 5, 6],
            "impressions": [9, 11, 8, 10, 11, 15, 9, 13],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=[
            {
                "name": "ctr_user",
                "numerator": "clicks",
                "denominator": "impressions",
                "level": "user",
            }
        ],
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    ratio_row = result[result["metric_name"] == "ctr_user"].iloc[0]
    assert ratio_row["metric_type"] == "ratio"
    assert not math.isnan(float(ratio_row["s.e. CUPED"]))
    assert not math.isnan(float(ratio_row["p-value CUPED"]))
    assert not math.isnan(float(ratio_row["mde_abs CUPED"]))
    assert not math.isnan(float(ratio_row["mde_relative CUPED"]))


def test_compute_test_metrics_cuped_warns_when_pre_variance_is_not_positive() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 12, 11, 13, 15, 17, 16, 18],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [5] * 8,
        }
    )

    with pytest.warns(UserWarning, match="pre-experiment covariate variance is not positive"):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = _single_metric_row(result, "orders")
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))
    assert math.isnan(float(orders_row["mde_abs CUPED"]))
    assert math.isnan(float(orders_row["mde_relative CUPED"]))


def test_compute_test_metrics_cuped_warns_when_no_overlapping_observations() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 12, 11, 13, 15, 17, 16, 18],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(101, 109)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [8, 9, 10, 11, 12, 13, 14, 15],
        }
    )

    with pytest.warns(UserWarning, match="no overlapping non-missing experiment/pre-experiment observations"):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = _single_metric_row(result, "orders")
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))


def test_compute_test_metrics_cuped_warns_when_too_few_usable_observations() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [10.0, np.nan, 14.0, 15.0],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [8.0, 9.0, 12.0, 13.0],
        }
    )

    with pytest.warns(UserWarning, match="not enough overlapping observations to run the CUPED t-test"):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = _single_metric_row(result, "orders")
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))


def test_compute_test_metrics_cuped_uses_only_overlapping_nonmissing_users() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4, 5, 6],
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [10.0, 11.0, 16.0, 14.0, 16.0, 16.0],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": [999, 6, 4, 5, 2, 1, 3, 1000],
            "group_name": ["control", "test", "test", "test", "control", "control", "control", "test"],
            "orders": [16.0, 16.0, 14.0, np.nan, 9.0, 8.0, 16.0, 16.0],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    expected_frame = pd.DataFrame(
        {
            "group_name": ["control", "control", "control", "test", "test"],
            "metric_exp": [10.0, 11.0, 16.0, 14.0, 16.0],
            "metric_pre": [8.0, 9.0, 16.0, 14.0, 16.0],
        }
    )
    _assert_cuped_row_matches_frame(
        _single_metric_row(result, "orders"),
        expected_frame,
        baseline_group="control",
        test_group="test",
    )


def test_compute_test_metrics_cuped_user_ratio_matches_manual_frame() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [4, 5, 6, 9, 8, 14, 10, 18],
            "impressions": [10, 0, 12, 10, 16, 20, -1, 20],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [3, 3, 4, 7, 4, 10, 14, 9],
            "impressions": [10, 6, 0, 10, 10, 20, 20, 0],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=[
            {
                "name": "ctr_user",
                "numerator": "clicks",
                "denominator": "impressions",
                "level": "user",
            }
        ],
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    expected_frame = pd.DataFrame(
        {
            "group_name": ["control", "control", "test", "test"],
            "metric_exp": [0.4, 0.9, 0.5, 0.7],
            "metric_pre": [0.3, 0.7, 0.4, 0.5],
        }
    )
    _assert_cuped_row_matches_frame(
        _single_metric_row(result, "ctr_user"),
        expected_frame,
        baseline_group="control",
        test_group="test",
    )


def test_compute_test_metrics_cuped_agg_ratio_matches_manual_linearization() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [10, 11, 12, 13, 16, 16, 16, 16],
            "impressions": [100, 110, 120, 130, 100, 100, 100, 100],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [9, 10, 11, 12, 13, 13, 13, 13],
            "impressions": [90, 100, 110, 120, 100, 100, 100, 100],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=[
            {
                "name": "ctr_agg",
                "numerator": "clicks",
                "denominator": "impressions",
                "level": "agg",
            }
        ],
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    expected_frame = pd.DataFrame(
        {
            "group_name": df["group_name"],
            "metric_exp": _manual_agg_ratio_linearized_values(
                df["clicks"].astype(float),
                df["impressions"].astype(float),
            ),
            "metric_pre": _manual_agg_ratio_linearized_values(
                pre_df["clicks"].astype(float),
                pre_df["impressions"].astype(float),
            ),
        }
    )
    _assert_cuped_row_matches_frame(
        _single_metric_row(result, "ctr_agg"),
        expected_frame,
        baseline_group="control",
        test_group="test",
    )


def test_compute_test_metrics_cuped_matches_each_multi_group_comparison() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": ["control"] * 3 + ["test_a"] * 3 + ["test_b"] * 3,
            "orders": [10.0, 11.0, 16.0, 13.0, 15.0, 16.0, 9.0, 12.0, 16.0],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": ["control"] * 3 + ["test_a"] * 3 + ["test_b"] * 3,
            "orders": [5.0, 7.0, 16.0, 8.0, 9.0, 16.0, 4.0, 6.0, 16.0],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=True,
        pre_exp_metrics_df=pre_df,
    )

    assert result.shape[0] == 3
    for test_group, baseline_group in [
        ("test_a", "control"),
        ("test_b", "control"),
        ("test_a", "test_b"),
    ]:
        comparison_mask = df["group_name"].isin([baseline_group, test_group])
        expected_frame = pd.DataFrame(
            {
                "group_name": df.loc[comparison_mask, "group_name"].to_numpy(),
                "metric_exp": df.loc[comparison_mask, "orders"].to_numpy(dtype=float),
                "metric_pre": pre_df.loc[comparison_mask, "orders"].to_numpy(dtype=float),
            }
        )
        _assert_cuped_row_matches_frame(
            _single_metric_row(result, "orders", group_1=test_group, group_2=baseline_group),
            expected_frame,
            baseline_group=baseline_group,
            test_group=test_group,
        )


def test_compute_test_metrics_warns_and_sets_nan_when_pre_metric_is_missing() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 11, 9, 12, 14, 15, 13, 16],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "gmv": [100, 110, 90, 120, 140, 150, 130, 160],
        }
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED p-value for metric 'orders'"):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))


def test_compute_test_metrics_validates_pre_experiment_group_assignments() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 5)),
            "group_name": ["control", "control", "test", "test"],
            "orders": [10, 11, 14, 15],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 5)),
            "group_name": ["control", "test", "test", "test"],
            "orders": [8, 9, 12, 13],
        }
    )

    with pytest.raises(ValueError, match="must match between df and pre_exp_metrics_df"):
        compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )
