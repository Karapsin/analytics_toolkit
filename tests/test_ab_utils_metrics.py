from __future__ import annotations

import inspect
import math
import threading
import time
import warnings
from types import SimpleNamespace
from typing import Any, Sequence

import analytics_toolkit.ab_utils as ab_utils
import analytics_toolkit.ab_utils.bootstrap as bootstrap_module
import analytics_toolkit.ab_utils.metrics as ab_metrics
import analytics_toolkit.ab_utils.planning as planning_module
import numpy as np
import pandas as pd
import pytest
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


def test_compute_test_metrics_bootstrap_progress_defaults_to_false() -> None:
    signature = inspect.signature(compute_test_metrics)

    assert signature.parameters["bootstrap_progress"].default is False


def test_compute_mde_start_dt_is_required() -> None:
    assert inspect.signature(compute_mde).parameters["start_dt"].default is inspect._empty
    assert inspect.signature(compute_mde_from_sql).parameters["start_dt"].default is inspect._empty
    assert (
        inspect.signature(compute_mde_sql_native).parameters["start_dt"].default is inspect._empty
    )


def test_compute_mde_from_sql_concurrency_defaults_to_one() -> None:
    assert inspect.signature(compute_mde_from_sql).parameters["concurrency"].default == 1
    assert inspect.signature(compute_mde_sql_native).parameters["concurrency"].default == 1


@pytest.mark.parametrize("value", [True, [], pd.NaT])
def test_mde_start_date_normalization_rejects_invalid_values(value: object) -> None:
    error = TypeError if value is True else ValueError
    with pytest.raises(error, match="start_dt must be a datelike value"):
        planning_module._normalize_start_dt(value)


def test_mde_metric_column_normalization_rejects_duplicates_and_missing() -> None:
    frame = pd.DataFrame({"user": [1], "dt": ["2026-01-01"], "metric": [1.0]})
    kwargs = {
        "df": frame,
        "ratio_specs": [],
        "user_id": "user",
        "date_column": "dt",
    }
    with pytest.raises(ValueError, match="must not contain duplicates"):
        planning_module._normalize_metric_columns(metric_columns=["metric", "metric"], **kwargs)
    with pytest.raises(ValueError, match="Missing metric"):
        planning_module._normalize_metric_columns(metric_columns=["missing"], **kwargs)


def test_mde_positive_grid_rejects_conflicting_empty_and_incomplete_inputs() -> None:
    names = {
        "values_name": "values",
        "min_name": "minimum",
        "max_name": "maximum",
        "step_name": "step",
    }
    with pytest.raises(ValueError, match="cannot be combined"):
        planning_module._resolve_positive_int_grid(
            values=[1], min_value=1, max_value=None, step=None, **names
        )
    with pytest.raises(ValueError, match="must not be empty"):
        planning_module._resolve_positive_int_grid(
            values=[], min_value=None, max_value=None, step=None, **names
        )
    with pytest.raises(ValueError, match="Either values"):
        planning_module._resolve_positive_int_grid(
            values=None, min_value=1, max_value=None, step=1, **names
        )
    with pytest.raises(ValueError, match="less than or equal"):
        planning_module._resolve_positive_int_grid(
            values=None, min_value=3, max_value=1, step=1, **names
        )


@pytest.mark.parametrize(
    ("value", "error"),
    [(True, TypeError), (1.5, TypeError), (0, ValueError), (-1, ValueError)],
)
def test_mde_positive_integer_validation(value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="positive integers"):
        planning_module._validate_positive_int(value, "value")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (True, TypeError),
        ("0.5", TypeError),
        (math.inf, ValueError),
        (0, ValueError),
        (1, ValueError),
    ],
)
def test_mde_control_share_validation(value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="control_share"):
        planning_module._validate_control_share(value)


def test_mde_split_rejects_empty_arm() -> None:
    with pytest.raises(ValueError, match="one control and one test"):
        planning_module._build_planned_split(group_size=1, control_share=0.5)


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (pd.DataFrame(), "at least one user-day"),
        (pd.DataFrame({"dt": ["2026-01-01"]}), "Column 'user' was not found"),
        (pd.DataFrame({"user": [1]}), "Column 'dt' was not found"),
        (
            pd.DataFrame({"user": [None], "dt": ["2026-01-01"]}),
            "user.*missing values",
        ),
        (pd.DataFrame({"user": [1], "dt": [None]}), "dt.*missing values"),
        (pd.DataFrame({"user": [1], "dt": ["not-a-date"]}), "datelike values"),
        (
            pd.DataFrame({"user": [1, 1], "dt": ["2026-01-01", "2026-01-01"]}),
            "unique user-day rows",
        ),
    ],
)
def test_prepare_mde_user_day_frame_rejects_invalid_rows(frame: pd.DataFrame, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        planning_module._prepare_mde_user_day_frame(df=frame, user_id="user", date_column="dt")


def test_mde_sql_where_and_required_column_validation() -> None:
    with pytest.raises(TypeError, match="string or None"):
        planning_module._normalize_sql_where(1)
    with pytest.raises(ValueError, match="must not be empty"):
        planning_module._normalize_sql_where("  ")
    with pytest.raises(ValueError, match="Column 'user'"):
        planning_module._validate_sql_source_required_columns(
            column_names=["dt"], user_id="user", date_column="dt"
        )
    with pytest.raises(ValueError, match="Column 'dt'"):
        planning_module._validate_sql_source_required_columns(
            column_names=["user"], user_id="user", date_column="dt"
        )


def test_mde_sql_result_and_date_coercion_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError, match="did not return a dataframe"):
        planning_module._normalize_sql_mde_window_frame(
            result=[], user_id="user", columns=["metric"]
        )
    monkeypatch.setattr(planning_module.sql_facade, "read", lambda *args, **kwargs: [])
    with pytest.raises(TypeError, match="did not return a dataframe"):
        planning_module._read_sql_mde_query(
            db_key="db",
            query="SELECT 1",
            print_queries=False,
            retry_cnt=0,
            timeout_increment=0,
            query_label=None,
        )
    with pytest.raises(ValueError, match="minimum date is missing"):
        planning_module._coerce_sql_date(None, "minimum date")
    with pytest.raises(ValueError, match="must be datelike"):
        planning_module._coerce_sql_date(object(), "minimum date")


