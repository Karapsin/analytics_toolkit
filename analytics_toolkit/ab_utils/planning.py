from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from numbers import Integral, Real
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from .constants import DEFAULT_ALPHA, DEFAULT_POWER
from .outliers import (
    _apply_outliers_to_agg_ratio_components,
    _apply_outliers_to_values,
    _build_outlier_context,
)
from .ratio import _build_ratio_valid_mask, _compute_agg_ratio_group_stats, _normalize_ratio_metrics
from .rows import _build_metric_definitions
from .stats import (
    _compute_mde_from_standard_error,
    _compute_sample_variance,
    _get_numeric_metric_series,
    _safe_mean,
    _safe_relative,
)
from .validation import _validate_mde_parameters, _validate_outlier_parameters


@dataclass(frozen=True)
class RatioMetricSpec:
    """Public ratio metric option bundle accepted by AB metric helpers."""

    name: str
    numerator: str
    denominator: str
    level: str = "agg"
    invalid_denominator: str = "ignore"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def compute_mde(
    df: pd.DataFrame,
    *,
    user_id: str = "user_id",
    metric_columns: Sequence[str] | None = None,
    ratio_metrics: Sequence[dict[str, object] | RatioMetricSpec] | None = None,
    control_share: float = 0.5,
    group_sizes: Sequence[int] | None = None,
    min_group_size: int | None = None,
    max_group_size: int | None = None,
    group_size_step: int | None = None,
    date_column: str = "dt",
    exp_days: Sequence[int] | None = None,
    min_days: int | None = None,
    max_days: int | None = None,
    days_step: int | None = None,
    exp_length_policy: str = "start",
    random_state: int | None = None,
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "truncate",
) -> pd.DataFrame:
    """Estimate MDE scenarios from historical user-day metric variance."""

    resolved_group_sizes = _resolve_positive_int_grid(
        values=group_sizes,
        min_value=min_group_size,
        max_value=max_group_size,
        step=group_size_step,
        values_name="group_sizes",
        min_name="min_group_size",
        max_name="max_group_size",
        step_name="group_size_step",
    )
    resolved_days = _resolve_positive_int_grid(
        values=exp_days,
        min_value=min_days,
        max_value=max_days,
        step=days_step,
        values_name="exp_days",
        min_name="min_days",
        max_name="max_days",
        step_name="days_step",
    )
    resolved_control_share = _validate_control_share(control_share)
    planned_splits = [
        _build_planned_split(
            group_size=group_size,
            control_share=resolved_control_share,
        )
        for group_size in resolved_group_sizes
    ]
    normalized_policy = _normalize_exp_length_policy(exp_length_policy)
    prepared_df = _prepare_mde_user_day_frame(
        df=df,
        user_id=user_id,
        date_column=date_column,
    )
    _validate_mde_parameters(mde_alpha=mde_alpha, mde_power=mde_power)
    _validate_outlier_parameters(
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )

    reserved_columns = {user_id, date_column}
    ratio_specs = _normalize_ratio_metrics(
        prepared_df,
        _coerce_ratio_metric_specs(ratio_metrics),
        reserved_columns=reserved_columns,
    )
    mean_metric_columns = _normalize_metric_columns(
        df=prepared_df,
        metric_columns=metric_columns,
        ratio_specs=ratio_specs,
        user_id=user_id,
        date_column=date_column,
    )
    _validate_metric_name_conflicts(mean_metric_columns, ratio_specs)
    if not mean_metric_columns and not ratio_specs:
        raise ValueError("At least one metric column or ratio metric is required.")

    metric_definitions = _build_metric_definitions(mean_metric_columns, ratio_specs)
    rows: list[dict[str, object]] = []
    normalized_outliers_policy = outliers_policy.strip().lower()
    windows = _select_mde_windows(
        df=prepared_df,
        days_values=resolved_days,
        date_column=date_column,
        exp_length_policy=normalized_policy,
        random_state=random_state,
    )
    for metric_definition in metric_definitions:
        for days in resolved_days:
            window_df = _filter_mde_window(
                df=prepared_df,
                date_column=date_column,
                start_date=windows[days],
                days=days,
            )
            user_metric_df = _aggregate_mde_window_to_users(
                df=window_df,
                metric_definition=metric_definition,
                user_id=user_id,
            )
            outlier_context = _build_outlier_context(
                df=user_metric_df,
                metric_definition=metric_definition,
                outliers_quantile=float(outliers_quantile),
                outliers_policy=normalized_outliers_policy,
            )
            metric_stats = _compute_mde_metric_stats(
                df=user_metric_df,
                metric_definition=metric_definition,
                outlier_context=outlier_context,
            )
            for split in planned_splits:
                rows.append(
                    _build_mde_planning_row(
                        metric_name=str(metric_definition["metric_key"]),
                        avg=metric_stats["avg"],
                        variance=metric_stats["var"],
                        days=days,
                        group_size=split["group_size"],
                        control_share=resolved_control_share,
                        control_n=split["control_n"],
                        test_n=split["test_n"],
                        mde_alpha=mde_alpha,
                        mde_power=mde_power,
                    )
                )

    return pd.DataFrame(
        rows,
        columns=[
            "metric_name",
            "avg",
            "var",
            "days",
            "group_size",
            "control_share",
            "mde_abs",
            "mde_relative",
        ],
    )


