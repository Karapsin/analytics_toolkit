from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import math
from numbers import Integral, Real
from typing import Any
import warnings

import pandas as pd

from analytics_toolkit import sql as sql_facade
from analytics_toolkit.sql.core.identifiers import (
    parse_table_identifier,
    quote_identifier_part,
)

from .constants import DEFAULT_ALPHA, DEFAULT_POWER
from .cuped import _build_metric_values_by_user
from .outliers import (
    _apply_outliers_to_agg_ratio_components,
    _apply_outliers_to_values,
    _build_outlier_context,
)
from .ratio import (
    _build_ratio_valid_mask,
    _compute_agg_ratio_group_stats,
    _normalize_ratio_metrics,
)
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
    start_dt: object | None,
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "non_zero_truncate",
    pre_exp_days: int | None = None,
    sum_agg_metrics: Sequence[str] | None = None,
    max_agg_metrics: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Estimate MDE scenarios from historical user-day metric variance."""

    options = _resolve_mde_options(
        control_share=control_share,
        group_sizes=group_sizes,
        min_group_size=min_group_size,
        max_group_size=max_group_size,
        group_size_step=group_size_step,
        exp_days=exp_days,
        min_days=min_days,
        max_days=max_days,
        days_step=days_step,
        start_dt=start_dt,
        pre_exp_days=pre_exp_days,
        mde_alpha=mde_alpha,
        mde_power=mde_power,
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )
    prepared_df = _prepare_mde_user_day_frame(
        df=df,
        user_id=user_id,
        date_column=date_column,
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
    aggregation_policies = _resolve_mde_aggregation_policies(
        metric_definitions=metric_definitions,
        sum_agg_metrics=sum_agg_metrics,
        max_agg_metrics=max_agg_metrics,
    )
    rows: list[dict[str, object]] = []
    normalized_outliers_policy = outliers_policy.strip().lower()
    windows = _select_mde_windows(
        min_date=pd.Timestamp(prepared_df[date_column].min()),
        max_date=pd.Timestamp(prepared_df[date_column].max()),
        days_values=options["days"],
        pre_exp_days=options["pre_exp_days"],
        date_column=date_column,
        start_dt=options["start_dt"],
    )
    for metric_definition in metric_definitions:
        for days in options["days"]:
            window = windows[days]
            window_df = _filter_mde_window(
                df=prepared_df,
                date_column=date_column,
                start_date=window["outcome_start"],
                days=days,
            )
            user_metric_df = _aggregate_mde_window_to_users(
                df=window_df,
                metric_definition=metric_definition,
                user_id=user_id,
                aggregation_policies=aggregation_policies,
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
            cuped_variance, cuped_reason = _compute_mde_cuped_variance(
                df=prepared_df,
                date_column=date_column,
                user_id=user_id,
                metric_definition=metric_definition,
                outcome_user_metric_df=user_metric_df,
                outcome_outlier_context=outlier_context,
                pre_start_date=window["pre_start"],
                pre_days=window["pre_days"],
                unavailable_reason=window["cuped_unavailable_reason"],
                outliers_quantile=float(outliers_quantile),
                outliers_policy=normalized_outliers_policy,
                aggregation_policies=aggregation_policies,
            )
            if cuped_reason is not None:
                warnings.warn(
                    (
                        "Could not compute CUPED MDE for metric "
                        f"{str(metric_definition['metric_key'])!r} "
                        f"(days={days}, pre_exp_days={window['pre_days']}): "
                        f"{cuped_reason}."
                    ),
                    stacklevel=2,
                )
            for split in options["planned_splits"]:
                rows.append(
                    _build_mde_planning_row(
                        metric_name=str(metric_definition["metric_key"]),
                        avg=metric_stats["avg"],
                        variance=metric_stats["var"],
                        days=days,
                        pre_exp_days=window["pre_days"],
                        group_size=split["group_size"],
                        control_share=options["control_share"],
                        control_n=split["control_n"],
                        test_n=split["test_n"],
                        cuped_variance=cuped_variance,
                        mde_alpha=mde_alpha,
                        mde_power=mde_power,
                    )
                )

    return _mde_result_frame(rows)


def compute_mde_from_sql(
    db_key: str,
    sql_table_name: str,
    *,
    sql_where: str | None = None,
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
    start_dt: object | None,
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    outliers_quantile: float = 0.999,
    outliers_policy: str = "non_zero_truncate",
    pre_exp_days: int | None = None,
    sum_agg_metrics: Sequence[str] | None = None,
    max_agg_metrics: Sequence[str] | None = None,
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    concurrency: int = 1,
) -> pd.DataFrame:
    """Estimate MDE scenarios from a SQL historical user-day table."""

    _validate_mde_sql_concurrency(concurrency)
    options = _resolve_mde_options(
        control_share=control_share,
        group_sizes=group_sizes,
        min_group_size=min_group_size,
        max_group_size=max_group_size,
        group_size_step=group_size_step,
        exp_days=exp_days,
        min_days=min_days,
        max_days=max_days,
        days_step=days_step,
        start_dt=start_dt,
        pre_exp_days=pre_exp_days,
        mde_alpha=mde_alpha,
        mde_power=mde_power,
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )
    normalized_where = _normalize_sql_where(sql_where)
    table_info = sql_facade.table_info(db_key, sql_table_name)
    if not table_info.exists:
        raise ValueError(f"SQL table {sql_table_name!r} does not exist.")

    column_names = list(table_info.columns)
    metadata_frame = pd.DataFrame(columns=column_names)
    _validate_sql_source_required_columns(
        column_names=column_names,
        user_id=user_id,
        date_column=date_column,
    )
    reserved_columns = {user_id, date_column}
    ratio_specs = _normalize_ratio_metrics(
        metadata_frame,
        _coerce_ratio_metric_specs(ratio_metrics),
        reserved_columns=reserved_columns,
    )
    mean_metric_columns = _normalize_metric_columns(
        df=metadata_frame,
        metric_columns=metric_columns,
        ratio_specs=ratio_specs,
        user_id=user_id,
        date_column=date_column,
    )
    _validate_metric_name_conflicts(mean_metric_columns, ratio_specs)
    if not mean_metric_columns and not ratio_specs:
        raise ValueError("At least one metric column or ratio metric is required.")

    metric_definitions = _build_metric_definitions(mean_metric_columns, ratio_specs)
    aggregation_policies = _resolve_mde_aggregation_policies(
        metric_definitions=metric_definitions,
        sum_agg_metrics=sum_agg_metrics,
        max_agg_metrics=max_agg_metrics,
    )
    source = _build_sql_mde_source(
        table_name=table_info.resolved_table or table_info.table,
        backend=table_info.backend,
    )
    source_stats = _validate_sql_mde_source_rows(
        db_key=db_key,
        backend=table_info.backend,
        source=source,
        sql_where=normalized_where,
        user_id=user_id,
        date_column=date_column,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    windows = _select_mde_windows(
        min_date=source_stats["min_date"],
        max_date=source_stats["max_date"],
        days_values=options["days"],
        pre_exp_days=options["pre_exp_days"],
        date_column=date_column,
        start_dt=options["start_dt"],
    )

    aggregation_columns = _ordered_mde_aggregation_columns(metric_definitions)
    normalized_outliers_policy = outliers_policy.strip().lower()
    if concurrency > 1:
        return _compute_parallel_sql_mde_rows(
            concurrency=concurrency,
            db_key=db_key,
            backend=table_info.backend,
            source=source,
            sql_where=normalized_where,
            user_id=user_id,
            date_column=date_column,
            aggregation_columns=aggregation_columns,
            aggregation_policies=aggregation_policies,
            metric_definitions=metric_definitions,
            days_values=options["days"],
            planned_splits=options["planned_splits"],
            control_share=float(options["control_share"]),
            windows=windows,
            outliers_quantile=float(outliers_quantile),
            outliers_policy=normalized_outliers_policy,
            mde_alpha=mde_alpha,
            mde_power=mde_power,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )

    outcome_frames: dict[int, pd.DataFrame] = {}
    pre_frames: dict[int, pd.DataFrame | None] = {}
    for days in options["days"]:
        window = windows[days]
        outcome_frames[days] = _read_sql_mde_user_window(
            db_key=db_key,
            backend=table_info.backend,
            source=source,
            sql_where=normalized_where,
            user_id=user_id,
            date_column=date_column,
            columns=aggregation_columns,
            aggregation_policies=aggregation_policies,
            start_date=window["outcome_start"],
            days=days,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        pre_frames[days] = None
        if window["pre_start"] is not None:
            pre_frames[days] = _read_sql_mde_user_window(
                db_key=db_key,
                backend=table_info.backend,
                source=source,
                sql_where=normalized_where,
                user_id=user_id,
                date_column=date_column,
                columns=aggregation_columns,
                aggregation_policies=aggregation_policies,
                start_date=window["pre_start"],
                days=window["pre_days"],
                print_queries=print_queries,
                retry_cnt=retry_cnt,
                timeout_increment=timeout_increment,
                query_label=query_label,
            )

    rows: list[dict[str, object]] = []
    for metric_definition in metric_definitions:
        for days in options["days"]:
            window = windows[days]
            user_metric_df = outcome_frames[days]
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
            cuped_variance, cuped_reason = _compute_mde_cuped_variance_from_user_frames(
                user_id=user_id,
                metric_definition=metric_definition,
                outcome_user_metric_df=user_metric_df,
                pre_user_metric_df=pre_frames[days],
                outcome_outlier_context=outlier_context,
                pre_days=window["pre_days"],
                unavailable_reason=window["cuped_unavailable_reason"],
                outliers_quantile=float(outliers_quantile),
                outliers_policy=normalized_outliers_policy,
            )
            if cuped_reason is not None:
                warnings.warn(
                    (
                        "Could not compute CUPED MDE for metric "
                        f"{str(metric_definition['metric_key'])!r} "
                        f"(days={days}, pre_exp_days={window['pre_days']}): "
                        f"{cuped_reason}."
                    ),
                    stacklevel=2,
                )
            for split in options["planned_splits"]:
                rows.append(
                    _build_mde_planning_row(
                        metric_name=str(metric_definition["metric_key"]),
                        avg=metric_stats["avg"],
                        variance=metric_stats["var"],
                        days=days,
                        pre_exp_days=window["pre_days"],
                        group_size=split["group_size"],
                        control_share=options["control_share"],
                        control_n=split["control_n"],
                        test_n=split["test_n"],
                        cuped_variance=cuped_variance,
                        mde_alpha=mde_alpha,
                        mde_power=mde_power,
                    )
                )

    return _mde_result_frame(rows)


def _compute_parallel_sql_mde_rows(
    *,
    concurrency: int,
    db_key: str,
    backend: str,
    source: str,
    sql_where: str | None,
    user_id: str,
    date_column: str,
    aggregation_columns: Sequence[str],
    aggregation_policies: dict[str, str],
    metric_definitions: Sequence[dict[str, object]],
    days_values: Sequence[int],
    planned_splits: Sequence[dict[str, int]],
    control_share: float,
    windows: dict[int, dict[str, Any]],
    outliers_quantile: float,
    outliers_policy: str,
    mde_alpha: float,
    mde_power: float,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    task_specs = [
        (days_index, split_index, int(days), split)
        for days_index, days in enumerate(days_values)
        for split_index, split in enumerate(planned_splits)
    ]
    rows_by_task: dict[tuple[int, int], dict[int, dict[str, object]]] = {}
    cuped_reasons_by_task: dict[tuple[int, int], dict[int, str]] = {}

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_key = {
            executor.submit(
                _compute_sql_mde_day_size_rows,
                db_key=db_key,
                backend=backend,
                source=source,
                sql_where=sql_where,
                user_id=user_id,
                date_column=date_column,
                aggregation_columns=aggregation_columns,
                aggregation_policies=aggregation_policies,
                metric_definitions=metric_definitions,
                days=days,
                split=split,
                control_share=control_share,
                window=windows[days],
                outliers_quantile=outliers_quantile,
                outliers_policy=outliers_policy,
                mde_alpha=mde_alpha,
                mde_power=mde_power,
                print_queries=print_queries,
                retry_cnt=retry_cnt,
                timeout_increment=timeout_increment,
                query_label=query_label,
            ): (days_index, split_index)
            for days_index, split_index, days, split in task_specs
        }
        try:
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                task_rows, cuped_reasons = future.result()
                rows_by_task[key] = task_rows
                cuped_reasons_by_task[key] = cuped_reasons
        except BaseException:
            for future in future_to_key:
                future.cancel()
            raise

    rows: list[dict[str, object]] = []
    emitted_cuped_warnings: set[tuple[int, int, int, str]] = set()
    for metric_index, metric_definition in enumerate(metric_definitions):
        for days_index, days in enumerate(days_values):
            window = windows[int(days)]
            for split_index, _split in enumerate(planned_splits):
                key = (days_index, split_index)
                cuped_reason = cuped_reasons_by_task[key].get(metric_index)
                if cuped_reason is not None:
                    warning_key = (
                        metric_index,
                        int(days),
                        int(window["pre_days"]),
                        cuped_reason,
                    )
                    if warning_key not in emitted_cuped_warnings:
                        emitted_cuped_warnings.add(warning_key)
                        _warn_mde_cuped_reason(
                            metric_definition=metric_definition,
                            days=int(days),
                            pre_days=int(window["pre_days"]),
                            reason=cuped_reason,
                            stacklevel=3,
                        )
                rows.append(rows_by_task[key][metric_index])
    return _mde_result_frame(rows)


def _compute_sql_mde_day_size_rows(
    *,
    db_key: str,
    backend: str,
    source: str,
    sql_where: str | None,
    user_id: str,
    date_column: str,
    aggregation_columns: Sequence[str],
    aggregation_policies: dict[str, str],
    metric_definitions: Sequence[dict[str, object]],
    days: int,
    split: dict[str, int],
    control_share: float,
    window: dict[str, Any],
    outliers_quantile: float,
    outliers_policy: str,
    mde_alpha: float,
    mde_power: float,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> tuple[dict[int, dict[str, object]], dict[int, str]]:
    outcome_frame = _read_sql_mde_user_window(
        db_key=db_key,
        backend=backend,
        source=source,
        sql_where=sql_where,
        user_id=user_id,
        date_column=date_column,
        columns=aggregation_columns,
        aggregation_policies=aggregation_policies,
        start_date=window["outcome_start"],
        days=days,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    pre_frame = None
    if window["pre_start"] is not None:
        pre_frame = _read_sql_mde_user_window(
            db_key=db_key,
            backend=backend,
            source=source,
            sql_where=sql_where,
            user_id=user_id,
            date_column=date_column,
            columns=aggregation_columns,
            aggregation_policies=aggregation_policies,
            start_date=window["pre_start"],
            days=window["pre_days"],
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )

    rows_by_metric: dict[int, dict[str, object]] = {}
    cuped_reasons_by_metric: dict[int, str] = {}
    for metric_index, metric_definition in enumerate(metric_definitions):
        outlier_context = _build_outlier_context(
            df=outcome_frame,
            metric_definition=metric_definition,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        metric_stats = _compute_mde_metric_stats(
            df=outcome_frame,
            metric_definition=metric_definition,
            outlier_context=outlier_context,
        )
        cuped_variance, cuped_reason = _compute_mde_cuped_variance_from_user_frames(
            user_id=user_id,
            metric_definition=metric_definition,
            outcome_user_metric_df=outcome_frame,
            pre_user_metric_df=pre_frame,
            outcome_outlier_context=outlier_context,
            pre_days=window["pre_days"],
            unavailable_reason=window["cuped_unavailable_reason"],
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        if cuped_reason is not None:
            cuped_reasons_by_metric[metric_index] = cuped_reason
        rows_by_metric[metric_index] = _build_mde_planning_row(
            metric_name=str(metric_definition["metric_key"]),
            avg=metric_stats["avg"],
            variance=metric_stats["var"],
            days=days,
            pre_exp_days=window["pre_days"],
            group_size=split["group_size"],
            control_share=control_share,
            control_n=split["control_n"],
            test_n=split["test_n"],
            cuped_variance=cuped_variance,
            mde_alpha=mde_alpha,
            mde_power=mde_power,
        )
    return rows_by_metric, cuped_reasons_by_metric


def _validate_mde_sql_concurrency(concurrency: object) -> None:
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or concurrency < 1
    ):
        raise ValueError("concurrency must be an integer >= 1.")


def _warn_mde_cuped_reason(
    *,
    metric_definition: dict[str, object],
    days: int,
    pre_days: int,
    reason: str,
    stacklevel: int,
) -> None:
    warnings.warn(
        (
            "Could not compute CUPED MDE for metric "
            f"{str(metric_definition['metric_key'])!r} "
            f"(days={days}, pre_exp_days={pre_days}): "
            f"{reason}."
        ),
        stacklevel=stacklevel,
    )


def _mde_result_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=[
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
        ],
    )


def _resolve_mde_options(
    *,
    control_share: float,
    group_sizes: Sequence[int] | None,
    min_group_size: int | None,
    max_group_size: int | None,
    group_size_step: int | None,
    exp_days: Sequence[int] | None,
    min_days: int | None,
    max_days: int | None,
    days_step: int | None,
    start_dt: object | None,
    pre_exp_days: int | None,
    mde_alpha: float,
    mde_power: float,
    outliers_quantile: float,
    outliers_policy: str,
) -> dict[str, object]:
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
    _validate_mde_parameters(mde_alpha=mde_alpha, mde_power=mde_power)
    _validate_outlier_parameters(
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )
    normalized_start_dt = _normalize_optional_start_dt(start_dt)
    planned_splits = [
        _build_planned_split(
            group_size=group_size,
            control_share=resolved_control_share,
        )
        for group_size in resolved_group_sizes
    ]
    return {
        "days": resolved_days,
        "pre_exp_days": _validate_optional_pre_exp_days(pre_exp_days),
        "control_share": resolved_control_share,
        "planned_splits": planned_splits,
        "start_dt": normalized_start_dt,
    }


def _normalize_optional_start_dt(start_dt: object | None) -> pd.Timestamp | None:
    if start_dt is None:
        return None
    return _normalize_start_dt(start_dt)


def _normalize_start_dt(start_dt: object) -> pd.Timestamp:
    if isinstance(start_dt, bool):
        raise TypeError("start_dt must be a datelike value.")
    try:
        normalized = pd.Timestamp(start_dt).normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError("start_dt must be a datelike value.") from exc
    if pd.isna(normalized):
        raise ValueError("start_dt must be a datelike value.")
    return normalized


def _build_mde_planning_row(
    *,
    metric_name: str,
    avg: float,
    variance: float,
    days: int,
    pre_exp_days: int,
    group_size: int,
    control_share: float,
    control_n: int,
    test_n: int,
    cuped_variance: float,
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
    cuped_standard_error = math.nan
    if not math.isnan(cuped_variance):
        cuped_standard_error = math.sqrt(
            (cuped_variance / control_n) + (cuped_variance / test_n)
        )
    mde_abs_cuped = _compute_mde_from_standard_error(
        standard_error=cuped_standard_error,
        alpha=mde_alpha,
        power=mde_power,
    )
    return {
        "metric_name": metric_name,
        "avg": avg,
        "var": variance,
        "days": days,
        "pre_exp_days": pre_exp_days,
        "group_size": group_size,
        "control_share": control_share,
        "mde_abs": mde_abs,
        "mde_relative": _safe_relative(mde_abs, avg),
        "mde_abs_cuped": mde_abs_cuped,
        "mde_relative_cuped": _safe_relative(mde_abs_cuped, avg),
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


def _compute_mde_cuped_variance(
    *,
    df: pd.DataFrame,
    date_column: str,
    user_id: str,
    metric_definition: dict[str, object],
    outcome_user_metric_df: pd.DataFrame,
    outcome_outlier_context: dict[str, object] | None,
    pre_start_date: pd.Timestamp | None,
    pre_days: int,
    unavailable_reason: str | None,
    outliers_quantile: float,
    outliers_policy: str,
    aggregation_policies: dict[str, str],
) -> tuple[float, str | None]:
    if unavailable_reason is not None:
        return math.nan, unavailable_reason
    if pre_start_date is None:
        return math.nan, "pre-experiment window is unavailable"

    pre_window_df = _filter_mde_window(
        df=df,
        date_column=date_column,
        start_date=pre_start_date,
        days=pre_days,
    )
    pre_user_metric_df = _aggregate_mde_window_to_users(
        df=pre_window_df,
        metric_definition=metric_definition,
        user_id=user_id,
        aggregation_policies=aggregation_policies,
    )
    return _compute_mde_cuped_variance_from_user_frames(
        user_id=user_id,
        metric_definition=metric_definition,
        outcome_user_metric_df=outcome_user_metric_df,
        pre_user_metric_df=pre_user_metric_df,
        outcome_outlier_context=outcome_outlier_context,
        pre_days=pre_days,
        unavailable_reason=None,
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )


def _compute_mde_cuped_variance_from_user_frames(
    *,
    user_id: str,
    metric_definition: dict[str, object],
    outcome_user_metric_df: pd.DataFrame,
    pre_user_metric_df: pd.DataFrame | None,
    outcome_outlier_context: dict[str, object] | None,
    pre_days: int,
    unavailable_reason: str | None,
    outliers_quantile: float,
    outliers_policy: str,
) -> tuple[float, str | None]:
    if unavailable_reason is not None:
        return math.nan, unavailable_reason
    if pre_user_metric_df is None:
        return math.nan, "pre-experiment window is unavailable"
    pre_outlier_context = _build_outlier_context(
        df=pre_user_metric_df,
        metric_definition=metric_definition,
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
        allow_missing=True,
    )
    exp_values, exp_error = _build_metric_values_by_user(
        df=outcome_user_metric_df,
        user_id_column=user_id,
        metric_definition=metric_definition,
        value_column="metric_exp",
        outlier_context=outcome_outlier_context,
    )
    if exp_error is not None:
        return math.nan, f"experiment metric values are unavailable: {exp_error}"

    pre_values, pre_error = _build_metric_values_by_user(
        df=pre_user_metric_df,
        user_id_column=user_id,
        metric_definition=metric_definition,
        value_column="metric_pre",
        outlier_context=pre_outlier_context,
    )
    if pre_error is not None:
        return math.nan, f"pre-experiment metric values are unavailable: {pre_error}"

    cuped_frame = exp_values.merge(pre_values, on=user_id, how="inner")
    cuped_frame = cuped_frame.dropna(subset=["metric_exp", "metric_pre"]).reset_index(
        drop=True
    )
    if cuped_frame.shape[0] < 2:
        return (
            math.nan,
            "not enough overlapping non-missing experiment/pre-experiment observations",
        )

    metric_exp = cuped_frame["metric_exp"].astype(float)
    metric_pre = cuped_frame["metric_pre"].astype(float)
    pre_variance = float(metric_pre.var(ddof=1))
    if math.isnan(pre_variance) or pre_variance <= 0:
        return math.nan, "pre-experiment covariate variance is not positive"

    theta = float(metric_exp.cov(metric_pre) / pre_variance)
    adjusted = metric_exp - theta * (metric_pre - float(metric_pre.mean()))
    adjusted_variance = _compute_sample_variance(pd.Series(adjusted))
    if math.isnan(adjusted_variance):
        return math.nan, "not enough adjusted observations to estimate CUPED variance"
    return adjusted_variance, None


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


def _validate_optional_pre_exp_days(pre_exp_days: int | None) -> int | None:
    if pre_exp_days is None:
        return None
    return _validate_positive_int(pre_exp_days, "pre_exp_days")


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
    min_date: pd.Timestamp,
    max_date: pd.Timestamp,
    days_values: Sequence[int],
    pre_exp_days: int | None,
    date_column: str,
    start_dt: pd.Timestamp | None,
) -> dict[int, dict[str, Any]]:
    del date_column
    outcome_start = min_date if start_dt is None else pd.Timestamp(start_dt)
    if outcome_start < min_date:
        raise ValueError(
            f"start_dt {outcome_start.date()} is before the first available "
            f"historical date {min_date.date()}."
        )
    windows: dict[int, dict[str, Any]] = {}
    for days in days_values:
        resolved_pre_days = int(days if pre_exp_days is None else pre_exp_days)
        outcome_end = outcome_start + pd.Timedelta(days=days - 1)
        if outcome_end > max_date:
            available_days = int((max_date - outcome_start).days) + 1
            raise ValueError(
                f"exp_days value {days} exceeds the available historical span "
                f"from start_dt ({available_days} day(s))."
            )
        pre_start = outcome_start - pd.Timedelta(days=resolved_pre_days)
        cuped_unavailable_reason = None
        if pre_start < min_date:
            pre_start = None
            cuped_unavailable_reason = (
                "not enough historical calendar days before start_dt for the "
                "requested pre-experiment window"
            )
        windows[int(days)] = {
            "outcome_start": outcome_start,
            "pre_start": pre_start,
            "pre_days": resolved_pre_days,
            "cuped_unavailable_reason": cuped_unavailable_reason,
        }
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
    aggregation_policies: dict[str, str],
) -> pd.DataFrame:
    if metric_definition["kind"] == "mean":
        metric_column = str(metric_definition["column"])
        numeric_values = _get_numeric_metric_series(df, metric_column)
        aggregate_frame = pd.DataFrame(
            {user_id: df[user_id].to_numpy(), metric_column: numeric_values.to_numpy()},
            index=df.index,
        )
        return _aggregate_mde_columns_to_users(
            aggregate_frame=aggregate_frame,
            user_id=user_id,
            columns=[metric_column],
            aggregation_policies=aggregation_policies,
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
    return _aggregate_mde_columns_to_users(
        aggregate_frame=aggregate_frame,
        user_id=user_id,
        columns=[numerator_column, denominator_column],
        aggregation_policies=aggregation_policies,
    )


def _aggregate_mde_columns_to_users(
    *,
    aggregate_frame: pd.DataFrame,
    user_id: str,
    columns: Sequence[str],
    aggregation_policies: dict[str, str],
) -> pd.DataFrame:
    grouped = aggregate_frame.groupby(user_id, as_index=False)
    aggregated_columns = []
    for column in columns:
        policy = aggregation_policies.get(column, "sum")
        if policy == "sum":
            aggregated = grouped[[column]].sum(min_count=1)
        elif policy == "max":
            aggregated = grouped[[column]].max()
        else:
            raise AssertionError(f"Unexpected MDE aggregation policy: {policy}")
        aggregated_columns.append(aggregated[[column]])

    result = grouped.size()[[user_id]].reset_index(drop=True)
    for aggregated_column in aggregated_columns:
        result[aggregated_column.columns[0]] = aggregated_column.iloc[:, 0].to_numpy()
    return result.reset_index(drop=True)


def _resolve_mde_aggregation_policies(
    *,
    metric_definitions: Sequence[dict[str, object]],
    sum_agg_metrics: Sequence[str] | None,
    max_agg_metrics: Sequence[str] | None,
) -> dict[str, str]:
    if sum_agg_metrics is not None and max_agg_metrics is not None:
        raise ValueError("Only one of sum_agg_metrics or max_agg_metrics can be provided.")

    metric_columns = _collect_mde_aggregation_columns(metric_definitions)
    if sum_agg_metrics is None and max_agg_metrics is None:
        return {column: "sum" for column in metric_columns}

    if max_agg_metrics is not None:
        max_columns = _validate_mde_aggregation_metric_names(
            values=max_agg_metrics,
            name="max_agg_metrics",
            metric_columns=metric_columns,
        )
        return {
            column: "max" if column in max_columns else "sum"
            for column in metric_columns
        }

    sum_columns = _validate_mde_aggregation_metric_names(
        values=sum_agg_metrics,
        name="sum_agg_metrics",
        metric_columns=metric_columns,
    )
    return {
        column: "sum" if column in sum_columns else "max"
        for column in metric_columns
    }


def _collect_mde_aggregation_columns(
    metric_definitions: Sequence[dict[str, object]],
) -> set[str]:
    columns: set[str] = set()
    for metric_definition in metric_definitions:
        if metric_definition["kind"] == "mean":
            columns.add(str(metric_definition["column"]))
            continue
        ratio_spec = dict(metric_definition["ratio_spec"])
        columns.add(ratio_spec["numerator"])
        columns.add(ratio_spec["denominator"])
    return columns


def _ordered_mde_aggregation_columns(
    metric_definitions: Sequence[dict[str, object]],
) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for metric_definition in metric_definitions:
        if metric_definition["kind"] == "mean":
            candidates = [str(metric_definition["column"])]
        else:
            ratio_spec = dict(metric_definition["ratio_spec"])
            candidates = [ratio_spec["numerator"], ratio_spec["denominator"]]
        for column in candidates:
            if column not in seen:
                columns.append(column)
                seen.add(column)
    return columns


def _normalize_sql_where(sql_where: str | None) -> str | None:
    if sql_where is None:
        return None
    if not isinstance(sql_where, str):
        raise TypeError("sql_where must be a string or None.")
    normalized = sql_where.strip()
    if not normalized:
        raise ValueError("sql_where must not be empty when provided.")
    return normalized


def _validate_sql_source_required_columns(
    *,
    column_names: Sequence[str],
    user_id: str,
    date_column: str,
) -> None:
    available = set(column_names)
    if user_id not in available:
        raise ValueError(f"Column '{user_id}' was not found.")
    if date_column not in available:
        raise ValueError(f"Column '{date_column}' was not found.")


def _build_sql_mde_source(*, table_name: str, backend: str) -> str:
    return parse_table_identifier(table_name, backend).render_quoted(backend)


def _validate_sql_mde_source_rows(
    *,
    db_key: str,
    backend: str,
    source: str,
    sql_where: str | None,
    user_id: str,
    date_column: str,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> dict[str, pd.Timestamp]:
    user_expr = _quote_sql_identifier(user_id, backend)
    dt_expr = _sql_date_expr(backend, _quote_sql_identifier(date_column, backend))
    where_clause = _sql_where_clause(sql_where)
    query = f"""