def test_mde_aggregation_name_and_query_policy_failures() -> None:
    assert (
        planning_module._validate_mde_aggregation_metric_names(
            values=None, name="metrics", metric_columns={"a"}
        )
        == set()
    )
    with pytest.raises(ValueError, match="duplicates"):
        planning_module._validate_mde_aggregation_metric_names(
            values=["a", "a"], name="metrics", metric_columns={"a"}
        )
    with pytest.raises(ValueError, match="unknown metric"):
        planning_module._validate_mde_aggregation_metric_names(
            values=["b"], name="metrics", metric_columns={"a"}
        )
    with pytest.raises(AssertionError, match="Unexpected MDE aggregation policy"):
        planning_module._build_sql_mde_user_window_query(
            backend="gp",
            source='"public"."events"',
            sql_where=None,
            user_id="user",
            date_column="dt",
            columns=["metric"],
            aggregation_policies={"metric": "median"},
            start_date=pd.Timestamp("2026-01-01"),
            days=7,
        )


def test_ab_metric_outlier_policy_defaults_to_non_zero_truncate() -> None:
    assert (
        inspect.signature(compute_test_metrics).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde).parameters["outliers_policy"].default == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde_from_sql).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde_sql_native).parameters["outliers_policy"].default
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
    assert "compute_test_metrics: bootstrap adjustment start resamples=3 n_jobs=1" in output
    assert "compute_test_metrics: bootstrap adjustment complete" in output
    assert f"compute_test_metrics: finish rows={len(result)}" in output


@pytest.mark.parametrize(
    "outliers_policy",
    ["truncate", "drop", "non_zero_truncate"],
)
def test_compute_test_metrics_matches_centered_bootstrap_oracle(
    outliers_policy: str,
) -> None:
    df = pd.DataFrame(
        {
            "user_id": range(36),
            "group_name": ["control"] * 12 + ["test_a"] * 12 + ["test_b"] * 12,
            "orders": [
                0,
                0,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                30,
                3,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                35,
                1,
                3,
                5,
                7,
                9,
                11,
                13,
                15,
                17,
                19,
                21,
                40,
            ],
        }
    )

    result = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=80,
        bootstrap_random_state=11,
        bootstrap_n_jobs=1,
        outliers_quantile=0.8,
        outliers_policy=outliers_policy,
    )
    expected = _manual_centered_bootstrap_adjustment(
        df,
        group="group_name",
        control="control",
        metric_kind="mean",
        metric_columns=("orders",),
        test_vs_test=True,
        resamples=80,
        random_state=11,
        outliers_quantile=0.8,
        outliers_policy=outliers_policy,
    )

    for row in result.to_dict("records"):
        expected_p, expected_se = expected[(row["group_1"], row["group_2"])]
        assert row["bootstrap_adj_p"] == pytest.approx(expected_p)
        assert row["s.e. bootstrap"] == pytest.approx(expected_se)


@pytest.mark.parametrize("ratio_level", ["user", "agg"])
def test_compute_test_metrics_ratio_bootstrap_matches_independent_oracle(
    ratio_level: str,
) -> None:
    denominator = np.tile(np.array([5.0, 8.0, 10.0, 12.0, 15.0, 20.0]), 8)
    numerator = np.concatenate(
        [
            denominator[:24] * np.linspace(0.1, 0.9, 24),
            denominator[24:] * np.linspace(0.2, 1.1, 24),
        ]
    )
    numerator[-1] = denominator[-1] * 8
    df = pd.DataFrame(
        {
            "user_id": range(48),
            "group_name": ["control"] * 24 + ["test"] * 24,
            "clicks": numerator,
            "impressions": denominator,
        }
    )
    ratio_metrics = [
        {
            "name": "ctr",
            "numerator": "clicks",
            "denominator": "impressions",
            "level": ratio_level,
        }
    ]

    result = compute_test_metrics(
        df,
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=60,
        bootstrap_random_state=23,
        outliers_quantile=0.85,
        outliers_policy="truncate",
    )
    expected = _manual_centered_bootstrap_adjustment(
        df,
        group="group_name",
        control="control",
        metric_kind=ratio_level,
        metric_columns=("clicks", "impressions"),
        test_vs_test=False,
        resamples=60,
        random_state=23,
        outliers_quantile=0.85,
        outliers_policy="truncate",
    )[("test", "control")]

    ratio_row = result[result["metric_name"] == "ctr"].iloc[0]
    assert ratio_row["bootstrap_adj_p"] == pytest.approx(expected[0])
    assert ratio_row["s.e. bootstrap"] == pytest.approx(expected[1])


def test_compute_test_metrics_centered_bootstrap_extreme_effect_reaches_finite_floor() -> None:
    control = np.linspace(-1.0, 1.0, 50)
    test = np.linspace(99.0, 101.0, 50)
    df = pd.DataFrame(
        {
            "user_id": range(100),
            "group_name": ["control"] * 50 + ["test"] * 50,
            "value": np.concatenate([control, test]),
        }
    )

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=199,
        bootstrap_random_state=7,
        outliers_quantile=1,
    )

    row = result.iloc[0]
    assert row["p-value"] < 1e-50
    assert row["bootstrap_adj_p"] == pytest.approx(1 / 200)


def test_compute_test_metrics_centered_bootstrap_is_one_for_exact_null() -> None:
    values = np.linspace(-2.0, 2.0, 40)
    df = pd.DataFrame(
        {
            "user_id": range(80),
            "group_name": ["control"] * 40 + ["test"] * 40,
            "value": np.concatenate([values, values]),
        }
    )

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=99,
        bootstrap_random_state=5,
        outliers_quantile=1,
    )

    assert result.iloc[0]["bootstrap_adj_p"] == 1


