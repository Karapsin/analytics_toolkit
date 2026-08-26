from __future__ import annotations

import inspect
import math
import threading
import time
import warnings
from types import SimpleNamespace
from typing import Any, Sequence

import analytics_toolkit.ab_utils.bootstrap as bootstrap_module
import analytics_toolkit.ab_utils.metrics as ab_metrics
import analytics_toolkit.ab_utils.planning as planning_module
import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import ab_utils
from analytics_toolkit.ab_utils import (
    RatioMetricSpec,
    compute_mde,
    compute_mde_from_sql,
    compute_mde_sql_native,
    compute_test_metrics,
)
from analytics_toolkit.ab_utils.metrics import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
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
    _get_numeric_metric_series,
    _normalize_ratio_metrics,
    _prepare_cuped_context,
    _prepare_metric_context,
)
from scipy.stats import ttest_ind


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
            "gmv": [
                100.0,
                120.0,
                95.0,
                110.0,
                130.0,
                145.0,
                118.0,
                140.0,
                92.0,
                105.0,
                99.0,
                108.0,
            ],
            "clicks": [5, 3, 4, 2, 7, 5, 6, 8, 4, 5, 3, 4],
            "impressions": [10, 8, 0, 4, 14, 10, 12, 16, 8, 10, 6, 8],
        }
    )


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
    assert row["mde_relative CUPED"] == pytest.approx(expected_mde_abs / row["metric_group_2"])


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


