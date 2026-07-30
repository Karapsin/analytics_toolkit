from __future__ import annotations

import re

# ruff: noqa: EM101, EM102, PLR0913, TRY003
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd

from analytics_toolkit import excel, sql
from analytics_toolkit.sql.backends.utils import sql_literal
from analytics_toolkit.sql.core.identifiers import parse_table_identifier

from .constants import DEFAULT_ALPHA, DEFAULT_POWER
from .formatter import format_ab_metrics
from .planning import (
    RatioMetricSpec,
    _coerce_ratio_metric_specs,
    _normalize_sql_where,
    _quote_sql_identifier,
)
from .sql_native import (
    _DEFAULT_BOOTSTRAP_LARGE_SOURCE_RESAMPLES_PER_QUERY,
    _DEFAULT_BOOTSTRAP_LARGE_SOURCE_ROW_THRESHOLD,
    _is_sql_numeric_type,
    compute_test_metrics_sql_native,
)

_SEGMENT_VALUE_COLUMN = "__analytics_toolkit_segment_value__"
_REPORT_SHEET_COLUMN = "__analytics_toolkit_report_sheet__"
_MAX_PROBABILITY = 1.0
_REQUIRED_DISTINCT_REPORT_COLUMNS = 3


@dataclass(frozen=True)
class _ReportSource:
    backend: str
    table_sql: str
    metric_columns: tuple[str, ...]