def test_compute_test_metrics_rejects_infinite_observed_standard_error() -> None:
    df = pd.DataFrame(
        {
            "user_id": range(4),
            "group_name": ["control", "control", "test", "test"],
            "value": [1e308, -1e308, 1e308, -1e308],
        }
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = compute_test_metrics(
            df,
            test_vs_test=False,
            multiple_comparisons_adjustment=True,
            multiple_comparisons_adjustment_resamples=9,
            bootstrap_random_state=1,
            outliers_quantile=1,
        )

    row = result.iloc[0]
    assert math.isinf(float(row["s.e."]))
    assert math.isnan(float(row["bootstrap_adj_p"]))


def test_compute_test_metrics_warns_when_no_family_resample_is_valid() -> None:
    df = pd.DataFrame(
        {
            "user_id": range(4),
            "group_name": ["control", "control", "test", "test"],
            "value": [0.0, 1.0, 0.2, 1.2],
        }
    )

    with pytest.warns(
        RuntimeWarning,
        match="Bootstrap discarded 1 of 1 resamples for metric 'value'",
    ):
        result = compute_test_metrics(
            df,
            test_vs_test=False,
            multiple_comparisons_adjustment=True,
            multiple_comparisons_adjustment_resamples=1,
            bootstrap_random_state=0,
            outliers_quantile=1,
        )

    assert math.isnan(float(result.iloc[0]["bootstrap_adj_p"]))
    assert math.isnan(float(result.iloc[0]["s.e. bootstrap"]))


def test_compute_test_metrics_aggregate_ratio_bootstrap_is_scale_invariant() -> None:
    df = pd.DataFrame(
        {
            "user_id": range(60),
            "group_name": ["control"] * 30 + ["test"] * 30,
            "clicks": np.linspace(1.0, 30.0, 60),
            "impressions": np.linspace(10.0, 90.0, 60),
        }
    )
    ratio_metrics = [
        {
            "name": "ctr",
            "numerator": "clicks",
            "denominator": "impressions",
            "level": "agg",
        }
    ]
    kwargs = {
        "ratio_metrics": ratio_metrics,
        "test_vs_test": False,
        "multiple_comparisons_adjustment": True,
        "multiple_comparisons_adjustment_resamples": 75,
        "bootstrap_random_state": 29,
        "outliers_quantile": 1,
    }

    original = compute_test_metrics(df, **kwargs)
    scaled_df = df.copy()
    scaled_df[["clicks", "impressions"]] *= 100
    scaled = compute_test_metrics(scaled_df, **kwargs)
    original_ratio = original[original["metric_name"] == "ctr"].iloc[0]
    scaled_ratio = scaled[scaled["metric_name"] == "ctr"].iloc[0]

    assert scaled_ratio["bootstrap_adj_p"] == pytest.approx(original_ratio["bootstrap_adj_p"])
    assert scaled_ratio["s.e. bootstrap"] == pytest.approx(original_ratio["s.e. bootstrap"])


def test_compute_test_metrics_aligns_group_position_columns() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(df, test_vs_test=False)

    assert result.columns.tolist()[:14] == [
        "metric_type",
        "group_1",
        "group_2",
        "metric_name",
        "n_group_1",
        "n_group_2",
        "outliers_cutoff",
        "outliers_n_group_1",
        "outliers_n_group_2",
        "metric_group_1",
        "metric_group_2",
        "variance_group_1",
        "variance_group_2",
        "delta_abs",
    ]
    assert result.columns[result.columns.get_loc("mde_relative") + 1] == "s.e."
    assert result.columns[result.columns.get_loc("s.e.") + 1] == "p-value"
    assert {
        "n0",
        "n1",
        "outliers_n_control",
        "outliers_n_test",
        "metric_control",
        "metric_test",
        "variance_control",
        "variance_test",
    }.isdisjoint(result.columns)

    orders_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "orders")
    ].iloc[0]
    orders_cutoff = float(df["orders"].quantile(0.999))
    assert orders_row["metric_type"] == "mean"
    assert orders_row["metric_group_2"] == pytest.approx((10 + 12 + 9) / 3)
    assert orders_row["metric_group_1"] == pytest.approx((13 + orders_cutoff + 11 + 14) / 4)
    assert orders_row["outliers_cutoff"] == pytest.approx(orders_cutoff)
    assert orders_row["outliers_n_group_2"] == 0
    assert orders_row["outliers_n_group_1"] == 1
    control_values = pd.Series([10, 12, 9], dtype=float)
    test_values = pd.Series([13, orders_cutoff, 11, 14], dtype=float)
    expected_control_variance = control_values.var(ddof=1)
    expected_test_variance = test_values.var(ddof=1)
    assert orders_row["variance_group_2"] == pytest.approx(expected_control_variance)
    assert orders_row["variance_group_1"] == pytest.approx(expected_test_variance)
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
    expected_cuped_se = math.sqrt((expected_cuped_variance / 6) + (expected_cuped_variance / 4))
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 2),
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
            if "CAST(\"dt\" AS DATE) >= DATE '2024-01-03'" in query:
                frames[str(task["name"])] = pd.DataFrame(
                    {"user_id": [1, 2, 3], "orders": [7.0, 14.0, 23.0]}
                )
                continue
            if "CAST(\"dt\" AS DATE) >= DATE '2024-01-01'" in query:
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