def _build_mde_planning_row(
    *,
    metric_name: str,
    avg: float,
    variance: float,
    days: int,
    group_size: int,
    control_share: float,
    control_n: int,
    test_n: int,
    mde_alpha: float,
    mde_power: float,
) -> dict[str, object]:
    standard_error = math.nan
    if not math.isnan(variance):
        standard_error = math.sqrt((variance / control_n) + (variance / test_n))
    mde_abs = _compute_mde_from_standard_error(
        standard_error=standard_error,
        alpha=mde_alpha,
        power=mde_power,
    )
    return {
        "metric_name": metric_name,
        "avg": avg,
        "var": variance,
        "days": days,
        "group_size": group_size,
        "control_share": control_share,
        "mde_abs": mde_abs,
        "mde_relative": _safe_relative(mde_abs, avg),
    }


def _compute_mde_metric_stats(
    *,
    df: pd.DataFrame,
    metric_definition: dict[str, object],
    outlier_context: dict[str, object] | None,
) -> dict[str, float]:
    if metric_definition["kind"] == "mean":
        values = _get_numeric_metric_series(df, str(metric_definition["column"]))
        values, _ = _apply_outliers_to_values(values, outlier_context)
        nonmissing_values = values.dropna()
        return {
            "avg": _safe_mean(nonmissing_values),
            "var": _compute_sample_variance(nonmissing_values),
        }

    ratio_spec = dict(metric_definition["ratio_spec"])
    numerator = _get_numeric_metric_series(df, ratio_spec["numerator"])
    denominator = _get_numeric_metric_series(df, ratio_spec["denominator"])
    if ratio_spec["level"] == "user":
        valid_mask = _build_ratio_valid_mask(
            numerator=numerator,
            denominator=denominator,
            level=ratio_spec["level"],
        )
        values = pd.Series(math.nan, index=df.index, dtype=float)
        values.loc[valid_mask] = numerator.loc[valid_mask] / denominator.loc[valid_mask]
        values, _ = _apply_outliers_to_values(values, outlier_context)
        nonmissing_values = values.dropna()
        return {
            "avg": _safe_mean(nonmissing_values),
            "var": _compute_sample_variance(nonmissing_values),
        }

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
    group_frame = pd.DataFrame(
        {
            "numerator": numerator[valid_mask],
            "denominator": denominator[valid_mask],
        }
    )
    stats = _compute_agg_ratio_group_stats(group_frame)
    return {
        "avg": stats["ratio"],
        "var": _compute_agg_ratio_unit_variance(group_frame, stats["ratio"]),
    }


def _compute_agg_ratio_unit_variance(
    group_frame: pd.DataFrame,
    ratio: float,
) -> float:
    n = int(group_frame.shape[0])
    if n < 2 or math.isnan(ratio):
        return math.nan

    denominator_mean = float(group_frame["denominator"].mean())
    if denominator_mean <= 0:
        return math.nan

    centered = group_frame["numerator"] - ratio * group_frame["denominator"]
    centered_variance = float(centered.var(ddof=1))
    if math.isnan(centered_variance):
        return math.nan
    return centered_variance / (denominator_mean**2)