def compute_metrics_report(
    table_name: str,
    segment: str | None = None,
    *,
    db_key: str,
    sql_where: str | None = None,
    pre_exp_table_name: str | None = None,
    pre_exp_sql_where: str | None = None,
    group: str = "group_name",
    control: str = "control",
    user_id: str = "user_id",
    metric_columns: Sequence[str] | None = None,
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    ratio_metrics: Sequence[dict[str, object] | RatioMetricSpec] | None = None,
    test_vs_test: bool = False,
    multiple_comparisons_adjustment: bool = False,
    multiple_comparisons_adjustment_resamples: int = 2000,
    bootstrap_random_state: int | None = 0,
    bootstrap_n_jobs: int = 1,
    bootstrap_progress: bool = False,
    bootstrap_large_source_row_threshold: int = _DEFAULT_BOOTSTRAP_LARGE_SOURCE_ROW_THRESHOLD,
    bootstrap_large_source_resamples_per_query: int = (
        _DEFAULT_BOOTSTRAP_LARGE_SOURCE_RESAMPLES_PER_QUERY
    ),
    outliers_quantile: float = 0.999,
    outliers_policy: str = "non_zero_truncate",
    concurrency: int = 1,
    fail_fast: bool = True,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = 5,
    progress: bool = False,
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
    query_label: str | None = None,
    pooled_test_group: str = "test_all",
    all_segment_label: str = "ALL",
    metric_names_override: Mapping[str, str] | None = None,
    groups_order: Sequence[str] | None = None,
    create_excel: bool = True,
    excel_file_name: str | Path | None = None,
    report_significance_alpha: float = 0.01,
) -> pd.DataFrame:
    """Compute SQL-native AB metrics, optionally by segment, and write Excel."""
    _validate_report_options(
        segment=segment,
        group=group,
        control=control,
        user_id=user_id,
        pooled_test_group=pooled_test_group,
        all_segment_label=all_segment_label,
        create_excel=create_excel,
        report_significance_alpha=report_significance_alpha,
    )
    normalized_sql_where = _normalize_sql_where(sql_where)
    normalized_pre_exp_sql_where = _normalize_sql_where(pre_exp_sql_where)
    ratio_specs = _coerce_ratio_metric_specs(ratio_metrics)
    source = _resolve_report_source(
        db_key=db_key,
        table_name=table_name,
        segment=segment,
        group=group,
        user_id=user_id,
        metric_columns=metric_columns,
        ratio_metrics=ratio_specs,
    )
    pre_source = (
        _resolve_report_source(
            db_key=db_key,
            table_name=pre_exp_table_name,
            segment=segment,
            group=group,
            user_id=user_id,
            metric_columns=source.metric_columns,
            ratio_metrics=ratio_specs,
        )
        if pre_exp_table_name is not None
        else None
    )
    if pre_source is not None and pre_source.backend != source.backend:
        raise ValueError("pre_exp_table_name must use the same backend as table_name.")

    segment_values = (
        _read_distinct_values(
            db_key=db_key,
            table_sql=source.table_sql,
            column=segment,
            backend=source.backend,
            sql_where=normalized_sql_where,
            output_column=_SEGMENT_VALUE_COLUMN,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        if segment is not None
        else []
    )
    if segment is not None and any(str(value) == all_segment_label for value in segment_values):
        raise ValueError(
            f"all_segment_label {all_segment_label!r} conflicts with an observed segment value."
        )
    observed_groups = [
        str(value)
        for value in _read_distinct_values(
            db_key=db_key,
            table_sql=source.table_sql,
            column=group,
            backend=source.backend,
            sql_where=normalized_sql_where,
            output_column="__analytics_toolkit_group_value__",
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
    ]
    if pooled_test_group in observed_groups:
        raise ValueError(
            f"pooled_test_group {pooled_test_group!r} conflicts with an observed group."
        )
    effective_group_order = _resolve_group_order(
        groups_order=groups_order,
        observed_groups=[*observed_groups, pooled_test_group],
    )

    task_specs = _build_report_tasks(
        table_name=table_name,
        table_sql=source.table_sql,
        pre_exp_table_name=pre_exp_table_name,
        pre_exp_table_sql=(pre_source.table_sql if pre_source is not None else None),
        backend=source.backend,
        segment=segment,
        segment_values=segment_values,
        all_segment_label=all_segment_label,
        sql_where=normalized_sql_where,
        pre_exp_sql_where=normalized_pre_exp_sql_where,
        group=group,
        control=control,
        user_id=user_id,
        metric_columns=list(source.metric_columns),
        ratio_metrics=ratio_specs,
        pooled_test_group=pooled_test_group,
    )
    result = compute_test_metrics_sql_native(
        db_key,
        task_specs,
        group=group,
        control=control,
        user_id=user_id,
        metric_columns=list(source.metric_columns),
        mde_alpha=mde_alpha,
        mde_power=mde_power,
        ratio_metrics=ratio_specs,
        test_vs_test=test_vs_test,
        multiple_comparisons_adjustment=multiple_comparisons_adjustment,
        multiple_comparisons_adjustment_resamples=multiple_comparisons_adjustment_resamples,
        bootstrap_random_state=bootstrap_random_state,
        bootstrap_n_jobs=bootstrap_n_jobs,
        bootstrap_progress=bootstrap_progress,
        bootstrap_large_source_row_threshold=bootstrap_large_source_row_threshold,
        bootstrap_large_source_resamples_per_query=(bootstrap_large_source_resamples_per_query),
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
        concurrency=concurrency,
        fail_fast=fail_fast,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
        progress=progress,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    metrics_df = _combine_report_results(result)
    metrics_df = _apply_metric_name_overrides(metrics_df, metric_names_override)
    metrics_df = _sort_metrics_df(
        metrics_df,
        segment=segment,
        segment_order=[all_segment_label, *segment_values],
        groups_order=effective_group_order,
    )

    if create_excel:
        output = _resolve_excel_output(
            table_name=table_name,
            backend=source.backend,
            excel_file_name=excel_file_name,
        )
        _write_metrics_workbook(
            metrics_df=metrics_df,
            segment=segment,
            control=control,
            groups_order=effective_group_order,
            output=output,
            significance_alpha=report_significance_alpha,
            significance_p_value=("p_values_cuped" if pre_source is not None else "p_values"),
            test_vs_test=test_vs_test,
        )
    return metrics_df


def _resolve_report_source(
    *,
    db_key: str,
    table_name: str,
    segment: str | None,
    group: str,
    user_id: str,
    metric_columns: Sequence[str] | None,
    ratio_metrics: Sequence[dict[str, object]] | None,
) -> _ReportSource:
    info = sql.table_info(db_key, table_name)
    if not info.exists:
        raise ValueError(f"SQL table {table_name!r} does not exist.")
    available = list(info.columns)
    required = [group, user_id] if segment is None else [segment, group, user_id]
    missing = [column for column in required if column not in info.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}.")
    ratio_columns = _ratio_component_columns(ratio_metrics)
    excluded_columns = {group, user_id}
    if segment is not None:
        excluded_columns.add(segment)
    requested_metrics = (
        [str(column) for column in metric_columns]
        if metric_columns is not None
        else [
            column
            for column in available
            if column not in excluded_columns and _is_sql_numeric_type(str(info.columns[column]))
        ]
    )
    if len(set(requested_metrics)) != len(requested_metrics):
        raise ValueError("metric_columns must not contain duplicates.")
    referenced = [*requested_metrics, *ratio_columns]
    missing_referenced = [column for column in referenced if column not in info.columns]
    if missing_referenced:
        missing_names = ", ".join(dict.fromkeys(missing_referenced))
        raise ValueError(f"Missing metric column(s): {missing_names}.")
    if not requested_metrics and not ratio_metrics:
        raise ValueError("At least one metric column or ratio metric is required.")
    table_identifier = parse_table_identifier(
        info.resolved_table or info.table,
        info.backend,
    )
    return _ReportSource(
        backend=info.backend,
        table_sql=table_identifier.render_quoted(info.backend),
        metric_columns=tuple(requested_metrics),
    )


def _ratio_component_columns(
    ratio_metrics: Sequence[dict[str, object]] | None,
) -> list[str]:
    columns: list[str] = []
    for index, spec in enumerate(ratio_metrics or []):
        for field in ("numerator", "denominator"):
            value = str(spec.get(field, "")).strip()
            if not value:
                raise ValueError(f"ratio_metrics[{index}] is missing required key {field!r}.")
            if value not in columns:
                columns.append(value)
    return columns


def _read_distinct_values(
    *,
    db_key: str,
    table_sql: str,
    column: str,
    backend: str,
    sql_where: str | None,
    output_column: str,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: float,
    query_label: str | None,
) -> list[Any]:
    column_sql = _quote_sql_identifier(column, backend)
    output_sql = _quote_sql_identifier(output_column, backend)
    where = _combine_where(sql_where, f"{column_sql} IS NOT NULL")
    query = (
        f"SELECT DISTINCT {column_sql} AS {output_sql} "  # noqa: S608 - identifiers are parsed and adapter-quoted.
        f"FROM {table_sql} WHERE {where} ORDER BY 1"
    )
    values = cast(
        "pd.DataFrame",
        sql.read(
            db_key,
            query,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        ),
    )
    if output_column not in values.columns:
        raise ValueError(f"SQL distinct-value query did not return {output_column!r}.")
    return list(values[output_column].tolist())


def _build_report_tasks(
    *,
    table_name: str,
    table_sql: str,
    pre_exp_table_name: str | None,
    pre_exp_table_sql: str | None,
    backend: str,
    segment: str | None,
    segment_values: Sequence[Any],
    all_segment_label: str,
    sql_where: str | None,
    pre_exp_sql_where: str | None,
    group: str,
    control: str,
    user_id: str,
    metric_columns: Sequence[str],
    ratio_metrics: Sequence[dict[str, object]] | None,
    pooled_test_group: str,
) -> dict[str, dict[str, object]]:
    tasks: dict[str, dict[str, object]] = {}
    labels_and_values = (
        [(all_segment_label, None), *zip(segment_values, segment_values)]
        if segment is not None
        else [(None, None)]
    )
    pooled_source = _build_pooled_source_sql(
        table_sql=table_sql,
        backend=backend,
        segment=segment,
        group=group,
        control=control,
        user_id=user_id,
        metric_columns=metric_columns,
        ratio_metrics=ratio_metrics,
        pooled_test_group=pooled_test_group,
    )
    pooled_pre_source = (
        _build_pooled_source_sql(
            table_sql=pre_exp_table_sql,
            backend=backend,
            segment=segment,
            group=group,
            control=control,
            user_id=user_id,
            metric_columns=metric_columns,
            ratio_metrics=ratio_metrics,
            pooled_test_group=pooled_test_group,
        )
        if pre_exp_table_sql is not None
        else None
    )
    segment_sql = _quote_sql_identifier(segment, backend) if segment is not None else None
    for index, (label, value) in enumerate(labels_and_values):
        segment_filter = None if value is None else f"{segment_sql} = {sql_literal(value)}"
        current_where = _combine_where(sql_where, segment_filter)
        current_pre_where = _combine_where(pre_exp_sql_where, segment_filter)
        common: dict[str, object] = {"sql_where": current_where}
        if segment is not None:
            common["labels"] = {segment: label}
        if pre_exp_table_name is not None:
            common.update(
                {
                    "pre_exp_source": pre_exp_table_name,
                    "pre_exp_source_type": "table",
                    "pre_exp_sql_where": current_pre_where,
                }
            )
        task_prefix = f"segment_{index:04d}" if segment is not None else "total"
        tasks[f"{task_prefix}_groups"] = {
            **common,
            "source": table_name,
            "source_type": "table",
        }
        pooled_common = dict(common)
        if pooled_pre_source is not None:
            pooled_common.update(
                {
                    "pre_exp_source": pooled_pre_source,
                    "pre_exp_source_type": "sql",
                }
            )
        tasks[f"{task_prefix}_pooled"] = {
            **pooled_common,
            "source": pooled_source,
            "source_type": "sql",
        }
    return tasks


def _build_pooled_source_sql(
    *,
    table_sql: str,
    backend: str,
    segment: str | None,
    group: str,
    control: str,
    user_id: str,
    metric_columns: Sequence[str],
    ratio_metrics: Sequence[dict[str, object]] | None,
    pooled_test_group: str,
) -> str:
    group_sql = _quote_sql_identifier(group, backend)
    projected_columns = [user_id]
    if segment is not None:
        projected_columns.append(segment)
    projected_columns.extend([*metric_columns, *_ratio_component_columns(ratio_metrics)])
    projected_columns = list(dict.fromkeys(projected_columns))
    projection = [
        f"{_quote_sql_identifier(column, backend)} AS {_quote_sql_identifier(column, backend)}"
        for column in projected_columns
    ]
    pooled_group = (
        f"CASE WHEN {group_sql} IS NULL THEN NULL "
        f"WHEN {group_sql} = {sql_literal(control)} THEN {sql_literal(control)} "
        f"ELSE {sql_literal(pooled_test_group)} END AS {group_sql}"
    )
    return (
        f"SELECT {', '.join([*projection, pooled_group])} "  # noqa: S608 - identifiers are parsed and adapter-quoted.
        f"FROM {table_sql}"
    )


def _combine_where(base: str | None, extra: str | None) -> str | None:
    clauses = [
        str(value).strip() for value in (base, extra) if value is not None and str(value).strip()
    ]
    if not clauses:
        return None
    return " AND ".join(f"({clause})" for clause in clauses)


def _combine_report_results(
    result: pd.DataFrame | dict[str, pd.DataFrame | str],
) -> pd.DataFrame:
    if isinstance(result, pd.DataFrame):
        return result.copy()
    failures = {name: value for name, value in result.items() if isinstance(value, str)}
    if failures:
        details = "; ".join(f"{name}: {error}" for name, error in failures.items())
        raise RuntimeError(f"Metric report task(s) failed: {details}")
    frames = [value for value in result.values() if isinstance(value, pd.DataFrame)]
    if not frames:
        raise ValueError("Segment metric computation returned no dataframes.")
    return pd.concat(frames, ignore_index=True).drop_duplicates().reset_index(drop=True)


def _apply_metric_name_overrides(
    df: pd.DataFrame,
    overrides: Mapping[str, str] | None,
) -> pd.DataFrame:
    if overrides is None:
        return df
    if not isinstance(overrides, Mapping):
        raise TypeError("metric_names_override must be a mapping or None.")
    normalized: dict[str, str] = {}
    for source, target in overrides.items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError("metric_names_override keys must be non-empty strings.")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("metric_names_override values must be non-empty strings.")
        normalized[source] = target
    observed = [str(value) for value in pd.unique(df["metric_name"])]
    unknown = [name for name in normalized if name not in observed]
    if unknown:
        raise ValueError(f"Unknown metric_names_override key(s): {', '.join(unknown)}.")
    final_names = [normalized.get(name, name) for name in observed]
    duplicates = sorted({name for name in final_names if final_names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"Metric name overrides create duplicate name(s): {', '.join(duplicates)}."
        )
    renamed = df.copy()
    renamed["metric_name"] = renamed["metric_name"].map(
        lambda value: normalized.get(str(value), value)
    )
    return renamed


def _resolve_group_order(
    *,
    groups_order: Sequence[str] | None,
    observed_groups: Sequence[str],
) -> list[str]:
    requested = [] if groups_order is None else list(groups_order)
    if isinstance(groups_order, (str, bytes)):
        raise TypeError("groups_order must be a sequence of group names or None.")
    if any(not isinstance(value, str) or not value.strip() for value in requested):
        raise ValueError("groups_order must contain only non-empty strings.")
    if len(set(requested)) != len(requested):
        raise ValueError("groups_order must not contain duplicates.")
    observed = list(dict.fromkeys(str(value) for value in observed_groups))
    return [*requested, *(value for value in observed if value not in requested)]


def _sort_metrics_df(
    df: pd.DataFrame,
    *,
    segment: str | None,
    segment_order: Sequence[Any],
    groups_order: Sequence[str],
) -> pd.DataFrame:
    result = df.copy()
    metric_rank = {value: index for index, value in enumerate(pd.unique(result["metric_name"]))}
    group_rank = {value: index for index, value in enumerate(groups_order)}
    result["__metric_rank"] = result["metric_name"].map(metric_rank)
    result["__group_1_rank"] = result["group_1"].map(
        lambda value: group_rank.get(str(value), len(group_rank))
    )
    result["__group_2_rank"] = result["group_2"].map(
        lambda value: group_rank.get(str(value), len(group_rank))
    )
    sort_columns = ["__metric_rank", "__group_1_rank", "__group_2_rank"]
    if segment is not None:
        segment_rank = {value: index for index, value in enumerate(segment_order)}
        result["__segment_rank"] = result[segment].map(segment_rank)
        sort_columns.insert(0, "__segment_rank")
    result = result.sort_values(sort_columns, kind="stable").drop(columns=sort_columns)
    if segment is not None:
        ordered_columns = [segment, *(column for column in result.columns if column != segment)]
        result = result[ordered_columns]
    return result.reset_index(drop=True)


def _write_metrics_workbook(
    *,
    metrics_df: pd.DataFrame,
    segment: str | None,
    control: str,
    groups_order: Sequence[str],
    output: Path,
    significance_alpha: float,
    significance_p_value: str,
    test_vs_test: bool,
) -> None:
    values_df = format_ab_metrics(
        metrics_df,
        label_cols=[] if segment is None else [segment],
        output_type=["metric_values"],
        allow_repeated_groups=[control],
    )
    values_df = _reorder_formatted_columns(values_df, segment, groups_order)
    simple_comparison_names = not test_vs_test
    uplifts_df = format_ab_metrics(
        metrics_df,
        label_cols=[] if segment is None else [segment],
        output_type=["delta_relative_significant"],
        significance_alpha=significance_alpha,
        significance_p_value=significance_p_value,
        allow_repeated_groups=[control],
        keep_simple_group_names=simple_comparison_names,
    )
    comparison_columns = _ordered_comparison_columns(
        metrics_df,
        simple_names=simple_comparison_names,
    )
    leading_columns = ([] if segment is None else [segment]) + ["metric"]
    uplifts_df = uplifts_df[
        [*leading_columns, *(column for column in comparison_columns if column in uplifts_df)]
    ]
    excel.break_table(
        [
            values_df.assign(**{_REPORT_SHEET_COLUMN: "summary"}),
            uplifts_df.assign(**{_REPORT_SHEET_COLUMN: "summary"}),
        ],
        output=output,
        break_by=segment,
        sheet_by=_REPORT_SHEET_COLUMN,
        append=False,
    )
    excel.break_table(
        metrics_df.assign(**{_REPORT_SHEET_COLUMN: "raw_metrics"}),
        output=output,
        sheet_by=_REPORT_SHEET_COLUMN,
        append=True,
        prettify=False,
    )


def _reorder_formatted_columns(
    df: pd.DataFrame,
    segment: str | None,
    groups_order: Sequence[str],
) -> pd.DataFrame:
    leading = ([] if segment is None else [segment]) + ["metric"]
    ordered = [group for group in groups_order if group in df.columns]
    remaining = [column for column in df.columns if column not in {*leading, *ordered}]
    return df[[*leading, *ordered, *remaining]]


def _ordered_comparison_columns(
    metrics_df: pd.DataFrame,
    *,
    simple_names: bool,
) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in metrics_df[["group_1", "group_2"]].drop_duplicates().itertuples(index=False):
        test_group = str(row.group_1)
        baseline_group = str(row.group_2)
        column = (
            test_group
            if simple_names
            else f"{test_group}_vs_{baseline_group}_delta_relative_significant"
        )
        if column not in seen:
            seen.add(column)
            columns.append(column)
    return columns


def _resolve_excel_output(
    *,
    table_name: str,
    backend: str,
    excel_file_name: str | Path | None,
) -> Path:
    if excel_file_name is not None:
        if not isinstance(excel_file_name, (str, Path)):
            raise TypeError("excel_file_name must be a string, Path, or None.")
        if not str(excel_file_name).strip():
            raise ValueError("excel_file_name must not be empty.")
        output = Path(excel_file_name)
        return output if output.is_absolute() else Path.cwd() / output
    relation = parse_table_identifier(table_name, backend).relation
    safe_relation = re.sub(r"[^A-Za-z0-9._-]+", "_", relation).strip("._")
    return Path.cwd() / f"{safe_relation or 'ab'}_metrics.xlsx"


def _validate_report_options(
    *,
    segment: str | None,
    group: str,
    control: str,
    user_id: str,
    pooled_test_group: str,
    all_segment_label: str,
    create_excel: bool,
    report_significance_alpha: float,
) -> None:
    named_values = {
        "group": group,
        "control": control,
        "user_id": user_id,
        "pooled_test_group": pooled_test_group,
        "all_segment_label": all_segment_label,
    }
    for name, value in named_values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string.")
    if segment is not None and (not isinstance(segment, str) or not segment.strip()):
        raise ValueError("segment must be a non-empty string or None.")
    required_columns = {group, user_id} if segment is None else {segment, group, user_id}
    expected_column_count = 2 if segment is None else _REQUIRED_DISTINCT_REPORT_COLUMNS
    if len(required_columns) != expected_column_count:
        raise ValueError("segment, group, and user_id must name different columns.")
    if segment is not None and segment in {_SEGMENT_VALUE_COLUMN, _REPORT_SHEET_COLUMN}:
        raise ValueError("segment conflicts with an internal report column name.")
    if pooled_test_group == control:
        raise ValueError("pooled_test_group must differ from control.")
    if not isinstance(create_excel, bool):
        raise TypeError("create_excel must be a boolean.")
    if isinstance(report_significance_alpha, bool) or not isinstance(
        report_significance_alpha, (int, float)
    ):
        raise TypeError("report_significance_alpha must be a real number.")
    if not 0 < float(report_significance_alpha) < _MAX_PROBABILITY:
        raise ValueError("report_significance_alpha must be between 0 and 1.")