WITH source AS (
    SELECT
        {user_expr} AS __mde_user_id,
        {dt_expr} AS __mde_dt
    FROM {source}
    {where_clause}
)
SELECT
    COUNT(*) AS row_count,
    SUM(CASE WHEN __mde_user_id IS NULL THEN 1 ELSE 0 END) AS null_user_rows,
    SUM(CASE WHEN __mde_dt IS NULL THEN 1 ELSE 0 END) AS null_date_rows,
    MIN(__mde_dt) AS min_dt,
    MAX(__mde_dt) AS max_dt
FROM source
""".strip()
    rows = _read_sql_mde_query(
        db_key=db_key,
        query=query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    if rows.empty:
        raise ValueError("SQL source validation returned no rows.")
    row = rows.iloc[0]
    row_count = _coerce_sql_int(row.get("row_count"))
    if row_count <= 0:
        raise ValueError("SQL source must contain at least one user-day row.")
    null_user_rows = _coerce_sql_int(row.get("null_user_rows"))
    if null_user_rows > 0:
        raise ValueError(f"Column '{user_id}' must not contain missing values.")
    null_date_rows = _coerce_sql_int(row.get("null_date_rows"))
    if null_date_rows > 0:
        raise ValueError(f"Column '{date_column}' must not contain missing values.")

    duplicate_rows = _read_sql_mde_query(
        db_key=db_key,
        query=f"""
