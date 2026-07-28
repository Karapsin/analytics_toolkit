from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from analytics_toolkit.general import time_print

from .bootstrap import _apply_multiple_comparisons_adjustment
from .constants import DEFAULT_ALPHA, DEFAULT_POWER
from .cuped import _compute_cuped_statistics, _prepare_cuped_context
from .outliers import _build_outlier_context
from .ratio import _normalize_ratio_metrics
from .rows import (
    _build_comparisons,
    _build_metric_definitions,
    _build_metric_row,
    _prepare_metric_context,
)
from .stats import _compute_mde_from_standard_error, _safe_relative
from .validation import (
    _validate_input_columns,
    _validate_mde_parameters,
    _validate_multiple_comparisons_parameters,
    _validate_outlier_parameters,
    _validate_pre_experiment_dataframe,
)


def compute_test_metrics(
    df: pd.DataFrame | Mapping[str, Mapping[str, Any]],
    group: str = "group_name",
    control: str = "control",
    user_id: str = "user_id",
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    ratio_metrics: list[dict[str, object]] | None = None,
    test_vs_test: bool = True,
    multiple_comparisons_adjustment: bool = False,
    multiple_comparisons_adjustment_resamples: int = 2000,
    bootstrap_random_state: int | None = 0,
    bootstrap_n_jobs: int = 1,
    bootstrap_progress: bool = False,
    pre_exp_metrics_df: pd.DataFrame | None = None,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "non_zero_truncate",
    concurrency: int = 1,
    fail_fast: bool = True,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = 5,
    progress: bool = False,
) -> pd.DataFrame | dict[str, pd.DataFrame | str]:
    """Compute experiment metric statistics for one dataframe or named tasks."""

    metric_kwargs = {
        "group": group,
        "control": control,
        "user_id": user_id,
        "mde_alpha": mde_alpha,
        "mde_power": mde_power,
        "ratio_metrics": ratio_metrics,
        "test_vs_test": test_vs_test,
        "multiple_comparisons_adjustment": multiple_comparisons_adjustment,
        "multiple_comparisons_adjustment_resamples": multiple_comparisons_adjustment_resamples,
        "bootstrap_random_state": bootstrap_random_state,
        "bootstrap_n_jobs": bootstrap_n_jobs,
        "bootstrap_progress": bootstrap_progress,
        "outliers_quantile": outliers_quantile,
        "outliers_policy": outliers_policy,
    }
    if pre_exp_metrics_df is not None:
        metric_kwargs["pre_exp_metrics_df"] = pre_exp_metrics_df

    if isinstance(df, pd.DataFrame):
        if concurrency != 1:
            raise ValueError("concurrency can be greater than 1 only when df is a task mapping.")
        return _compute_test_metrics_dataframe(df=df, **metric_kwargs)

    if isinstance(df, Mapping):
        from .parallel import _compute_metric_tasks

        metric_defaults = _changed_metric_defaults(
            group=group,
            control=control,
            user_id=user_id,
            mde_alpha=mde_alpha,
            mde_power=mde_power,
            ratio_metrics=ratio_metrics,
            test_vs_test=test_vs_test,
            multiple_comparisons_adjustment=multiple_comparisons_adjustment,
            multiple_comparisons_adjustment_resamples=(multiple_comparisons_adjustment_resamples),
            bootstrap_random_state=bootstrap_random_state,
            bootstrap_n_jobs=bootstrap_n_jobs,
            bootstrap_progress=bootstrap_progress,
            pre_exp_metrics_df=pre_exp_metrics_df,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        return _compute_metric_tasks(
            df,
            metric_defaults=metric_defaults,
            concurrency=concurrency,
            fail_fast=fail_fast,
            soft_concurrency_cap=soft_concurrency_cap,
            hard_concurrency_cap=hard_concurrency_cap,
            progress=progress,
        )

    raise TypeError("df must be a pandas DataFrame or a non-empty task mapping.")


def _changed_metric_defaults(
    *,
    group: str,
    control: str,
    user_id: str,
    mde_alpha: float,
    mde_power: float,
    ratio_metrics: list[dict[str, object]] | None,
    test_vs_test: bool,
    multiple_comparisons_adjustment: bool,
    multiple_comparisons_adjustment_resamples: int,
    bootstrap_random_state: int | None,
    bootstrap_n_jobs: int,
    bootstrap_progress: bool,
    pre_exp_metrics_df: pd.DataFrame | None,
    outliers_quantile: float,
    outliers_policy: str,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    if group != "group_name":
        defaults["group"] = group
    if control != "control":
        defaults["control"] = control
    if user_id != "user_id":
        defaults["user_id"] = user_id
    if mde_alpha != DEFAULT_ALPHA:
        defaults["mde_alpha"] = mde_alpha
    if mde_power != DEFAULT_POWER:
        defaults["mde_power"] = mde_power
    if ratio_metrics is not None:
        defaults["ratio_metrics"] = ratio_metrics
    if test_vs_test is not True:
        defaults["test_vs_test"] = test_vs_test
    if multiple_comparisons_adjustment is not False:
        defaults["multiple_comparisons_adjustment"] = multiple_comparisons_adjustment
    if multiple_comparisons_adjustment_resamples != 2000:
        defaults["multiple_comparisons_adjustment_resamples"] = (
            multiple_comparisons_adjustment_resamples
        )
    if bootstrap_random_state != 0:
        defaults["bootstrap_random_state"] = bootstrap_random_state
    if bootstrap_n_jobs != 1:
        defaults["bootstrap_n_jobs"] = bootstrap_n_jobs
    if bootstrap_progress is not False:
        defaults["bootstrap_progress"] = bootstrap_progress
    if pre_exp_metrics_df is not None:
        defaults["pre_exp_metrics_df"] = pre_exp_metrics_df
    if outliers_quantile != 0.999:
        defaults["outliers_quantile"] = outliers_quantile
    if outliers_policy != "non_zero_truncate":
        defaults["outliers_policy"] = outliers_policy
    return defaults


def _compute_test_metrics_dataframe(
    df: pd.DataFrame,
    group: str = "group_name",
    control: str = "control",
    user_id: str = "user_id",
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    ratio_metrics: list[dict[str, object]] | None = None,
    test_vs_test: bool = True,
    multiple_comparisons_adjustment: bool = False,
    multiple_comparisons_adjustment_resamples: int = 2000,
    bootstrap_random_state: int | None = 0,
    bootstrap_n_jobs: int = 1,
    bootstrap_progress: bool = False,
    pre_exp_metrics_df: pd.DataFrame | None = None,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "non_zero_truncate",
) -> pd.DataFrame:
    """Compute per-metric experiment comparison statistics.

    Notes:
    - The input must contain exactly one row per user.
    - All columns except `group` and `user_id` are treated as metric columns.
    - Missing metric values are ignored independently for each metric/group pair.
    - `mde_abs` and `mde_relative` use a two-sided normal approximation based
      on the observed sample variances.
    - Ratio metrics can be passed through `ratio_metrics`.
    """

    initial_metric_column_count = len(
        [column for column in df.columns if column not in {group, user_id}]
    )
    time_print(
        "compute_test_metrics: start "
        f"rows={len(df)} metric_columns={initial_metric_column_count} "
        f"ratio_metrics={bool(ratio_metrics)} "
        f"cuped={pre_exp_metrics_df is not None} "
        f"bootstrap={bool(multiple_comparisons_adjustment)}"
    )

    _validate_input_columns(df, group=group, user_id=user_id)
    _validate_mde_parameters(mde_alpha=mde_alpha, mde_power=mde_power)
    _validate_multiple_comparisons_parameters(
        multiple_comparisons_adjustment=multiple_comparisons_adjustment,
        multiple_comparisons_adjustment_resamples=multiple_comparisons_adjustment_resamples,
        bootstrap_random_state=bootstrap_random_state,
        bootstrap_n_jobs=bootstrap_n_jobs,
        bootstrap_progress=bootstrap_progress,
    )
    _validate_outlier_parameters(
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )
    normalized_outliers_quantile = float(outliers_quantile)
    normalized_outliers_policy = outliers_policy.strip().lower()

    if df[user_id].isna().any():
        raise ValueError(f"Column '{user_id}' must not contain missing values.")
    if df[user_id].duplicated().any():
        raise ValueError(f"Column '{user_id}' must contain unique user ids.")
    if df[group].isna().any():
        raise ValueError(f"Column '{group}' must not contain missing values.")

    if pre_exp_metrics_df is not None:
        _validate_pre_experiment_dataframe(
            df=df,
            pre_exp_metrics_df=pre_exp_metrics_df,
            group=group,
            control=control,
            user_id=user_id,
        )

    metric_columns = [column for column in df.columns if column not in {group, user_id}]
    if not metric_columns:
        if not ratio_metrics:
            raise ValueError("The dataframe must contain at least one metric column.")

    group_names = df[group].drop_duplicates().tolist()
    if control not in group_names:
        raise ValueError(f"Control label '{control}' was not found in column '{group}'.")

    ratio_specs = _normalize_ratio_metrics(df, ratio_metrics, reserved_columns={group, user_id})
    comparisons = _build_comparisons(group_names, control, test_vs_test=test_vs_test)
    metric_definitions = _build_metric_definitions(metric_columns, ratio_specs)
    group_values = df[group].to_numpy()
    group_masks = {group_name: group_values == group_name for group_name in group_names}
    comparison_frames = {
        (test_group, baseline_group): df.loc[
            group_masks[test_group] | group_masks[baseline_group],
            [user_id, group],
        ].copy()
        for test_group, baseline_group in comparisons
    }
    time_print(
        "compute_test_metrics: setup complete "
        f"groups={len(group_names)} comparisons={len(comparisons)} "
        f"metrics={len(metric_definitions)}"
    )

    time_print("compute_test_metrics: building outlier contexts")
    for metric_definition in metric_definitions:
        metric_definition["_outlier_context"] = _build_outlier_context(
            df=df,
            metric_definition=metric_definition,
            outliers_quantile=normalized_outliers_quantile,
            outliers_policy=normalized_outliers_policy,
        )
        if pre_exp_metrics_df is not None:
            metric_definition["_pre_outlier_context"] = _build_outlier_context(
                df=pre_exp_metrics_df,
                metric_definition=metric_definition,
                outliers_quantile=normalized_outliers_quantile,
                outliers_policy=normalized_outliers_policy,
                allow_missing=True,
            )
    time_print("compute_test_metrics: outlier contexts complete")

    prepared_metric_contexts: dict[str, dict[str, object]] = {}
    prepared_cuped_contexts: dict[str, dict[str, object]] = {}
    for metric_definition in metric_definitions:
        metric_key = str(metric_definition["metric_key"])
        outlier_context = metric_definition.get("_outlier_context")
        prepared_metric_contexts[metric_key] = _prepare_metric_context(
            df=df,
            metric_definition=metric_definition,
            outlier_context=outlier_context,
        )
        if pre_exp_metrics_df is not None:
            prepared_cuped_contexts[metric_key] = _prepare_cuped_context(
                df=df,
                pre_exp_metrics_df=pre_exp_metrics_df,
                user_id_column=user_id,
                metric_definition=metric_definition,
                outlier_context=outlier_context,
                pre_outlier_context=metric_definition.get("_pre_outlier_context"),
            )

    rows: list[dict[str, object]] = []
    for test_group, baseline_group in comparisons:
        time_print(f"compute_test_metrics: comparison {test_group} vs {baseline_group}")
        for metric_definition in metric_definitions:
            metric_name = str(metric_definition["metric_key"])
            metric_type = str(metric_definition["kind"])
            time_print(f"compute_test_metrics: metric {metric_name} ({metric_type})")
            outlier_context = metric_definition.get("_outlier_context")
            row = _build_metric_row(
                df=df,
                group_column=group,
                baseline_group=baseline_group,
                test_group=test_group,
                metric_definition=metric_definition,
                mde_alpha=mde_alpha,
                mde_power=mde_power,
                outlier_context=outlier_context,
                prepared_metric_context=prepared_metric_contexts.get(metric_name),
                group_masks=group_masks,
            )
            row["_comparison_key"] = (test_group, baseline_group)
            if pre_exp_metrics_df is not None:
                time_print(f"compute_test_metrics: CUPED {metric_name} ({metric_type})")
                row["p-value CUPED"], row["s.e. CUPED"] = _compute_cuped_statistics(
                    df=df,
                    pre_exp_metrics_df=pre_exp_metrics_df,
                    group_column=group,
                    user_id_column=user_id,
                    baseline_group=baseline_group,
                    test_group=test_group,
                    metric_definition=metric_definition,
                    outlier_context=outlier_context,
                    pre_outlier_context=metric_definition.get("_pre_outlier_context"),
                    prepared_cuped_context={
                        **prepared_cuped_contexts[metric_name],
                        "comparison_frame": comparison_frames[(test_group, baseline_group)],
                    },
                )
                row["mde_abs CUPED"] = _compute_mde_from_standard_error(
                    standard_error=row["s.e. CUPED"],
                    alpha=mde_alpha,
                    power=mde_power,
                )
                row["mde_relative CUPED"] = _safe_relative(
                    row["mde_abs CUPED"],
                    row["metric_control"],
                )
            row = {
                "metric_type": str(metric_definition["kind"]),
                "group_1": test_group,
                "group_2": baseline_group,
                **row,
            }
            rows.append(row)

    if multiple_comparisons_adjustment:
        time_print(
            "compute_test_metrics: bootstrap adjustment start "
            f"resamples={multiple_comparisons_adjustment_resamples} "
            f"n_jobs={bootstrap_n_jobs}"
        )
        _apply_multiple_comparisons_adjustment(
            rows=rows,
            df=df,
            group_column=group,
            metric_definitions=metric_definitions,
            comparisons=comparisons,
            resamples=multiple_comparisons_adjustment_resamples,
            random_state=bootstrap_random_state,
            n_jobs=bootstrap_n_jobs,
            show_progress=bootstrap_progress,
        )
        time_print("compute_test_metrics: bootstrap adjustment complete")

    columns = [
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
        "delta_relative",
        "mde_abs",
        "mde_relative",
        "s.e.",
        "p-value",
    ]
    if pre_exp_metrics_df is not None:
        columns.append("s.e. CUPED")
        columns.append("p-value CUPED")
        columns.append("mde_abs CUPED")
        columns.append("mde_relative CUPED")
    if multiple_comparisons_adjustment:
        columns.append("s.e. bootstrap")
        columns.append("bootstrap_adj_p")
    columns = ["metric_type", "group_1", "group_2", *columns]

    for row in rows:
        row.pop("_comparison_key", None)
        row.pop("_metric_key", None)
        row.pop("_test_stat", None)

    result = pd.DataFrame(rows, columns=columns)
    time_print(f"compute_test_metrics: finish rows={len(result)}")
    return result