def _normalize_metric_columns(
    *,
    df: pd.DataFrame,
    metric_columns: Sequence[str] | None,
    ratio_specs: list[dict[str, str]],
    user_id: str,
    date_column: str,
) -> list[str]:
    if metric_columns is not None:
        columns = [str(column) for column in metric_columns]
    else:
        excluded = {user_id, date_column}
        for ratio_spec in ratio_specs:
            excluded.add(ratio_spec["numerator"])
            excluded.add(ratio_spec["denominator"])
        columns = [column for column in df.columns if column not in excluded]

    if not columns:
        return []
    if len(set(columns)) != len(columns):
        raise ValueError("metric_columns must not contain duplicates.")
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise ValueError(f"Missing metric column(s): {', '.join(missing)}.")
    return columns


def _coerce_ratio_metric_specs(
    ratio_metrics: Sequence[dict[str, object] | RatioMetricSpec] | None,
) -> list[dict[str, object]] | None:
    if ratio_metrics is None:
        return None
    return [
        raw_spec.as_dict() if isinstance(raw_spec, RatioMetricSpec) else dict(raw_spec)
        for raw_spec in ratio_metrics
    ]


def _resolve_positive_int_grid(
    *,
    values: Sequence[int] | None,
    min_value: int | None,
    max_value: int | None,
    step: int | None,
    values_name: str,
    min_name: str,
    max_name: str,
    step_name: str,
) -> list[int]:
    range_names = (min_name, max_name, step_name)
    range_values = (min_value, max_value, step)
    if values is not None:
        provided_range_names = [
            name for name, value in zip(range_names, range_values) if value is not None
        ]
        if provided_range_names:
            raise ValueError(
                f"{values_name} cannot be combined with "
                f"{', '.join(provided_range_names)}."
            )
        normalized_values = [_validate_positive_int(value, values_name) for value in values]
        if not normalized_values:
            raise ValueError(f"{values_name} must not be empty.")
        return sorted(set(normalized_values))

    missing_range_names = [name for name, value in zip(range_names, range_values) if value is None]
    if missing_range_names:
        raise ValueError(
            f"Either {values_name} or all of {min_name}, {max_name}, and "
            f"{step_name} must be provided."
        )

    resolved_min = _validate_positive_int(min_value, min_name)
    resolved_max = _validate_positive_int(max_value, max_name)
    resolved_step = _validate_positive_int(step, step_name)
    if resolved_min > resolved_max:
        raise ValueError(f"{min_name} must be less than or equal to {max_name}.")
    return list(range(resolved_min, resolved_max + 1, resolved_step))


def _validate_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{name} must contain positive integers.")
    normalized = int(value)
    if normalized <= 0:
        raise ValueError(f"{name} must contain positive integers.")
    return normalized


def _validate_control_share(control_share: float) -> float:
    if isinstance(control_share, bool) or not isinstance(control_share, Real):
        raise TypeError("control_share must be a finite number between 0 and 1.")
    normalized = float(control_share)
    if not math.isfinite(normalized) or not 0 < normalized < 1:
        raise ValueError("control_share must be finite and strictly between 0 and 1.")
    return normalized


def _build_planned_split(*, group_size: int, control_share: float) -> dict[str, int]:
    control_n = math.floor(group_size * control_share)
    test_n = group_size - control_n
    if control_n < 1 or test_n < 1:
        raise ValueError(
            "Each group_size and control_share scenario must produce at least "
            "one control and one test user."
        )
    return {"group_size": group_size, "control_n": control_n, "test_n": test_n}


def _normalize_exp_length_policy(exp_length_policy: str) -> str:
    normalized = str(exp_length_policy).strip().lower()
    if normalized not in {"start", "end", "random"}:
        raise ValueError("exp_length_policy must be 'start', 'end', or 'random'.")
    return normalized