def test_compute_mde_variants_match_for_same_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [user_id for user_id in range(1, 6) for _ in range(4)],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 5),
            "orders": [
                1.0,
                2.0,
                4.0,
                6.0,
                2.0,
                3.0,
                5.0,
                7.0,
                4.0,
                5.0,
                7.0,
                10.0,
                3.0,
                6.0,
                8.0,
                12.0,
                5.0,
                8.0,
                13.0,
                17.0,
            ],
            "clicks": [
                2.0,
                3.0,
                4.0,
                5.0,
                1.0,
                4.0,
                5.0,
                7.0,
                3.0,
                5.0,
                6.0,
                9.0,
                4.0,
                6.0,
                8.0,
                10.0,
                5.0,
                7.0,
                9.0,
                12.0,
            ],
            "views": [
                10.0,
                12.0,
                14.0,
                16.0,
                8.0,
                10.0,
                15.0,
                18.0,
                11.0,
                13.0,
                17.0,
                20.0,
                12.0,
                15.0,
                19.0,
                23.0,
                13.0,
                16.0,
                21.0,
                26.0,
            ],
            "converted": [
                0.0,
                1.0,
                0.0,
                1.0,
                0.0,
                0.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                0.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
            ],
        }
    )
    ratio_metrics = [
        RatioMetricSpec(
            name="ctr_user",
            numerator="clicks",
            denominator="views",
            level="user",
        ),
        RatioMetricSpec(
            name="ctr_agg",
            numerator="clicks",
            denominator="views",
            level="agg",
        ),
    ]
    common_kwargs = {
        "user_id": "user_id",
        "metric_columns": ["orders", "converted"],
        "ratio_metrics": ratio_metrics,
        "group_sizes": [10, 14],
        "exp_days": [1, 2],
        "start_dt": "2024-01-03",
        "control_share": 0.6,
        "outliers_quantile": 1,
        "max_agg_metrics": ["converted"],
    }
    expected = compute_mde(source_df, **common_kwargs)
    table_info = SimpleNamespace(
        exists=True,
        columns={
            "user_id": "int",
            "dt": "date",
            "orders": "double precision",
            "clicks": "double precision",
            "views": "double precision",
            "converted": "double precision",
        },
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )

    def aggregate_window(start: str, days: int) -> pd.DataFrame:
        start_date = pd.Timestamp(start)
        mask = (source_df["dt"] >= start_date) & (
            source_df["dt"] < start_date + pd.Timedelta(days=days)
        )
        return (
            source_df.loc[mask]
            .groupby("user_id", as_index=False)
            .agg(
                {
                    "orders": "sum",
                    "clicks": "sum",
                    "views": "sum",
                    "converted": "max",
                }
            )
        )

    def fake_table_info(db_key: str, table: str) -> SimpleNamespace:
        assert db_key == "analytics"
        assert table == "sandbox.events"
        return table_info

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        assert db_key == "analytics"
        assert kwargs["query_label"] in {"mde-parity", "mde-native-parity"}
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
            task_name = str(task["name"])
            assert task["db_key"] == "analytics"
            assert task["query_label"] == "mde-parity"
            if task_name == "mde_outcome_1":
                frames[task_name] = aggregate_window("2024-01-03", 1)
            elif task_name == "mde_outcome_2":
                frames[task_name] = aggregate_window("2024-01-03", 2)
            elif task_name == "mde_pre_1":
                frames[task_name] = aggregate_window("2024-01-02", 1)
            elif task_name == "mde_pre_2":
                frames[task_name] = aggregate_window("2024-01-01", 2)
            else:
                raise AssertionError(f"Unexpected SQL task name: {task_name}")
        return frames

    def fake_load_sql_native_mde_stats(
        *,
        metric_definitions: Sequence[dict[str, object]],
        aggregation_policies: dict[str, str],
        days_values: Sequence[int],
        windows: dict[int, dict[str, Any]],
        outliers_quantile: float,
        outliers_policy: str,
        **kwargs: object,
    ) -> dict[tuple[int, int], dict[str, object]]:
        del kwargs
        stats_by_metric_day: dict[tuple[int, int], dict[str, object]] = {}
        for metric_index, metric_definition in enumerate(metric_definitions):
            for days in days_values:
                window = windows[int(days)]
                window_df = planning_module._filter_mde_window(
                    df=source_df,
                    date_column="dt",
                    start_date=window["outcome_start"],
                    days=int(days),
                )
                user_metric_df = planning_module._aggregate_mde_window_to_users(
                    df=window_df,
                    metric_definition=metric_definition,
                    user_id="user_id",
                    aggregation_policies=aggregation_policies,
                )
                outlier_context = planning_module._build_outlier_context(
                    df=user_metric_df,
                    metric_definition=metric_definition,
                    outliers_quantile=outliers_quantile,
                    outliers_policy=outliers_policy,
                )
                metric_stats = planning_module._compute_mde_metric_stats(
                    df=user_metric_df,
                    metric_definition=metric_definition,
                    outlier_context=outlier_context,
                )
                cuped_variance, cuped_reason = planning_module._compute_mde_cuped_variance(
                    df=source_df,
                    date_column="dt",
                    user_id="user_id",
                    metric_definition=metric_definition,
                    outcome_user_metric_df=user_metric_df,
                    outcome_outlier_context=outlier_context,
                    pre_start_date=window["pre_start"],
                    pre_days=window["pre_days"],
                    unavailable_reason=window["cuped_unavailable_reason"],
                    outliers_quantile=outliers_quantile,
                    outliers_policy=outliers_policy,
                    aggregation_policies=aggregation_policies,
                )
                assert cuped_reason is None
                stats_by_metric_day[(metric_index, int(days))] = {
                    "avg": metric_stats["avg"],
                    "var": metric_stats["var"],
                    "cuped_pair_n": 5,
                    "cuped_pre_var": 1.0,
                    "cuped_adjusted_var": cuped_variance,
                }
        return stats_by_metric_day

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

    sql_result = compute_mde_from_sql(
        "analytics",
        "sandbox.events",
        **common_kwargs,
        query_label="mde-parity",
    )
    monkeypatch.setattr(
        planning_module,
        "_load_sql_native_mde_stats",
        fake_load_sql_native_mde_stats,
    )
    native_result = compute_mde_sql_native(
        "analytics",
        "sandbox.events",
        **common_kwargs,
        query_label="mde-native-parity",
    )

    pd.testing.assert_frame_equal(sql_result, expected)
    pd.testing.assert_frame_equal(native_result, expected)


