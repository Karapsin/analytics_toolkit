from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from collections.abc import Sequence
from typing import Any

import pandas as pd

from .constants import DEFAULT_ALPHA, DEFAULT_POWER
from .outliers import (
    _apply_outliers_to_agg_ratio_components,
    _apply_outliers_to_values,
    _build_outlier_context,
    _get_outlier_cutoff,
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


@dataclass(frozen=True)
class MdePlanningOptions:
    """Option bundle for historical-variance MDE planning."""

    n0: int
    n1: int
    mde_alpha: float = DEFAULT_ALPHA
    mde_power: float = DEFAULT_POWER
    outliers_quantile: float = 0.999
    outliers_policy: str = "truncate"


def compute_mde_only(
    df: pd.DataFrame,
    *,
    n0: int | None = None,
    n1: int | None = None,
    metric_columns: Sequence[str] | None = None,
    ratio_metrics: Sequence[dict[str, object] | RatioMetricSpec] | None = None,
    user_id: str | None = None,
    options: MdePlanningOptions | None = None,
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "truncate",
) -> pd.DataFrame:
    """Estimate pre-test MDE from historical metric variance and planned sizes."""

    if options is not None:
        if any(value is not None for value in (n0, n1)):
            raise ValueError("Pass either options or n0/n1, not both.")
        n0 = options.n0
        n1 = options.n1
        mde_alpha = options.mde_alpha
        mde_power = options.mde_power
        outliers_quantile = options.outliers_quantile
        outliers_policy = options.outliers_policy

    planned_n0 = _validate_planned_group_size(n0, "n0")
    planned_n1 = _validate_planned_group_size(n1, "n1")
    _validate_mde_parameters(mde_alpha=mde_alpha, mde_power=mde_power)
    _validate_outlier_parameters(
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )

    reserved_columns = {user_id} if user_id is not None else set()
    ratio_specs = _normalize_ratio_metrics(
        df,
        _coerce_ratio_metric_specs(ratio_metrics),
        reserved_columns=reserved_columns,
    )
    mean_metric_columns = _normalize_metric_columns(
        df=df,
        metric_columns=metric_columns,
        ratio_specs=ratio_specs,
        user_id=user_id,
    )
    if not mean_metric_columns and not ratio_specs:
        raise ValueError("At least one metric column or ratio metric is required.")

    metric_definitions = _build_metric_definitions(mean_metric_columns, ratio_specs)
    rows: list[dict[str, object]] = []
    normalized_outliers_policy = outliers_policy.strip().lower()
    for metric_definition in metric_definitions:
        outlier_context = _build_outlier_context(
            df=df,
            metric_definition=metric_definition,
            outliers_quantile=float(outliers_quantile),
            outliers_policy=normalized_outliers_policy,
        )
        rows.append(
            _build_mde_planning_row(
                df=df,
                metric_definition=metric_definition,
                planned_n0=planned_n0,
                planned_n1=planned_n1,
                mde_alpha=mde_alpha,
                mde_power=mde_power,
                outlier_context=outlier_context,
            )
        )

    return pd.DataFrame(
        rows,
        columns=[
            "metric_type",
            "metric_name",
            "historical_n",
            "planned_n0",
            "planned_n1",
            "metric_baseline",
            "variance",
            "s.e.",
            "mde_abs",
            "mde_relative",
            "outliers_cutoff",
            "outliers_n",
        ],
    )


def _build_mde_planning_row(
    *,
    df: pd.DataFrame,
    metric_definition: dict[str, object],
    planned_n0: int,
    planned_n1: int,
    mde_alpha: float,
    mde_power: float,
    outlier_context: dict[str, object] | None,
) -> dict[str, object]:
    if metric_definition["kind"] == "mean":
        values = _get_numeric_metric_series(df, str(metric_definition["column"]))
        values, outlier_mask = _apply_outliers_to_values(values, outlier_context)
        nonmissing_values = values.dropna()
        metric_baseline = _safe_mean(nonmissing_values)
        variance = _compute_sample_variance(nonmissing_values)
    elif dict(metric_definition["ratio_spec"])["level"] == "user":
        ratio_spec = dict(metric_definition["ratio_spec"])
        numerator = _get_numeric_metric_series(df, ratio_spec["numerator"])
        denominator = _get_numeric_metric_series(df, ratio_spec["denominator"])
        valid_mask = _build_ratio_valid_mask(
            numerator=numerator,
            denominator=denominator,
            level=ratio_spec["level"],
        )
        values = pd.Series(math.nan, index=df.index, dtype=float)
        values.loc[valid_mask] = numerator.loc[valid_mask] / denominator.loc[valid_mask]
        values, outlier_mask = _apply_outliers_to_values(values, outlier_context)
        nonmissing_values = values.dropna()
        metric_baseline = _safe_mean(nonmissing_values)
        variance = _compute_sample_variance(nonmissing_values)
    else:
        ratio_spec = dict(metric_definition["ratio_spec"])
        numerator = _get_numeric_metric_series(df, ratio_spec["numerator"])
        denominator = _get_numeric_metric_series(df, ratio_spec["denominator"])
        numerator, denominator, outlier_mask = _apply_outliers_to_agg_ratio_components(
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
        metric_baseline = stats["ratio"]
        variance = _compute_agg_ratio_unit_variance(group_frame, metric_baseline)
        nonmissing_values = group_frame["numerator"]

    standard_error = math.nan
    if not math.isnan(variance):
        standard_error = math.sqrt((variance / planned_n0) + (variance / planned_n1))
    mde_abs = _compute_mde_from_standard_error(
        standard_error=standard_error,
        alpha=mde_alpha,
        power=mde_power,
    )
    return {
        "metric_type": str(metric_definition["kind"]),
        "metric_name": str(metric_definition["metric_key"]),
        "historical_n": int(nonmissing_values.shape[0]),
        "planned_n0": planned_n0,
        "planned_n1": planned_n1,
        "metric_baseline": metric_baseline,
        "variance": variance,
        "s.e.": standard_error,
        "mde_abs": mde_abs,
        "mde_relative": _safe_relative(mde_abs, metric_baseline),
        "outliers_cutoff": _get_outlier_cutoff(outlier_context),
        "outliers_n": int(outlier_mask.sum()),
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
    user_id: str | None,
) -> list[str]:
    if metric_columns is not None:
        columns = [str(column) for column in metric_columns]
    else:
        excluded = {user_id} if user_id is not None else set()
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


def _validate_planned_group_size(value: int | None, name: str) -> int:
    if value is None:
        raise ValueError(f"{name} is required.")
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value