def _prepare_mde_user_day_frame(
    *,
    df: pd.DataFrame,
    user_id: str,
    date_column: str,
) -> pd.DataFrame:
    if df.empty:
        raise ValueError("df must contain at least one user-day row.")
    if user_id not in df.columns:
        raise ValueError(f"Column '{user_id}' was not found.")
    if date_column not in df.columns:
        raise ValueError(f"Column '{date_column}' was not found.")
    if df[user_id].isna().any():
        raise ValueError(f"Column '{user_id}' must not contain missing values.")
    if df[date_column].isna().any():
        raise ValueError(f"Column '{date_column}' must not contain missing values.")

    try:
        normalized_dates = pd.to_datetime(df[date_column], errors="raise").dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Column '{date_column}' must contain datelike values.") from exc
    if normalized_dates.isna().any():
        raise ValueError(f"Column '{date_column}' must contain datelike values.")

    prepared = df.copy()
    prepared[date_column] = normalized_dates
    if prepared.duplicated(subset=[user_id, date_column]).any():
        raise ValueError(
            f"Columns '{user_id}' and '{date_column}' must identify unique user-day rows."
        )
    return prepared


def _select_mde_windows(
    *,
    df: pd.DataFrame,
    days_values: Sequence[int],
    date_column: str,
    exp_length_policy: str,
    random_state: int | None,
) -> dict[int, pd.Timestamp]:
    min_date = pd.Timestamp(df[date_column].min())
    max_date = pd.Timestamp(df[date_column].max())
    total_days = int((max_date - min_date).days) + 1
    rng = np.random.default_rng(random_state) if exp_length_policy == "random" else None
    windows: dict[int, pd.Timestamp] = {}
    for days in days_values:
        possible_starts = total_days - days + 1
        if possible_starts < 1:
            raise ValueError(
                f"exp_days value {days} exceeds the available historical calendar span "
                f"of {total_days} day(s)."
            )
        if exp_length_policy == "start":
            offset = 0
        elif exp_length_policy == "end":
            offset = possible_starts - 1
        else:
            offset = int(rng.integers(0, possible_starts)) if rng is not None else 0
        windows[int(days)] = min_date + pd.Timedelta(days=offset)
    return windows


def _filter_mde_window(
    *,
    df: pd.DataFrame,
    date_column: str,
    start_date: pd.Timestamp,
    days: int,
) -> pd.DataFrame:
    end_date = start_date + pd.Timedelta(days=days)
    mask = (df[date_column] >= start_date) & (df[date_column] < end_date)
    return df.loc[mask]


def _aggregate_mde_window_to_users(
    *,
    df: pd.DataFrame,
    metric_definition: dict[str, object],
    user_id: str,
) -> pd.DataFrame:
    if metric_definition["kind"] == "mean":
        metric_column = str(metric_definition["column"])
        numeric_values = _get_numeric_metric_series(df, metric_column)
        aggregate_frame = pd.DataFrame(
            {user_id: df[user_id].to_numpy(), metric_column: numeric_values.to_numpy()},
            index=df.index,
        )
        return (
            aggregate_frame.groupby(user_id, as_index=False)[[metric_column]]
            .sum(min_count=1)
            .reset_index(drop=True)
        )

    ratio_spec = dict(metric_definition["ratio_spec"])
    numerator_column = ratio_spec["numerator"]
    denominator_column = ratio_spec["denominator"]
    numerator = _get_numeric_metric_series(df, numerator_column)
    denominator = _get_numeric_metric_series(df, denominator_column)
    aggregate_frame = pd.DataFrame(
        {
            user_id: df[user_id].to_numpy(),
            numerator_column: numerator.to_numpy(),
            denominator_column: denominator.to_numpy(),
        },
        index=df.index,
    )
    return (
        aggregate_frame.groupby(user_id, as_index=False)[[numerator_column, denominator_column]]
        .sum(min_count=1)
        .reset_index(drop=True)
    )


def _validate_metric_name_conflicts(
    mean_metric_columns: list[str],
    ratio_specs: list[dict[str, str]],
) -> None:
    mean_metric_names = set(mean_metric_columns)
    for ratio_spec in ratio_specs:
        ratio_name = ratio_spec["name"]
        if ratio_name in mean_metric_names:
            raise ValueError(
                f"Ratio metric name '{ratio_name}' conflicts with a mean metric column."
            )