def test_compute_mde_sql_native_uses_compact_sql_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
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
    expected_row = expected.iloc[0]
    pre_values = pd.Series([3.0, 7.0, 11.0])
    outcome_values = pd.Series([7.0, 14.0, 23.0])
    cuped_pre_var = float(pre_values.var(ddof=1))
    cuped_adjusted_var = _manual_cuped_adjusted_variance(outcome_values, pre_values)
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
        assert db_key == "analytics"
        assert kwargs["query_label"] == "mde-native"
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
        raise AssertionError(f"Unexpected direct query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        assert kwargs["concurrency"] == 1
        frames: dict[str, pd.DataFrame] = {}
        for task in tasks:
            assert task["db_key"] == "analytics"
            assert task["query_label"] == "mde-native"
            query = task["query"]
            assert isinstance(query, str)
            queries.append(query)
            assert "GROUP BY" in query
            assert "ORDER BY" not in query
            assert "MAX(value)" in query
            assert "VAR_SAMP" in query
            assert "COVAR_SAMP" in query
            frames[str(task["name"])] = pd.DataFrame(
                {
                    "avg": [expected_row["avg"]],
                    "var": [expected_row["var"]],
                    "cuped_pair_n": [3],
                    "cuped_pre_var": [cuped_pre_var],
                    "cuped_adjusted_var": [cuped_adjusted_var],
                }
            )
        return frames

    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    result = compute_mde_sql_native(
        "analytics",
        "sandbox.events",
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
        query_label="mde-native",
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any('FROM "sandbox"."events"' in query for query in queries)


def test_compute_mde_sql_native_generates_backend_specific_stats_sql() -> None:
    metric_definition = {"kind": "mean", "metric_key": "orders", "column": "orders"}

    gp_query = planning_module._build_sql_native_mde_stats_query(
        backend="gp",
        source='"sandbox"."events"',
        sql_where="country = 'US'",
        user_id="user_id",
        date_column="dt",
        metric_definition=metric_definition,
        aggregation_policies={"orders": "sum"},
        outcome_start=pd.Timestamp("2024-01-03"),
        outcome_days=2,
        pre_start=pd.Timestamp("2024-01-01"),
        pre_days=2,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    )
    trino_query = planning_module._build_sql_native_mde_stats_query(
        backend="trino",
        source='"sandbox"."events"',
        sql_where=None,
        user_id="user_id",
        date_column="dt",
        metric_definition=metric_definition,
        aggregation_policies={"orders": "sum"},
        outcome_start=pd.Timestamp("2024-01-03"),
        outcome_days=2,
        pre_start=pd.Timestamp("2024-01-01"),
        pre_days=2,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    )
    ch_query = planning_module._build_sql_native_mde_stats_query(
        backend="ch",
        source="`sandbox`.`events`",
        sql_where=None,
        user_id="user_id",
        date_column="dt",
        metric_definition=metric_definition,
        aggregation_policies={"orders": "sum"},
        outcome_start=pd.Timestamp("2024-01-03"),
        outcome_days=2,
        pre_start=pd.Timestamp("2024-01-01"),
        pre_days=2,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    )

    assert "PERCENTILE_CONT" in gp_query
    assert "VAR_SAMP" in gp_query
    assert "COVAR_SAMP" in gp_query
    assert "(country = 'US')" in gp_query
    assert "approx_percentile" in trino_query
    assert "quantileExact" in ch_query
    assert "varSamp" in ch_query
    assert "covarSamp" in ch_query


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
            frames[str(task["name"])] = pd.DataFrame({"user_id": [1, 2], "orders": [10.0, 12.0]})
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
                if "CAST(\"dt\" AS DATE) < DATE '2024-01-02'" in query:
                    frame = pd.DataFrame({"user_id": [1, 2, 3], "orders": [1.0, 3.0, 5.0]})
                elif "CAST(\"dt\" AS DATE) < DATE '2024-01-03'" in query:
                    frame = pd.DataFrame({"user_id": [1, 2, 3], "orders": [3.0, 7.0, 11.0]})
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
        "hard_concurrency_cap": 5,
    }
    assert max_active_loads == 2
    assert events.count("aggregate") == 2
    assert max_active_compute_tasks == 2
    assert sorted(compute_calls) == [(1, 10), (1, 20), (2, 10), (2, 20)]
    cuped_warnings = [
        warning for warning in caught if "Could not compute CUPED MDE" in str(warning.message)
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
    missing_date = pd.DataFrame(
        {"user_id": [1, 2], "dt": ["2024-01-01", None], "orders": [1.0, 2.0]}
    )
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


def test_mde_public_entrypoints_require_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = pd.DataFrame({"user": [1], "dt": ["2026-01-01"]})
    with pytest.raises(ValueError, match="At least one metric"):
        compute_mde(
            frame,
            user_id="user",
            metric_columns=[],
            group_sizes=[2],
            exp_days=[1],
            start_dt=None,
        )

    table_info = SimpleNamespace(
        exists=True,
        columns=["user", "dt"],
        resolved_table="public.events",
        table="events",
        backend="gp",
    )
    monkeypatch.setattr(planning_module.sql_facade, "table_info", lambda *_args: table_info)
    for entrypoint in (compute_mde_from_sql, compute_mde_sql_native):
        with pytest.raises(ValueError, match="At least one metric"):
            entrypoint(
                "db",
                "public.events",
                user_id="user",
                metric_columns=[],
                group_sizes=[2],
                exp_days=[1],
                start_dt=None,
            )


def test_compute_mde_sql_native_rejects_missing_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planning_module.sql_facade,
        "table_info",
        lambda *_args: SimpleNamespace(exists=False),
    )
    with pytest.raises(ValueError, match="does not exist"):
        compute_mde_sql_native(
            "db",
            "public.missing",
            metric_columns=["metric"],
            group_sizes=[2],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_sql_native_warns_once_for_each_metric_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns=["user", "dt", "metric"],
        resolved_table="public.events",
        table="events",
        backend="gp",
    )
    monkeypatch.setattr(planning_module.sql_facade, "table_info", lambda *_args: table_info)
    monkeypatch.setattr(
        planning_module,
        "_resolve_mde_options",
        lambda **_kwargs: {
            "days": [1, 1],
            "pre_exp_days": None,
            "control_share": 0.5,
            "planned_splits": [{"group_size": 10, "control_n": 5, "test_n": 5}],
            "start_dt": None,
        },
    )
    monkeypatch.setattr(
        planning_module,
        "_validate_sql_mde_source_rows",
        lambda **_kwargs: {
            "min_date": pd.Timestamp("2026-01-01"),
            "max_date": pd.Timestamp("2026-01-01"),
        },
    )
    monkeypatch.setattr(
        planning_module,
        "_load_sql_native_mde_stats",
        lambda **_kwargs: {
            (0, 1): {
                "avg": 2.0,
                "var": 1.0,
                "cuped_pair_n": 0,
                "cuped_pre_var": None,
                "cuped_adjusted_var": None,
            }
        },
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE") as caught:
        result = compute_mde_sql_native(
            "db",
            "public.events",
            user_id="user",
            metric_columns=["metric"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )

    assert len(caught) == 1
    assert result.shape[0] == 2
    assert result["mde_abs_cuped"].isna().all()


def test_parallel_mde_preserves_first_exception_and_cancels_all_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_failure = RuntimeError("first failure")

    class FakeFuture:
        def __init__(self) -> None:
            self.cancelled = False

        def result(self) -> object:
            raise first_failure

        def cancel(self) -> None:
            self.cancelled = True

    futures = [FakeFuture(), FakeFuture()]
    exited: list[type[BaseException] | None] = []

    class FakeExecutor:
        def __init__(self, *, max_workers: int) -> None:
            assert max_workers == 2
            self.index = 0

        def __enter__(self) -> Any:
            return self

        def __exit__(self, exc_type: type[BaseException] | None, *_args: object) -> None:
            exited.append(exc_type)

        def submit(self, *_args: object, **_kwargs: object) -> FakeFuture:
            future = futures[self.index]
            self.index += 1
            return future

    def in_completion_order(mapping: dict[FakeFuture, object]) -> Any:
        return iter(mapping)

    monkeypatch.setattr(planning_module, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(planning_module, "as_completed", in_completion_order)

    with pytest.raises(RuntimeError, match="first failure"):
        planning_module._compute_parallel_sql_mde_rows(
            concurrency=2,
            user_id="user",
            metric_definitions=[{"kind": "mean", "metric_key": "metric", "column": "metric"}],
            days_values=[1],
            planned_splits=[
                {"group_size": 10, "control_n": 5, "test_n": 5},
                {"group_size": 20, "control_n": 10, "test_n": 10},
            ],
            control_share=0.5,
            windows={1: {"pre_days": 1}},
            outcome_frames={1: pd.DataFrame()},
            pre_frames={1: None},
            outliers_quantile=0.99,
            outliers_policy="truncate",
            mde_alpha=0.05,
            mde_power=0.8,
        )

    assert all(future.cancelled for future in futures)
    assert exited == [RuntimeError]


@pytest.mark.parametrize(
    ("loaded", "error"),
    [({"mde_native_0_1": []}, TypeError), ({"mde_native_0_1": pd.DataFrame()}, ValueError)],
)
def test_sql_native_mde_loader_rejects_invalid_results(
    monkeypatch: pytest.MonkeyPatch,
    loaded: dict[str, object],
    error: type[Exception],
) -> None:
    monkeypatch.setattr(
        planning_module.sql_facade, "parallel_sql", lambda *_args, **_kwargs: loaded
    )
    kwargs = {
        "concurrency": 1,
        "db_key": "db",
        "backend": "gp",
        "source": '"public"."events"',
        "sql_where": None,
        "user_id": "user",
        "date_column": "dt",
        "metric_definitions": [{"kind": "mean", "metric_key": "metric", "column": "metric"}],
        "aggregation_policies": {"metric": "sum"},
        "days_values": [1],
        "windows": {
            1: {
                "outcome_start": pd.Timestamp("2026-01-01"),
                "pre_start": None,
                "pre_days": 1,
            }
        },
        "outliers_quantile": 0.99,
        "outliers_policy": "truncate",
        "print_queries": False,
        "retry_cnt": 0,
        "timeout_increment": 0,
        "query_label": None,
    }
    with pytest.raises(error):
        planning_module._load_sql_native_mde_stats(**kwargs)


def test_sql_native_mde_loader_returns_first_row(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = {"mde_native_0_1": pd.DataFrame({"avg": [2.0], "var": [3.0]})}
    monkeypatch.setattr(
        planning_module.sql_facade, "parallel_sql", lambda *_args, **_kwargs: loaded
    )
    result = planning_module._load_sql_native_mde_stats(
        concurrency=1,
        db_key="db",
        backend="gp",
        source='"public"."events"',
        sql_where=None,
        user_id="user",
        date_column="dt",
        metric_definitions=[{"kind": "mean", "metric_key": "metric", "column": "metric"}],
        aggregation_policies={"metric": "sum"},
        days_values=[1],
        windows={
            1: {
                "outcome_start": pd.Timestamp("2026-01-01"),
                "pre_start": None,
                "pre_days": 1,
            }
        },
        outliers_quantile=0.99,
        outliers_policy="truncate",
        print_queries=False,
        retry_cnt=0,
        timeout_increment=0,
        query_label=None,
    )
    assert result == {(0, 1): {"avg": 2.0, "var": 3.0}}


@pytest.mark.parametrize(
    ("stats", "unavailable", "reason", "variance"),
    [
        ({}, "missing pre window", "missing pre window", math.nan),
        ({"cuped_pair_n": 1}, None, "not enough overlapping", math.nan),
        ({"cuped_pair_n": 2, "cuped_pre_var": 0}, None, "not positive", math.nan),
        ({"cuped_pair_n": 2, "cuped_pre_var": math.nan}, None, "not positive", math.nan),
        (
            {"cuped_pair_n": 2, "cuped_pre_var": 1, "cuped_adjusted_var": math.nan},
            None,
            "not enough adjusted",
            math.nan,
        ),
        (
            {"cuped_pair_n": 2, "cuped_pre_var": 1, "cuped_adjusted_var": 0.25},
            None,
            None,
            0.25,
        ),
    ],
)
def test_resolve_sql_native_cuped_results(
    stats: dict[str, object], unavailable: str | None, reason: str | None, variance: float
) -> None:
    actual_variance, actual_reason = planning_module._resolve_sql_native_cuped_result(
        stats=stats, unavailable_reason=unavailable
    )
    if math.isnan(variance):
        assert math.isnan(actual_variance)
    else:
        assert actual_variance == variance
    assert reason is None if actual_reason is None else reason in actual_reason


def test_sql_native_query_builders_cover_no_pre_ratio_and_policies() -> None:
    mean_metric = {"kind": "mean", "metric_key": "metric", "column": "metric"}
    no_pre_query = planning_module._build_sql_native_mde_stats_query(
        backend="gp",
        source='"public"."events"',
        sql_where="country = 'US'",
        user_id="user",
        date_column="dt",
        metric_definition=mean_metric,
        aggregation_policies={"metric": "max"},
        outcome_start=pd.Timestamp("2026-01-01"),
        outcome_days=7,
        pre_start=None,
        pre_days=7,
        outliers_quantile=0.99,
        outliers_policy="non_zero_truncate",
    )
    assert "pre_user AS" not in no_pre_query
    assert "CAST(0 AS INTEGER) AS cuped_pair_n" in no_pre_query
    assert 'MAX("metric")' in no_pre_query
    assert "(country = 'US')" in no_pre_query
    assert "value <> 0" in no_pre_query

    ratio_metric = {
        "kind": "ratio",
        "metric_key": "ratio",
        "ratio_spec": {"numerator": "shared", "denominator": "shared", "level": "agg"},
    }
    assert planning_module._sql_native_metric_columns(ratio_metric) == ["shared"]

    user_ratio = {
        "kind": "ratio",
        "metric_key": "ratio",
        "ratio_spec": {"numerator": "num", "denominator": "den", "level": "user"},
    }
    user_ctes = planning_module._build_sql_native_metric_value_ctes(
        prefix="outcome",
        backend="gp",
        user_id="user",
        metric_definition=user_ratio,
        outliers_quantile=0.99,
        outliers_policy="remove",
    )
    assert "CASE WHEN" in user_ctes[0]
    assert "THEN NULL ELSE value END" in user_ctes[2]

    ratio_metric["ratio_spec"] = {"numerator": "num", "denominator": "den", "level": "agg"}
    agg_ctes = planning_module._build_sql_native_metric_value_ctes(
        prefix="outcome",
        backend="gp",
        user_id="user",
        metric_definition=ratio_metric,
        outliers_quantile=0.99,
        outliers_policy="remove",
    )
    assert "THEN NULL ELSE numerator END" in agg_ctes[2]
    assert "THEN NULL ELSE denominator END" in agg_ctes[2]
    assert "CASE WHEN SUM(denominator) > 0" in agg_ctes[3]

    with pytest.raises(AssertionError, match="Unexpected MDE aggregation policy"):
        planning_module._build_sql_native_user_window_cte(
            cte_name="outcome_user",
            backend="gp",
            source='"public"."events"',
            sql_where=None,
            user_id="user",
            date_column="dt",
            columns=["metric"],
            aggregation_policies={"metric": "median"},
            start_date=pd.Timestamp("2026-01-01"),
            days=1,
        )


def test_mde_planning_row_keeps_nan_variance_outputs_nan() -> None:
    row = planning_module._build_mde_planning_row(
        metric_name="metric",
        avg=2.0,
        variance=math.nan,
        days=1,
        pre_exp_days=1,
        group_size=10,
        control_share=0.5,
        control_n=5,
        test_n=5,
        cuped_variance=math.nan,
        mde_alpha=0.05,
        mde_power=0.8,
    )
    assert math.isnan(float(row["mde_abs"]))
    assert math.isnan(float(row["mde_abs_cuped"]))


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


def test_frame_cuped_reports_missing_pre_start() -> None:
    variance, reason = planning_module._compute_mde_cuped_variance(
        df=pd.DataFrame({"user": [1], "dt": [pd.Timestamp("2026-01-01")], "metric": [1.0]}),
        date_column="dt",
        user_id="user",
        metric_definition={"kind": "mean", "metric_key": "metric", "column": "metric"},
        outcome_user_metric_df=pd.DataFrame({"user": [1], "metric": [1.0]}),
        outcome_outlier_context=None,
        pre_start_date=None,
        pre_days=1,
        unavailable_reason=None,
        outliers_quantile=0.99,
        outliers_policy="truncate",
        aggregation_policies={"metric": "sum"},
    )
    assert math.isnan(variance)
    assert reason == "pre-experiment window is unavailable"


def test_frame_cuped_reports_missing_frame_and_metric_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _frame_cuped_kwargs()
    kwargs["pre_user_metric_df"] = None
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert reason == "pre-experiment window is unavailable"

    kwargs["pre_user_metric_df"] = pd.DataFrame({"user": [1, 2], "metric": [1.0, 3.0]})
    monkeypatch.setattr(
        planning_module,
        "_build_metric_values_by_user",
        lambda **_kwargs: (pd.DataFrame(), "bad experiment"),
    )
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "experiment metric values are unavailable" in str(reason)

    calls = iter(
        [
            (pd.DataFrame({"user": [1, 2], "metric_exp": [2.0, 4.0]}), None),
            (pd.DataFrame(), "bad pre"),
        ]
    )
    monkeypatch.setattr(
        planning_module, "_build_metric_values_by_user", lambda **_kwargs: next(calls)
    )
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "pre-experiment metric values are unavailable" in str(reason)


def test_frame_cuped_overlap_prevariance_adjusted_and_valid_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _frame_cuped_kwargs()
    kwargs["pre_user_metric_df"] = pd.DataFrame({"user": [2, 3], "metric": [1.0, 2.0]})
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "not enough overlapping" in str(reason)

    kwargs = _frame_cuped_kwargs()
    kwargs["pre_user_metric_df"] = pd.DataFrame({"user": [1, 2], "metric": [1.0, 1.0]})
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "not positive" in str(reason)

    kwargs = _frame_cuped_kwargs()
    monkeypatch.setattr(planning_module, "_compute_sample_variance", lambda _values: math.nan)
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "not enough adjusted" in str(reason)
    monkeypatch.undo()

    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(
        **_frame_cuped_kwargs()
    )
    assert math.isfinite(variance)
    assert reason is None


@pytest.mark.parametrize(
    ("frame", "ratio", "expected_nan"),
    [
        (pd.DataFrame({"numerator": [1.0], "denominator": [1.0]}), 1.0, True),
        (
            pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [1.0, 2.0]}),
            math.nan,
            True,
        ),
        (
            pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [0.0, 0.0]}),
            1.0,
            True,
        ),
        (
            pd.DataFrame({"numerator": [1.0, 3.0], "denominator": [1.0, 1.0]}),
            2.0,
            False,
        ),
    ],
)
def test_aggregate_ratio_unit_variance_edges(
    frame: pd.DataFrame, ratio: float, expected_nan: bool
) -> None:
    variance = planning_module._compute_agg_ratio_unit_variance(frame, ratio)
    assert math.isnan(variance) if expected_nan else variance == pytest.approx(2.0)


def test_aggregate_ratio_unit_variance_handles_nan_centered_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [1.0, 1.0]})
    monkeypatch.setattr(pd.Series, "var", lambda *_args, **_kwargs: math.nan)
    assert math.isnan(planning_module._compute_agg_ratio_unit_variance(frame, 1.5))