WITH source AS (
    SELECT
        {user_expr} AS __mde_user_id,
        {dt_expr} AS __mde_dt
    FROM {source}
    {where_clause}
)
SELECT COALESCE(SUM(__mde_cnt - 1), 0) AS duplicate_user_day_rows
FROM (
    SELECT __mde_user_id, __mde_dt, COUNT(*) AS __mde_cnt
    FROM source
    WHERE __mde_user_id IS NOT NULL AND __mde_dt IS NOT NULL
    GROUP BY __mde_user_id, __mde_dt
    HAVING COUNT(*) > 1
) duplicates
""".strip(),
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    duplicate_count = (
        _coerce_sql_int(duplicate_rows.iloc[0].get("duplicate_user_day_rows"))
        if not duplicate_rows.empty
        else 0
    )
    if duplicate_count > 0:
        raise ValueError(
            f"Columns '{user_id}' and '{date_column}' must identify unique user-day rows."
        )
    return {
        "min_date": _coerce_sql_date(row.get("min_dt"), "min_dt"),
        "max_date": _coerce_sql_date(row.get("max_dt"), "max_dt"),
    }


def _read_sql_mde_user_window(
    *,
    db_key: str,
    backend: str,
    source: str,
    sql_where: str | None,
    user_id: str,
    date_column: str,
    columns: Sequence[str],
    aggregation_policies: dict[str, str],
    start_date: pd.Timestamp,
    days: int,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    user_expr = _quote_sql_identifier(user_id, backend)
    dt_expr = _sql_date_expr(backend, _quote_sql_identifier(date_column, backend))
    end_date = start_date + pd.Timedelta(days=days)
    conditions = [
        f"{dt_expr} >= {_sql_date_literal(start_date, backend)}",
        f"{dt_expr} < {_sql_date_literal(end_date, backend)}",
    ]
    where_clause = _sql_where_clause(sql_where, extra_conditions=conditions)
    select_parts = [f"{user_expr} AS {_quote_sql_identifier(user_id, backend)}"]
    for column in columns:
        quoted_column = _quote_sql_identifier(column, backend)
        policy = aggregation_policies.get(column, "sum")
        if policy == "sum":
            expression = f"SUM({quoted_column})"
        elif policy == "max":
            expression = f"MAX({quoted_column})"
        else:
            raise AssertionError(f"Unexpected MDE aggregation policy: {policy}")
        select_parts.append(f"{expression} AS {_quote_sql_identifier(column, backend)}")
    select_sql = ",\n    ".join(select_parts)
    query = f"""