def _manual_centered_bootstrap_adjustment(
    df: pd.DataFrame,
    *,
    group: str,
    control: str,
    metric_kind: str,
    metric_columns: tuple[str, ...],
    test_vs_test: bool,
    resamples: int,
    random_state: int,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "truncate",
) -> dict[tuple[str, str], tuple[float, float]]:
    group_names = df[group].drop_duplicates().tolist()
    test_groups = sorted(name for name in group_names if name != control)
    comparisons = [(name, control) for name in test_groups]
    if test_vs_test:
        comparisons.extend(
            (test_groups[left], test_groups[right])
            for left in range(len(test_groups))
            for right in range(left + 1, len(test_groups))
        )

    observed = {
        comparison: _manual_metric_delta_and_se(
            df,
            group=group,
            comparison=comparison,
            metric_kind=metric_kind,
            metric_columns=metric_columns,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        for comparison in comparisons
    }
    observed_valid = [
        comparison
        for comparison, (delta, standard_error) in observed.items()
        if math.isfinite(delta) and math.isfinite(standard_error) and standard_error > 0
    ]
    deltas = {comparison: [] for comparison in comparisons}
    family_max_statistics: list[float] = []
    for seed in np.random.SeedSequence(random_state).spawn(resamples):
        rng = np.random.default_rng(seed)
        sampled = pd.concat(
            [
                group_frame.iloc[rng.integers(0, len(group_frame), size=len(group_frame))]
                for _, group_frame in df.groupby(group, sort=False)
            ],
            ignore_index=True,
        )
        centered_statistics: list[float] = []
        family_valid = bool(observed_valid)
        for comparison in comparisons:
            delta, standard_error = _manual_metric_delta_and_se(
                sampled,
                group=group,
                comparison=comparison,
                metric_kind=metric_kind,
                metric_columns=metric_columns,
                outliers_quantile=outliers_quantile,
                outliers_policy=outliers_policy,
            )
            deltas[comparison].append(delta)
            if comparison not in observed_valid:
                continue
            centered = (delta - observed[comparison][0]) / standard_error
            if math.isfinite(centered):
                centered_statistics.append(abs(centered))
            else:
                family_valid = False
        family_max_statistics.append(
            max(centered_statistics)
            if family_valid and len(centered_statistics) == len(observed_valid)
            else math.nan
        )

    finite_max_statistics = [value for value in family_max_statistics if math.isfinite(value)]
    result: dict[tuple[str, str], tuple[float, float]] = {}
    for comparison in comparisons:
        observed_delta, observed_se = observed[comparison]
        observed_stat = observed_delta / observed_se
        adjusted_p = (
            (1 + sum(value >= abs(observed_stat) for value in finite_max_statistics))
            / (1 + len(finite_max_statistics))
            if finite_max_statistics and math.isfinite(observed_stat)
            else math.nan
        )
        finite_deltas = [value for value in deltas[comparison] if math.isfinite(value)]
        bootstrap_se = float(np.std(finite_deltas, ddof=1)) if len(finite_deltas) >= 2 else math.nan
        result[comparison] = adjusted_p, bootstrap_se
    return result


def _manual_metric_delta_and_se(
    df: pd.DataFrame,
    *,
    group: str,
    comparison: tuple[str, str],
    metric_kind: str,
    metric_columns: tuple[str, ...],
    outliers_quantile: float,
    outliers_policy: str,
) -> tuple[float, float]:
    test_group, baseline_group = comparison
    if metric_kind == "mean":
        values = df[metric_columns[0]].astype(float).copy()
    else:
        numerator = df[metric_columns[0]].astype(float).copy()
        denominator = df[metric_columns[1]].astype(float).copy()
        ratio_mask = numerator.notna() & denominator.notna() & (denominator > 0)
        values = pd.Series(np.nan, index=df.index, dtype=float)
        values.loc[ratio_mask] = numerator.loc[ratio_mask] / denominator.loc[ratio_mask]

    cutoff_values = values.dropna()
    if outliers_policy == "non_zero_truncate":
        cutoff_values = cutoff_values[cutoff_values != 0]
    cutoff = float(cutoff_values.quantile(outliers_quantile))
    outliers = values.notna() & (values > cutoff)
    if metric_kind in {"mean", "user"}:
        if outliers_policy in {"truncate", "non_zero_truncate"}:
            values.loc[outliers] = cutoff
        else:
            values.loc[outliers] = np.nan
        baseline = values[df[group] == baseline_group].dropna().to_numpy()
        test = values[df[group] == test_group].dropna().to_numpy()
        if baseline.size < 2 or test.size < 2:
            return math.nan, math.nan
        delta = float(test.mean() - baseline.mean())
        standard_error = math.sqrt(
            float(baseline.var(ddof=1)) / baseline.size + float(test.var(ddof=1)) / test.size
        )
        return delta, standard_error

    if outliers_policy in {"truncate", "non_zero_truncate"}:
        numerator.loc[outliers] = cutoff * denominator.loc[outliers]
    else:
        numerator.loc[outliers] = np.nan
        denominator.loc[outliers] = np.nan
    valid = numerator.notna() & denominator.notna()
    group_estimates: dict[str, tuple[float, float]] = {}
    for group_name in (baseline_group, test_group):
        mask = valid & (df[group] == group_name)
        group_numerator = numerator.loc[mask].to_numpy()
        group_denominator = denominator.loc[mask].to_numpy()
        if group_numerator.size < 2 or group_denominator.sum() <= 0:
            group_estimates[group_name] = (math.nan, math.nan)
            continue
        ratio = float(group_numerator.sum() / group_denominator.sum())
        centered = group_numerator - ratio * group_denominator
        variance = float(centered.var(ddof=1)) / (
            group_numerator.size * float(group_denominator.mean()) ** 2
        )
        group_estimates[group_name] = ratio, variance
    baseline_ratio, baseline_variance = group_estimates[baseline_group]
    test_ratio, test_variance = group_estimates[test_group]
    return (
        test_ratio - baseline_ratio,
        math.sqrt(baseline_variance + test_variance),
    )


def _frame_cuped_kwargs() -> dict[str, object]:
    return {
        "user_id": "user",
        "metric_definition": {"kind": "mean", "metric_key": "metric", "column": "metric"},
        "outcome_user_metric_df": pd.DataFrame({"user": [1, 2], "metric": [2.0, 4.0]}),
        "pre_user_metric_df": pd.DataFrame({"user": [1, 2], "metric": [1.0, 3.0]}),
        "outcome_outlier_context": None,
        "pre_days": 1,
        "unavailable_reason": None,
        "outliers_quantile": 0.99,
        "outliers_policy": "truncate",
    }


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_POWER",
    "Any",
    "RatioMetricSpec",
    "Sequence",
    "SimpleNamespace",
    "_assert_cuped_row_matches_frame",
    "_build_comparisons",
    "_build_metric_definitions",
    "_build_metric_row",
    "_build_outlier_contexts",
    "_build_ratio_valid_mask",
    "_build_sample_metrics_df",
    "_compute_agg_ratio_diff_standard_error",
    "_compute_agg_ratio_group_stats",
    "_compute_agg_ratio_variance",
    "_compute_cuped_statistics",
    "_compute_cuped_statistics_from_frame",
    "_compute_mde_from_standard_error",
    "_frame_cuped_kwargs",
    "_get_numeric_metric_series",
    "_manual_agg_ratio_linearized_values",
    "_manual_centered_bootstrap_adjustment",
    "_manual_cuped_adjusted_variance",
    "_manual_cuped_statistics_from_frame",
    "_manual_metric_delta_and_se",
    "_normalize_ratio_metrics",
    "_prepare_cuped_context",
    "_prepare_metric_context",
    "_single_metric_row",
    "ab_metrics",
    "ab_utils",
    "bootstrap_module",
    "compute_mde",
    "compute_mde_from_sql",
    "compute_mde_sql_native",
    "compute_test_metrics",
    "inspect",
    "math",
    "np",
    "pd",
    "planning_module",
    "pytest",
    "threading",
    "time",
    "ttest_ind",
    "warnings",
]