def test_prepare_mde_frame_rejects_nat_after_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "to_datetime", lambda *_args, **_kwargs: pd.Series([pd.NaT]))
    with pytest.raises(ValueError, match="datelike values"):
        planning_module._prepare_mde_user_day_frame(
            df=pd.DataFrame({"user": [1], "dt": ["2026-01-01"]}),
            user_id="user",
            date_column="dt",
        )


def test_user_aggregation_rejects_unknown_policy_before_metric_exclusion() -> None:
    with pytest.raises(AssertionError, match="Unexpected MDE aggregation policy"):
        planning_module._aggregate_mde_columns_to_users(
            aggregate_frame=pd.DataFrame({"user": [1], "metric": [math.nan]}),
            user_id="user",
            columns=["metric"],
            aggregation_policies={"metric": "median"},
        )


@pytest.mark.parametrize(
    ("source_rows", "message"),
    [
        (pd.DataFrame(), "returned no rows"),
        (
            pd.DataFrame(
                {
                    "row_count": [0],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [None],
                    "max_dt": [None],
                }
            ),
            "at least one user-day",
        ),
        (
            pd.DataFrame(
                {
                    "row_count": [1],
                    "null_user_rows": [1],
                    "null_date_rows": [0],
                    "min_dt": ["2026-01-01"],
                    "max_dt": ["2026-01-01"],
                }
            ),
            "user.*missing values",
        ),
        (
            pd.DataFrame(
                {
                    "row_count": [1],
                    "null_user_rows": [0],
                    "null_date_rows": [1],
                    "min_dt": ["2026-01-01"],
                    "max_dt": ["2026-01-01"],
                }
            ),
            "dt.*missing values",
        ),
    ],
)
def test_sql_mde_source_validation_rejects_empty_null_and_zero_sources(
    monkeypatch: pytest.MonkeyPatch, source_rows: pd.DataFrame, message: str
) -> None:
    monkeypatch.setattr(planning_module, "_read_sql_mde_query", lambda **_kwargs: source_rows)
    with pytest.raises(ValueError, match=message):
        planning_module._validate_sql_mde_source_rows(
            db_key="db",
            backend="gp",
            source='"public"."events"',
            sql_where=None,
            user_id="user",
            date_column="dt",
            print_queries=False,
            retry_cnt=0,
            timeout_increment=0,
            query_label=None,
        )