SELECT
    {select_sql}
FROM {source}
{where_clause}
GROUP BY {user_expr}
ORDER BY {user_expr}
""".strip()
    result = _read_sql_mde_query(
        db_key=db_key,
        query=query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    return result[[user_id, *columns]] if not result.empty else result


def _read_sql_mde_query(
    *,
    db_key: str,
    query: str,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    result = sql_facade.read(
        db_key,
        query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    if not isinstance(result, pd.DataFrame):
        raise TypeError("SQL read did not return a dataframe.")
    return result


def _quote_sql_identifier(identifier: str, backend: str) -> str:
    return quote_identifier_part(identifier, backend, quoted=True)


def _sql_date_expr(backend: str, expression: str) -> str:
    if backend == "ch":
        return f"toDate({expression})"
    return f"CAST({expression} AS DATE)"


def _sql_date_literal(value: pd.Timestamp, backend: str) -> str:
    date_value = pd.Timestamp(value).strftime("%Y-%m-%d")
    if backend == "ch":
        return f"toDate('{date_value}')"
    return f"DATE '{date_value}'"


def _sql_where_clause(
    sql_where: str | None,
    *,
    extra_conditions: Sequence[str] | None = None,
) -> str:
    conditions: list[str] = []
    if sql_where is not None:
        conditions.append(f"({sql_where})")
    if extra_conditions is not None:
        conditions.extend(extra_conditions)
    if not conditions:
        return ""
    return "WHERE " + " AND ".join(conditions)


def _coerce_sql_int(value: object) -> int:
    if value is None or pd.isna(value):
        return 0
    return int(value)


def _coerce_sql_date(value: object, name: str) -> pd.Timestamp:
    if value is None or pd.isna(value):
        raise ValueError(f"SQL source {name} is missing.")
    try:
        return pd.Timestamp(value).normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"SQL source {name} must be datelike.") from exc


def _validate_mde_aggregation_metric_names(
    *,
    values: Sequence[str] | None,
    name: str,
    metric_columns: set[str],
) -> set[str]:
    if values is None:
        return set()
    columns = [str(value) for value in values]
    if len(set(columns)) != len(columns):
        raise ValueError(f"{name} must not contain duplicates.")
    unknown_columns = sorted(set(columns) - metric_columns)
    if unknown_columns:
        raise ValueError(
            f"{name} contains unknown metric column(s): {', '.join(unknown_columns)}."
        )
    return set(columns)


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