def test_sql_mde_window_task_deduplicates_matching_window() -> None:
    tasks: list[dict[str, object]] = []
    task_names = {(pd.Timestamp("2026-01-01"), 1): "existing"}
    result = planning_module._add_sql_mde_window_load_task(
        tasks=tasks,
        task_names_by_window=task_names,
        task_name="new",
        db_key="db",
        backend="gp",
        source='"public"."events"',
        sql_where=None,
        user_id="user",
        date_column="dt",
        columns=["metric"],
        aggregation_policies={"metric": "sum"},
        start_date=pd.Timestamp("2026-01-01"),
        days=1,
        print_queries=False,
        retry_cnt=0,
        timeout_increment=0,
        query_label=None,
    )
    assert result == "existing"
    assert tasks == []


def test_read_sql_mde_user_window_delegates_and_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        planning_module,
        "_build_sql_mde_user_window_query",
        lambda **kwargs: captured.setdefault("builder", kwargs) and "SELECT metric",
    )

    def fake_read(**kwargs: object) -> pd.DataFrame:
        captured["reader"] = kwargs
        return pd.DataFrame({"user": [1], "metric": [2.0], "extra": [3.0]})

    monkeypatch.setattr(planning_module, "_read_sql_mde_query", fake_read)
    result = planning_module._read_sql_mde_user_window(
        db_key="db",
        backend="gp",
        source='"public"."events"',
        sql_where="active",
        user_id="user",
        date_column="dt",
        columns=["metric"],
        aggregation_policies={"metric": "sum"},
        start_date=pd.Timestamp("2026-01-01"),
        days=1,
        print_queries=True,
        retry_cnt=2,
        timeout_increment=3,
        query_label="mde",
    )
    assert result.to_dict("list") == {"user": [1], "metric": [2.0]}
    assert captured["reader"] == {
        "db_key": "db",
        "query": "SELECT metric",
        "print_queries": True,
        "retry_cnt": 2,
        "timeout_increment": 3,
        "query_label": "mde",
    }


@pytest.mark.parametrize(("value", "expected"), [(None, 0), (math.nan, 0), ("7", 7), (8, 8)])
def test_coerce_sql_int_handles_nulls_and_delegates_to_int(value: object, expected: int) -> None:
    assert planning_module._coerce_sql_int(value) == expected


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
        test_vs_test=True,
    )

    ratio_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "ctr")
    ].iloc[0]
    assert ratio_row["metric_type"] == "ratio"
    assert ratio_row["metric_group_2"] == pytest.approx((5 + 3 + 4 + 2) / (10 + 8 + 0 + 4))
    assert ratio_row["metric_group_1"] == pytest.approx((7 + 5 + 6 + 8) / (14 + 10 + 12 + 16))

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
    assert ratio_row["variance_group_2"] == pytest.approx(expected_control_variance)
    assert ratio_row["variance_group_1"] == pytest.approx(expected_test_variance)
    assert ratio_row["s.e."] == pytest.approx(
        _compute_agg_ratio_diff_standard_error(
            baseline_frame=baseline_frame,
            baseline_ratio=baseline_stats["ratio"],
            test_frame=test_frame,
            test_ratio=test_stats["ratio"],
        )
    )

    test_vs_test_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "test_b")
        & (result["metric_name"] == "ctr")
    ].iloc[0]
    assert test_vs_test_row["metric_group_1"] == pytest.approx((7 + 5 + 6 + 8) / 52)
    assert test_vs_test_row["metric_group_2"] == pytest.approx((4 + 5 + 3 + 4) / 32)


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
    assert orders_row["outliers_n_group_2"] == 0
    assert orders_row["outliers_n_group_1"] == 1
    assert orders_row["n_group_2"] == 3
    assert orders_row["n_group_1"] == 2
    assert orders_row["metric_group_1"] == pytest.approx((3 + 4) / 2)


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
    assert orders_row["metric_group_1"] == pytest.approx((100 + cutoff) / 2)


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
    assert default_row["outliers_n_group_1"] == 0
    assert default_row["metric_group_1"] == pytest.approx(20.0)
    assert truncate_row["outliers_cutoff"] == pytest.approx(0.0)
    assert truncate_row["outliers_n_group_1"] == 1
    assert truncate_row["metric_group_1"] == pytest.approx(0.0)


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
    assert truncate_row["outliers_n_group_1"] == 1
    assert truncate_row["metric_group_1"] == pytest.approx((0.3 + cutoff) / 2)
    assert truncate_row["n_group_1"] == 2
    assert drop_row["metric_group_1"] == pytest.approx(0.3)
    assert drop_row["n_group_1"] == 1


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
    assert row["outliers_n_group_1"] == 0
    assert row["metric_group_1"] == pytest.approx(2.0)


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
    assert truncate_row["outliers_n_group_1"] == 1
    assert truncate_row["metric_group_1"] == pytest.approx((3 + cutoff * 10) / 20)
    assert truncate_row["n_group_1"] == 2
    assert drop_row["metric_group_1"] == pytest.approx(3 / 10)
    assert drop_row["n_group_1"] == 1


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
    assert row["outliers_n_group_1"] == 0
    assert row["metric_group_1"] == pytest.approx(2.0)


@pytest.mark.filterwarnings("ignore:Precision loss occurred in moment calculation:RuntimeWarning")
@pytest.mark.filterwarnings("ignore:Bootstrap discarded .* resamples:RuntimeWarning")
def test_compute_test_metrics_bootstrap_is_deterministic_across_executors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _build_sample_metrics_df()

    serial = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=1,
    )
    process = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=2,
    )

    def unavailable_process_pool(*args: object, **kwargs: object) -> None:
        raise PermissionError("process pools unavailable")

    monkeypatch.setattr(bootstrap_module, "ProcessPoolExecutor", unavailable_process_pool)
    thread_fallback = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=2,
    )

    pd.testing.assert_frame_equal(serial, process)
    pd.testing.assert_frame_equal(serial, thread_fallback)
    assert serial.columns[serial.columns.get_loc("p-value") + 1] == "s.e. bootstrap"
    assert serial.columns[serial.columns.get_loc("s.e. bootstrap") + 1] == "bootstrap_adj_p"
    orders_row = serial[
        (serial["group_1"] == "test_a")
        & (serial["group_2"] == "control")
        & (serial["metric_name"] == "orders")
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
        (
            {"bootstrap_random_state": True},
            TypeError,
            "bootstrap_random_state must be an integer or None",
        ),
        (
            {"bootstrap_random_state": -1},
            ValueError,
            "bootstrap_random_state must be non-negative or None",
        ),
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
    assert orders_row["outliers_n_group_2"] == 0
    assert orders_row["outliers_n_group_1"] == 0
    assert orders_row["metric_group_1"] == pytest.approx(515.0)


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
    assert result.columns[result.columns.get_loc("mde_abs CUPED") + 1] == "mde_relative CUPED"
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
        expected_mde_abs / orders_row["metric_group_2"]
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
    assert orders_row["outliers_n_group_1"] == 1
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

    with pytest.warns(
        UserWarning, match="no overlapping non-missing experiment/pre-experiment observations"
    ):
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

    with pytest.warns(
        UserWarning, match="not enough overlapping observations to run the CUPED t-test"
    ):
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
            "group_name": [
                "control",
                "test",
                "test",
                "test",
                "control",
                "control",
                "control",
                "test",
            ],
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
