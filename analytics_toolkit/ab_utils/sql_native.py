from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import math
import secrets
import warnings
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
from scipy.stats import t as student_t
from tqdm import tqdm

from analytics_toolkit import sql as sql_facade
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.core.identifiers import parse_table_identifier

from .constants import DEFAULT_ALPHA, DEFAULT_POWER
from .parallel import (
    _DEFAULT_HARD_CONCURRENCY_CAP,
    _annotate_metric_exception,
    _shutdown_executor,
    _validate_concurrency,
    _validate_hard_concurrency_cap,
    _validate_optional_soft_concurrency_cap,
    _validate_progress,
)
from .planning import (
    _coerce_ratio_metric_specs,
    _coerce_sql_float,
    _coerce_sql_int,
    _normalize_sql_where,
    _quote_sql_identifier,
    _read_sql_mde_query,
    _sql_covar_samp_expr,
    _sql_float_expr,
    _sql_native_adjusted_agg_ratio_denominator_expr,
    _sql_native_adjusted_agg_ratio_numerator_expr,
    _sql_native_adjusted_value_expr,
    _sql_native_cutoff_filter,
    _sql_quantile_expr,
    _sql_var_samp_expr,
    _sql_where_clause,
)
from .ratio import _normalize_ratio_metrics
from .rows import _build_comparisons, _build_metric_definitions
from .sql_bootstrap import (
    _build_sql_native_bootstrap_query as _build_sql_native_bootstrap_batch_query,
    _plan_sql_native_bootstrap_batches,
    _reduce_sql_native_bootstrap_batches,
    _validate_sql_native_bootstrap_batch_options,
)
from .stats import (
    _both_present,
    _compute_group_diff_standard_error,
    _compute_mde_from_standard_error,
    _compute_normal_p_value,
    _compute_studentized_statistic,
    _safe_relative,
)
from .validation import (
    _validate_mde_parameters,
    _validate_multiple_comparisons_parameters,
    _validate_outlier_parameters,
)


_SOURCE_TYPES = frozenset({"table", "sql"})
_DEFAULT_BOOTSTRAP_LARGE_SOURCE_ROW_THRESHOLD = 100_000
_DEFAULT_BOOTSTRAP_LARGE_SOURCE_RESAMPLES_PER_QUERY = 10
_SQL_NATIVE_TASK_FIELDS = frozenset(
    {
        "source",
        "source_type",
        "sql_where",
        "pre_exp_source",
        "pre_exp_source_type",
        "pre_exp_sql_where",
        "labels",
        "group",
        "control",
        "user_id",
        "metric_columns",
        "mde_alpha",
        "mde_power",
        "ratio_metrics",
        "test_vs_test",
        "multiple_comparisons_adjustment",
        "multiple_comparisons_adjustment_resamples",
        "bootstrap_random_state",
        "bootstrap_n_jobs",
        "bootstrap_progress",
        "bootstrap_large_source_row_threshold",
        "bootstrap_large_source_resamples_per_query",
        "outliers_quantile",
        "outliers_policy",
        "print_queries",
        "retry_cnt",
        "timeout_increment",
        "query_label",
    }
)


def compute_test_metrics_sql_native(
    db_key: str,
    source: str | Mapping[str, Mapping[str, Any]],
    *,
    source_type: str = "table",
    sql_where: str | None = None,
    pre_exp_source: str | None = None,
    pre_exp_source_type: str = "table",
    pre_exp_sql_where: str | None = None,
    group: str = "group_name",
    control: str = "control",
    user_id: str = "user_id",
    metric_columns: Sequence[str] | None = None,
    mde_alpha: float = DEFAULT_ALPHA,
    mde_power: float = DEFAULT_POWER,
    ratio_metrics: Sequence[dict[str, object]] | None = None,
    test_vs_test: bool = True,
    multiple_comparisons_adjustment: bool = False,
    multiple_comparisons_adjustment_resamples: int = 2000,
    bootstrap_random_state: int | None = 0,
    bootstrap_n_jobs: int = 1,
    bootstrap_progress: bool = False,
    bootstrap_large_source_row_threshold: int = (_DEFAULT_BOOTSTRAP_LARGE_SOURCE_ROW_THRESHOLD),
    bootstrap_large_source_resamples_per_query: int = (
        _DEFAULT_BOOTSTRAP_LARGE_SOURCE_RESAMPLES_PER_QUERY
    ),
    outliers_quantile: float = 0.999,
    outliers_policy: str = "non_zero_truncate",
    concurrency: int = 1,
    fail_fast: bool = True,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = _DEFAULT_HARD_CONCURRENCY_CAP,
    progress: bool = False,
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
) -> pd.DataFrame | dict[str, pd.DataFrame | str]:
    """Compute AB metric comparisons from SQL-side aggregate statistics."""
    defaults = {
        "source_type": source_type,
        "sql_where": sql_where,
        "pre_exp_source": pre_exp_source,
        "pre_exp_source_type": pre_exp_source_type,
        "pre_exp_sql_where": pre_exp_sql_where,
        "group": group,
        "control": control,
        "user_id": user_id,
        "metric_columns": metric_columns,
        "mde_alpha": mde_alpha,
        "mde_power": mde_power,
        "ratio_metrics": ratio_metrics,
        "test_vs_test": test_vs_test,
        "multiple_comparisons_adjustment": multiple_comparisons_adjustment,
        "multiple_comparisons_adjustment_resamples": (multiple_comparisons_adjustment_resamples),
        "bootstrap_random_state": bootstrap_random_state,
        "bootstrap_n_jobs": bootstrap_n_jobs,
        "bootstrap_progress": bootstrap_progress,
        "bootstrap_large_source_row_threshold": bootstrap_large_source_row_threshold,
        "bootstrap_large_source_resamples_per_query": (bootstrap_large_source_resamples_per_query),
        "outliers_quantile": outliers_quantile,
        "outliers_policy": outliers_policy,
        "print_queries": print_queries,
        "retry_cnt": retry_cnt,
        "timeout_increment": timeout_increment,
        "query_label": query_label,
    }
    if isinstance(source, Mapping):
        return _compute_sql_native_metric_tasks(
            db_key=db_key,
            tasks=source,
            defaults=defaults,
            concurrency=concurrency,
            fail_fast=fail_fast,
            soft_concurrency_cap=soft_concurrency_cap,
            hard_concurrency_cap=hard_concurrency_cap,
            progress=progress,
        )

    if concurrency != 1:
        raise ValueError("concurrency can be greater than 1 only when source is a task mapping.")
    return _compute_test_metrics_sql_native_single(
        db_key=db_key,
        source=str(source),
        **defaults,
    )


def _compute_test_metrics_sql_native_single(
    *,
    db_key: str,
    source: str,
    source_type: str,
    sql_where: str | None,
    pre_exp_source: str | None,
    pre_exp_source_type: str,
    pre_exp_sql_where: str | None,
    group: str,
    control: str,
    user_id: str,
    metric_columns: Sequence[str] | None,
    mde_alpha: float,
    mde_power: float,
    ratio_metrics: Sequence[dict[str, object]] | None,
    test_vs_test: bool,
    multiple_comparisons_adjustment: bool,
    multiple_comparisons_adjustment_resamples: int,
    bootstrap_random_state: int | None,
    bootstrap_n_jobs: int,
    bootstrap_progress: bool,
    bootstrap_large_source_row_threshold: int,
    bootstrap_large_source_resamples_per_query: int,
    outliers_quantile: float,
    outliers_policy: str,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    _validate_mde_parameters(mde_alpha=mde_alpha, mde_power=mde_power)
    _validate_multiple_comparisons_parameters(
        multiple_comparisons_adjustment=multiple_comparisons_adjustment,
        multiple_comparisons_adjustment_resamples=multiple_comparisons_adjustment_resamples,
        bootstrap_random_state=bootstrap_random_state,
        bootstrap_n_jobs=bootstrap_n_jobs,
        bootstrap_progress=bootstrap_progress,
    )
    if bootstrap_n_jobs != 1:
        raise ValueError("SQL-native bootstrap is sequential; bootstrap_n_jobs must be 1.")
    _validate_sql_native_bootstrap_batch_options(
        row_threshold=bootstrap_large_source_row_threshold,
        resamples_per_query=bootstrap_large_source_resamples_per_query,
    )
    _validate_outlier_parameters(
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )
    normalized_outliers_policy = str(outliers_policy).strip().lower()
    source_ref = _resolve_sql_native_source(
        db_key=db_key,
        source=source,
        source_type=source_type,
        sql_where=sql_where,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    pre_source_ref = (
        _resolve_sql_native_source(
            db_key=db_key,
            source=pre_exp_source,
            source_type=pre_exp_source_type,
            sql_where=pre_exp_sql_where,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        if pre_exp_source is not None
        else None
    )
    if pre_source_ref is not None and pre_source_ref.backend != source_ref.backend:
        raise ValueError("pre_exp_source must use the same backend as source.")

    ratio_specs = _normalize_ratio_metrics(
        pd.DataFrame(columns=source_ref.columns),
        _coerce_ratio_metric_specs(ratio_metrics),
        reserved_columns={group, user_id},
    )
    mean_metric_columns = _resolve_metric_columns(
        columns=source_ref.columns,
        column_types=source_ref.column_types,
        metric_columns=metric_columns,
        ratio_specs=ratio_specs,
        group=group,
        user_id=user_id,
    )
    if not mean_metric_columns and not ratio_specs:
        raise ValueError("At least one metric column or ratio metric is required.")
    _validate_metric_name_conflicts(mean_metric_columns, ratio_specs)
    metric_definitions = _build_metric_definitions(mean_metric_columns, ratio_specs)

    validation = _read_sql_native_query(
        db_key=db_key,
        query=_build_sql_native_validation_query(
            backend=source_ref.backend,
            source_sql=source_ref.source_sql,
            sql_where=source_ref.sql_where,
            group=group,
            control=control,
            user_id=user_id,
        ),
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    _validate_sql_native_source_stats(validation, group=group, control=control, user_id=user_id)
    source_row_count = _coerce_sql_int(validation.iloc[0].get("row_count"))
    group_names = _read_sql_native_groups(
        db_key=db_key,
        backend=source_ref.backend,
        source_sql=source_ref.source_sql,
        sql_where=source_ref.sql_where,
        group=group,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    comparisons = _build_comparisons(group_names, control, test_vs_test=test_vs_test)

    base_stats = _read_sql_native_query(
        db_key=db_key,
        query=_build_sql_native_group_stats_query(
            backend=source_ref.backend,
            source_sql=source_ref.source_sql,
            sql_where=source_ref.sql_where,
            group=group,
            user_id=user_id,
            metric_definitions=metric_definitions,
            outliers_quantile=float(outliers_quantile),
            outliers_policy=normalized_outliers_policy,
        ),
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    cuped_stats = (
        _read_sql_native_query(
            db_key=db_key,
            query=_build_sql_native_cuped_query(
                backend=source_ref.backend,
                source_sql=source_ref.source_sql,
                sql_where=source_ref.sql_where,
                pre_source_sql=pre_source_ref.source_sql,
                pre_sql_where=pre_source_ref.sql_where,
                group=group,
                user_id=user_id,
                comparisons=comparisons,
                metric_definitions=metric_definitions,
                outliers_quantile=float(outliers_quantile),
                outliers_policy=normalized_outliers_policy,
            ),
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        if pre_source_ref is not None
        else None
    )
    bootstrap_stats = (
        _compute_sql_native_bootstrap_stats(
            db_key=db_key,
            backend=source_ref.backend,
            source_sql=source_ref.source_sql,
            sql_where=source_ref.sql_where,
            group=group,
            user_id=user_id,
            comparisons=comparisons,
            metric_definitions=metric_definitions,
            group_stats=base_stats,
            outliers_quantile=float(outliers_quantile),
            outliers_policy=normalized_outliers_policy,
            resamples=multiple_comparisons_adjustment_resamples,
            random_state=bootstrap_random_state,
            source_row_count=source_row_count,
            large_source_row_threshold=bootstrap_large_source_row_threshold,
            large_source_resamples_per_query=(bootstrap_large_source_resamples_per_query),
            show_progress=bootstrap_progress,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        if multiple_comparisons_adjustment
        else None
    )
    return _finalize_sql_native_metric_result(
        group_stats=base_stats,
        cuped_stats=cuped_stats,
        bootstrap_stats=bootstrap_stats,
        metric_definitions=metric_definitions,
        comparisons=comparisons,
        mde_alpha=mde_alpha,
        mde_power=mde_power,
        include_cuped=pre_source_ref is not None,
        include_bootstrap=multiple_comparisons_adjustment,
    )


class _SqlNativeSource:
    def __init__(
        self,
        *,
        backend: str,
        source_sql: str,
        sql_where: str | None,
        columns: list[str],
        column_types: dict[str, str],
    ) -> None:
        self.backend = backend
        self.source_sql = source_sql
        self.sql_where = sql_where
        self.columns = columns
        self.column_types = column_types


def _resolve_sql_native_source(
    *,
    db_key: str,
    source: str | None,
    source_type: str,
    sql_where: str | None,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> _SqlNativeSource:
    if source is None:
        raise ValueError("source must not be None.")
    normalized_type = _normalize_source_type(source_type)
    normalized_where = _normalize_sql_where(sql_where)
    if normalized_type == "table":
        table = str(source).strip()
        if not table:
            raise ValueError("source table name must not be empty.")
        table_info = sql_facade.table_info(db_key, table)
        if not table_info.exists:
            raise ValueError(f"SQL table {table!r} does not exist.")
        backend = table_info.backend
        source_sql = parse_table_identifier(
            table_info.resolved_table or table_info.table,
            backend,
        ).render_quoted(backend)
        return _SqlNativeSource(
            backend=backend,
            source_sql=source_sql,
            sql_where=normalized_where,
            columns=list(table_info.columns),
            column_types=dict(table_info.columns),
        )

    config = get_connection_config(db_key)
    query = str(source).strip().rstrip(";")
    if not query:
        raise ValueError("source SQL must not be empty.")
    metadata = _read_sql_native_query(
        db_key=db_key,
        query=f"SELECT * FROM ({query}) AS __analytics_toolkit_source_metadata WHERE 1 = 0",
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    return _SqlNativeSource(
        backend=config.backend,
        source_sql=f"({query}) AS __analytics_toolkit_source",
        sql_where=normalized_where,
        columns=[str(column) for column in metadata.columns],
        column_types={str(column): "" for column in metadata.columns},
    )


def _normalize_source_type(source_type: str) -> str:
    normalized = str(source_type).strip().lower()
    if normalized not in _SOURCE_TYPES:
        raise ValueError("source_type must be either 'table' or 'sql'.")
    return normalized


def _resolve_metric_columns(
    *,
    columns: Sequence[str],
    column_types: Mapping[str, str],
    metric_columns: Sequence[str] | None,
    ratio_specs: Sequence[dict[str, str]],
    group: str,
    user_id: str,
) -> list[str]:
    available = set(columns)
    required = [group, user_id]
    missing_required = [column for column in required if column not in available]
    if missing_required:
        raise ValueError(f"Missing required column(s): {', '.join(missing_required)}.")

    if metric_columns is not None:
        resolved = [str(column) for column in metric_columns]
        if len(set(resolved)) != len(resolved):
            raise ValueError("metric_columns must not contain duplicates.")
        missing = [column for column in resolved if column not in available]
        if missing:
            raise ValueError(f"Missing metric column(s): {', '.join(missing)}.")
        return resolved

    excluded = {group, user_id}
    candidates = [column for column in columns if column not in excluded]
    typed_candidates = [
        column for column in candidates if _is_sql_numeric_type(str(column_types.get(column, "")))
    ]
    return typed_candidates if typed_candidates else candidates


def _is_sql_numeric_type(column_type: str) -> bool:
    normalized = column_type.strip().lower()
    if not normalized:
        return False
    return any(
        token in normalized
        for token in (
            "int",
            "float",
            "double",
            "decimal",
            "numeric",
            "number",
            "real",
            "serial",
        )
    )


def _validate_metric_name_conflicts(
    metric_columns: Sequence[str],
    ratio_specs: Sequence[dict[str, str]],
) -> None:
    names = list(metric_columns) + [spec["name"] for spec in ratio_specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Duplicate metric name(s): {', '.join(duplicates)}.")


def _read_sql_native_query(
    *,
    db_key: str,
    query: str,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    return _read_sql_mde_query(
        db_key=db_key,
        query=query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )


def _build_sql_native_validation_query(
    *,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    group: str,
    control: str,
    user_id: str,
) -> str:
    user_expr = _quote_sql_identifier(user_id, backend)
    group_expr = _quote_sql_identifier(group, backend)
    where_clause = _sql_where_clause(sql_where)
    control_literal = _sql_string_literal(control)
    return f"""
WITH source AS (
    SELECT
        {user_expr} AS user_id,
        {group_expr} AS group_name
    FROM {source_sql}
    {where_clause}
),
duplicates AS (
    SELECT user_id, COUNT(*) AS row_count
    FROM source
    WHERE user_id IS NOT NULL
    GROUP BY user_id
    HAVING COUNT(*) > 1
)
SELECT
    COUNT(*) AS row_count,
    SUM(CASE WHEN user_id IS NULL THEN 1 ELSE 0 END) AS null_user_rows,
    SUM(CASE WHEN group_name IS NULL THEN 1 ELSE 0 END) AS null_group_rows,
    COALESCE((SELECT SUM(row_count - 1) FROM duplicates), 0) AS duplicate_user_rows,
    SUM(CASE WHEN group_name = {control_literal} THEN 1 ELSE 0 END) AS control_rows,
    COUNT(DISTINCT CASE WHEN group_name <> {control_literal} THEN group_name ELSE NULL END)
        AS non_control_group_count
FROM source
""".strip()


def _validate_sql_native_source_stats(
    stats: pd.DataFrame,
    *,
    group: str,
    control: str,
    user_id: str,
) -> None:
    if stats.empty:
        raise ValueError("SQL source validation returned no rows.")
    row = stats.iloc[0]
    if _coerce_sql_int(row.get("row_count")) <= 0:
        raise ValueError("SQL source must contain at least one row.")
    if _coerce_sql_int(row.get("null_user_rows")) > 0:
        raise ValueError(f"Column '{user_id}' must not contain missing values.")
    if _coerce_sql_int(row.get("duplicate_user_rows")) > 0:
        raise ValueError(f"Column '{user_id}' must contain unique user ids.")
    if _coerce_sql_int(row.get("null_group_rows")) > 0:
        raise ValueError(f"Column '{group}' must not contain missing values.")
    if _coerce_sql_int(row.get("control_rows")) <= 0:
        raise ValueError(f"Control label '{control}' was not found in column '{group}'.")
    if _coerce_sql_int(row.get("non_control_group_count")) <= 0:
        raise ValueError("At least one non-control group is required.")


def _read_sql_native_groups(
    *,
    db_key: str,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    group: str,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> list[str]:
    group_expr = _quote_sql_identifier(group, backend)
    where_clause = _sql_where_clause(sql_where)
    groups = _read_sql_native_query(
        db_key=db_key,
        query=f"""
SELECT DISTINCT {group_expr} AS group_name
FROM {source_sql}
{where_clause}
ORDER BY group_name
""".strip(),
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    if "group_name" not in groups.columns:
        raise ValueError("SQL group query did not return group_name.")
    return groups["group_name"].tolist()


def _build_sql_native_group_stats_query(
    *,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    group: str,
    user_id: str,
    metric_definitions: Sequence[dict[str, object]],
    outliers_quantile: float,
    outliers_policy: str,
) -> str:
    parts = [
        _build_sql_native_metric_group_stats_query(
            backend=backend,
            source_sql=source_sql,
            sql_where=sql_where,
            group=group,
            user_id=user_id,
            metric_definition=metric_definition,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        for metric_definition in metric_definitions
    ]
    return "\nUNION ALL\n".join(parts)


def _build_sql_native_metric_group_stats_query(
    *,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    group: str,
    user_id: str,
    metric_definition: dict[str, object],
    outliers_quantile: float,
    outliers_policy: str,
) -> str:
    del user_id
    group_expr = _quote_sql_identifier(group, backend)
    group_column = _quote_sql_identifier("group_name", backend)
    where_clause = _sql_where_clause(sql_where)
    metric_name = str(metric_definition["metric_key"])
    if metric_definition["kind"] == "mean":
        value_expr = _sql_float_expr(
            _quote_sql_identifier(str(metric_definition["column"]), backend),
            backend,
        )
        ctes = _build_sql_native_value_group_ctes(
            backend=backend,
            source_sql=source_sql,
            sql_where_clause=where_clause,
            group_expr=group_expr,
            value_expression=value_expr,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        stats_exprs = _sql_value_metric_group_stats_exprs(backend)
    else:
        ratio_spec = dict(metric_definition["ratio_spec"])
        numerator = _sql_float_expr(
            _quote_sql_identifier(str(ratio_spec["numerator"]), backend),
            backend,
        )
        denominator = _sql_float_expr(
            _quote_sql_identifier(str(ratio_spec["denominator"]), backend),
            backend,
        )
        if ratio_spec["level"] == "user":
            value_expr = (
                "CASE WHEN "
                f"{numerator} IS NOT NULL AND {denominator} IS NOT NULL "
                f"AND {denominator} > 0 THEN {numerator} / {denominator} "
                "ELSE NULL END"
            )
            ctes = _build_sql_native_value_group_ctes(
                backend=backend,
                source_sql=source_sql,
                sql_where_clause=where_clause,
                group_expr=group_expr,
                value_expression=value_expr,
                outliers_quantile=outliers_quantile,
                outliers_policy=outliers_policy,
            )
            stats_exprs = _sql_value_metric_group_stats_exprs(backend)
        else:
            ctes = _build_sql_native_agg_ratio_group_ctes(
                backend=backend,
                source_sql=source_sql,
                sql_where_clause=where_clause,
                group_expr=group_expr,
                numerator=numerator,
                denominator=denominator,
                outliers_quantile=outliers_quantile,
                outliers_policy=outliers_policy,
            )
            stats_exprs = _sql_agg_ratio_group_stats_exprs(backend)
    with_sql = ",\n".join(ctes)
    return f"""
WITH {with_sql}
SELECT
    {_sql_string_literal(metric_name)} AS metric_name,
    {_sql_string_literal(str(metric_definition["kind"]))} AS metric_type,
    {group_column} AS group_name,
    {stats_exprs}
FROM stats
""".strip()


def _build_sql_native_value_group_ctes(
    *,
    backend: str,
    source_sql: str,
    sql_where_clause: str,
    group_expr: str,
    value_expression: str,
    outliers_quantile: float,
    outliers_policy: str,
) -> list[str]:
    cutoff_filter = _sql_native_cutoff_filter("value", outliers_policy)
    adjusted_value = _sql_native_adjusted_value_expr(
        value_expression="value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    outlier_expr = "CASE WHEN cutoff.cutoff IS NOT NULL AND value > cutoff.cutoff THEN 1 ELSE 0 END"
    return [
        f"""
source AS (
    SELECT
        {group_expr} AS group_name,
        {value_expression} AS value
    FROM {source_sql}
    {sql_where_clause}
)
""".strip(),
        f"""
cutoff AS (
    SELECT {_sql_quantile_expr("value", outliers_quantile, backend)} AS cutoff
    FROM source
    WHERE value IS NOT NULL{cutoff_filter}
)
""".strip(),
        f"""
prepared AS (
    SELECT
        source.group_name AS group_name,
        {adjusted_value} AS metric_value,
        {outlier_expr} AS outlier_flag,
        cutoff.cutoff AS cutoff
    FROM source
    CROSS JOIN cutoff
)
""".strip(),
        f"""
stats AS (
    SELECT
        group_name,
        COUNT(metric_value) AS n,
        AVG(metric_value) AS metric_value,
        {_sql_var_samp_expr("metric_value", backend)} AS variance_value,
        MAX(cutoff) AS outliers_cutoff,
        SUM(outlier_flag) AS outliers_n
    FROM prepared
    GROUP BY group_name
)
""".strip(),
    ]


def _build_sql_native_agg_ratio_group_ctes(
    *,
    backend: str,
    source_sql: str,
    sql_where_clause: str,
    group_expr: str,
    numerator: str,
    denominator: str,
    outliers_quantile: float,
    outliers_policy: str,
) -> list[str]:
    ratio_value = (
        "CASE WHEN "
        f"{numerator} IS NOT NULL AND {denominator} IS NOT NULL "
        f"AND {denominator} > 0 THEN {numerator} / {denominator} "
        "ELSE NULL END"
    )
    cutoff_filter = _sql_native_cutoff_filter("ratio_value", outliers_policy)
    adjusted_numerator = _sql_native_adjusted_agg_ratio_numerator_expr(
        numerator_expression="numerator",
        denominator_expression="denominator",
        ratio_expression="ratio_value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    adjusted_denominator = _sql_native_adjusted_agg_ratio_denominator_expr(
        denominator_expression="denominator",
        ratio_expression="ratio_value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    outlier_expr = (
        "CASE WHEN cutoff.cutoff IS NOT NULL AND ratio_value > cutoff.cutoff THEN 1 ELSE 0 END"
    )
    return [
        f"""
source AS (
    SELECT
        {group_expr} AS group_name,
        {numerator} AS numerator,
        {denominator} AS denominator,
        {ratio_value} AS ratio_value
    FROM {source_sql}
    {sql_where_clause}
)
""".strip(),
        f"""
cutoff AS (
    SELECT {_sql_quantile_expr("ratio_value", outliers_quantile, backend)} AS cutoff
    FROM source
    WHERE ratio_value IS NOT NULL{cutoff_filter}
)
""".strip(),
        f"""
prepared AS (
    SELECT
        source.group_name AS group_name,
        {adjusted_numerator} AS numerator,
        {adjusted_denominator} AS denominator,
        {outlier_expr} AS outlier_flag,
        cutoff.cutoff AS cutoff
    FROM source
    CROSS JOIN cutoff
)
""".strip(),
        """
summary AS (
    SELECT
        group_name,
        COUNT(*) AS n,
        SUM(numerator) AS numerator_sum,
        SUM(denominator) AS denominator_sum,
        AVG(denominator) AS denominator_mean,
        CASE WHEN SUM(denominator) > 0
            THEN SUM(numerator) / SUM(denominator)
            ELSE NULL
        END AS ratio,
        MAX(cutoff) AS outliers_cutoff,
        SUM(outlier_flag) AS outliers_n
    FROM prepared
    WHERE numerator IS NOT NULL AND denominator IS NOT NULL
    GROUP BY group_name
)
""".strip(),
        """
linearized AS (
    SELECT
        prepared.group_name AS group_name,
        prepared.numerator - summary.ratio * prepared.denominator AS metric_value
    FROM prepared
    INNER JOIN summary
        ON prepared.group_name = summary.group_name
    WHERE prepared.numerator IS NOT NULL
        AND prepared.denominator IS NOT NULL
        AND summary.ratio IS NOT NULL
)
""".strip(),
        f"""
stats AS (
    SELECT
        summary.group_name AS group_name,
        summary.n AS n,
        summary.ratio AS metric_value,
        CASE WHEN summary.n >= 2
            AND summary.denominator_mean <> 0
            AND summary.ratio IS NOT NULL
            THEN {_sql_var_samp_expr("linearized.metric_value", backend)}
                / (summary.n * summary.denominator_mean * summary.denominator_mean)
            ELSE NULL
        END AS variance_value,
        summary.outliers_cutoff AS outliers_cutoff,
        summary.outliers_n AS outliers_n
    FROM summary
    LEFT JOIN linearized
        ON summary.group_name = linearized.group_name
    GROUP BY
        summary.group_name,
        summary.n,
        summary.ratio,
        summary.denominator_mean,
        summary.outliers_cutoff,
        summary.outliers_n
)
""".strip(),
    ]


def _sql_value_metric_group_stats_exprs(backend: str) -> str:
    del backend
    return """
    n,
    metric_value,
    variance_value,
    outliers_cutoff,
    outliers_n
""".strip()


def _sql_agg_ratio_group_stats_exprs(backend: str) -> str:
    del backend
    return """
    n,
    metric_value,
    variance_value,
    outliers_cutoff,
    outliers_n
""".strip()


def _build_sql_native_cuped_query(
    *,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    pre_source_sql: str,
    pre_sql_where: str | None,
    group: str,
    user_id: str,
    comparisons: Sequence[tuple[str, str]],
    metric_definitions: Sequence[dict[str, object]],
    outliers_quantile: float,
    outliers_policy: str,
) -> str:
    parts = [
        _build_sql_native_metric_cuped_query(
            backend=backend,
            source_sql=source_sql,
            sql_where=sql_where,
            pre_source_sql=pre_source_sql,
            pre_sql_where=pre_sql_where,
            group=group,
            user_id=user_id,
            test_group=test_group,
            baseline_group=baseline_group,
            metric_definition=metric_definition,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        for test_group, baseline_group in comparisons
        for metric_definition in metric_definitions
    ]
    return "\nUNION ALL\n".join(parts)


def _build_sql_native_metric_cuped_query(
    *,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    pre_source_sql: str,
    pre_sql_where: str | None,
    group: str,
    user_id: str,
    test_group: str,
    baseline_group: str,
    metric_definition: dict[str, object],
    outliers_quantile: float,
    outliers_policy: str,
) -> str:
    user_expr = _quote_sql_identifier(user_id, backend)
    group_expr = _quote_sql_identifier(group, backend)
    exp_value_expr, pre_value_expr = _sql_native_cuped_value_expressions(
        backend=backend,
        metric_definition=metric_definition,
    )
    covar_expr = _sql_covar_samp_expr("metric_exp", "metric_pre", backend)
    pre_var_expr = _sql_var_samp_expr("metric_pre", backend)
    adjusted_var_expr = _sql_var_samp_expr("adjusted_value", backend)
    adjusted_mean_expr = "AVG(adjusted_value)"
    exp_where = _sql_where_clause(sql_where)
    pre_where = _sql_where_clause(pre_sql_where)
    cutoff_filter = _sql_native_cutoff_filter("value", outliers_policy)
    exp_adjusted = _sql_native_adjusted_value_expr(
        value_expression="value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    pre_adjusted = _sql_native_adjusted_value_expr(
        value_expression="value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    return f"""
WITH exp_raw AS (
    SELECT
        {user_expr} AS user_id,
        {group_expr} AS group_name,
        {exp_value_expr} AS value
    FROM {source_sql}
    {exp_where}
),
exp_cutoff AS (
    SELECT {_sql_quantile_expr("value", outliers_quantile, backend)} AS cutoff
    FROM exp_raw
    WHERE value IS NOT NULL{cutoff_filter}
),
exp_values AS (
    SELECT
        user_id,
        group_name,
        {exp_adjusted} AS metric_exp
    FROM exp_raw
    CROSS JOIN exp_cutoff AS cutoff
),
pre_raw AS (
    SELECT
        {user_expr} AS user_id,
        {pre_value_expr} AS value
    FROM {pre_source_sql}
    {pre_where}
),
pre_cutoff AS (
    SELECT {_sql_quantile_expr("value", outliers_quantile, backend)} AS cutoff
    FROM pre_raw
    WHERE value IS NOT NULL{cutoff_filter}
),
pre_values AS (
    SELECT
        user_id,
        {pre_adjusted} AS metric_pre
    FROM pre_raw
    CROSS JOIN pre_cutoff AS cutoff
),
pairs AS (
    SELECT
        exp_values.user_id AS user_id,
        exp_values.group_name AS group_name,
        exp_values.metric_exp AS metric_exp,
        pre_values.metric_pre AS metric_pre
    FROM exp_values
    INNER JOIN pre_values
        ON exp_values.user_id = pre_values.user_id
    WHERE exp_values.metric_exp IS NOT NULL
        AND pre_values.metric_pre IS NOT NULL
        AND exp_values.group_name IN (
            {_sql_string_literal(test_group)}, {_sql_string_literal(baseline_group)}
        )
),
summary AS (
    SELECT
        COUNT(*) AS pair_n,
        {pre_var_expr} AS pre_var,
        {covar_expr} AS covar_value,
        AVG(metric_pre) AS pre_mean
    FROM pairs
),
adjusted AS (
    SELECT
        pairs.group_name AS group_name,
        CASE WHEN summary.pair_n >= 2 AND summary.pre_var > 0
            THEN pairs.metric_exp
                - (summary.covar_value / summary.pre_var)
                * (pairs.metric_pre - summary.pre_mean)
            ELSE NULL
        END AS adjusted_value
    FROM pairs
    CROSS JOIN summary
),
group_stats AS (
    SELECT
        group_name,
        COUNT(adjusted_value) AS n,
        {adjusted_mean_expr} AS metric_value,
        {adjusted_var_expr} AS variance_value
    FROM adjusted
    GROUP BY group_name
)
SELECT
    {_sql_string_literal(str(metric_definition["metric_key"]))} AS metric_name,
    {_sql_string_literal(test_group)} AS group_1,
    {_sql_string_literal(baseline_group)} AS group_2,
    summary.pair_n AS pair_n,
    summary.pre_var AS pre_var,
    test_stats.n AS n1,
    control_stats.n AS n0,
    test_stats.metric_value AS metric_test,
    control_stats.metric_value AS metric_control,
    test_stats.variance_value AS variance_test,
    control_stats.variance_value AS variance_control
FROM summary
LEFT JOIN group_stats AS test_stats
    ON test_stats.group_name = {_sql_string_literal(test_group)}
LEFT JOIN group_stats AS control_stats
    ON control_stats.group_name = {_sql_string_literal(baseline_group)}
""".strip()


def _sql_native_cuped_value_expressions(
    *,
    backend: str,
    metric_definition: dict[str, object],
) -> tuple[str, str]:
    if metric_definition["kind"] == "mean":
        expression = _sql_float_expr(
            _quote_sql_identifier(str(metric_definition["column"]), backend),
            backend,
        )
        return expression, expression
    ratio_spec = dict(metric_definition["ratio_spec"])
    numerator = _sql_float_expr(
        _quote_sql_identifier(str(ratio_spec["numerator"]), backend),
        backend,
    )
    denominator = _sql_float_expr(
        _quote_sql_identifier(str(ratio_spec["denominator"]), backend),
        backend,
    )
    expression = (
        "CASE WHEN "
        f"{numerator} IS NOT NULL AND {denominator} IS NOT NULL "
        f"AND {denominator} > 0 THEN {numerator} / {denominator} "
        "ELSE NULL END"
    )
    return expression, expression


def _build_sql_native_bootstrap_query(
    *,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    group: str,
    user_id: str,
    comparisons: Sequence[tuple[str, str]],
    metric_definitions: Sequence[dict[str, object]],
    outliers_quantile: float,
    outliers_policy: str,
    resamples: int,
    random_state: int | None,
    resample_start: int = 1,
    observed_statistics: Mapping[tuple[str, str, str], tuple[float, float]] | None = None,
) -> str:
    return _build_sql_native_bootstrap_batch_query(
        backend=backend,
        source_sql=source_sql,
        sql_where=sql_where,
        group=group,
        user_id=user_id,
        comparisons=comparisons,
        metric_definitions=metric_definitions,
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
        resamples=resamples,
        random_state=random_state,
        resample_start=resample_start,
        observed_statistics=observed_statistics,
    )


def _compute_sql_native_bootstrap_stats(
    *,
    db_key: str,
    backend: str,
    source_sql: str,
    sql_where: str | None,
    group: str,
    user_id: str,
    comparisons: Sequence[tuple[str, str]],
    metric_definitions: Sequence[dict[str, object]],
    group_stats: pd.DataFrame,
    outliers_quantile: float,
    outliers_policy: str,
    resamples: int,
    random_state: int | None,
    source_row_count: int,
    large_source_row_threshold: int,
    large_source_resamples_per_query: int,
    show_progress: bool,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    observed_statistics = _build_sql_native_observed_statistics(
        group_stats=group_stats,
        metric_definitions=metric_definitions,
        comparisons=comparisons,
    )
    batches = _plan_sql_native_bootstrap_batches(
        row_count=source_row_count,
        resamples=resamples,
        large_source_row_threshold=large_source_row_threshold,
        large_source_resamples_per_query=large_source_resamples_per_query,
    )
    if len(batches) > 1:
        largest_batch = max(count for _, count in batches)
        warnings.warn(
            f"SQL-native bootstrap will execute {len(batches)} sequential queries "
            f"for {resamples} resamples (up to approximately "
            f"{source_row_count * largest_batch:,} sampled rows per query). "
            "Keep the source stable until all batches finish.",
            RuntimeWarning,
            stacklevel=3,
        )

    seed = secrets.randbits(63) if random_state is None else random_state
    batch_frames: list[tuple[int, pd.DataFrame]] = []
    progress_bar = tqdm(
        batches,
        desc="SQL-native bootstrap",
        unit="query",
        disable=not show_progress,
    )
    for resample_start, batch_size in progress_bar:
        frame = _read_sql_native_query(
            db_key=db_key,
            query=_build_sql_native_bootstrap_query(
                backend=backend,
                source_sql=source_sql,
                sql_where=sql_where,
                group=group,
                user_id=user_id,
                comparisons=comparisons,
                metric_definitions=metric_definitions,
                outliers_quantile=outliers_quantile,
                outliers_policy=outliers_policy,
                resamples=batch_size,
                random_state=seed,
                resample_start=resample_start,
                observed_statistics=observed_statistics,
            ),
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        batch_frames.append((batch_size, frame))
    return _reduce_sql_native_bootstrap_batches(
        batches=batch_frames,
        observed_statistics=observed_statistics,
    )


def _build_sql_native_observed_statistics(
    *,
    group_stats: pd.DataFrame,
    metric_definitions: Sequence[dict[str, object]],
    comparisons: Sequence[tuple[str, str]],
) -> dict[tuple[str, str, str], tuple[float, float]]:
    stats_by_key = {
        (str(row["metric_name"]), str(row["group_name"])): row for _, row in group_stats.iterrows()
    }
    observed: dict[tuple[str, str, str], tuple[float, float]] = {}
    for metric_definition in metric_definitions:
        metric_name = str(metric_definition["metric_key"])
        is_agg_ratio = (
            metric_definition["kind"] == "ratio"
            and dict(metric_definition["ratio_spec"])["level"] == "agg"
        )
        for test_group, baseline_group in comparisons:
            baseline = stats_by_key.get(
                (metric_name, baseline_group),
                pd.Series(dtype=object),
            )
            test = stats_by_key.get(
                (metric_name, test_group),
                pd.Series(dtype=object),
            )
            baseline_mean = _coerce_sql_float(baseline.get("metric_value"))
            test_mean = _coerce_sql_float(test.get("metric_value"))
            delta = (
                test_mean - baseline_mean if _both_present(test_mean, baseline_mean) else math.nan
            )
            baseline_variance = _coerce_sql_float(baseline.get("variance_value"))
            test_variance = _coerce_sql_float(test.get("variance_value"))
            if is_agg_ratio:
                standard_error = (
                    math.sqrt(baseline_variance + test_variance)
                    if math.isfinite(baseline_variance)
                    and math.isfinite(test_variance)
                    and baseline_variance + test_variance > 0
                    else math.nan
                )
            else:
                standard_error = _compute_group_diff_standard_error(
                    baseline_variance=baseline_variance,
                    baseline_n=_coerce_sql_int(baseline.get("n")),
                    test_variance=test_variance,
                    test_n=_coerce_sql_int(test.get("n")),
                )
            observed[(metric_name, test_group, baseline_group)] = (
                delta,
                standard_error,
            )
    return observed


def _finalize_sql_native_metric_result(
    *,
    group_stats: pd.DataFrame,
    cuped_stats: pd.DataFrame | None,
    bootstrap_stats: pd.DataFrame | None,
    metric_definitions: Sequence[dict[str, object]],
    comparisons: Sequence[tuple[str, str]],
    mde_alpha: float,
    mde_power: float,
    include_cuped: bool,
    include_bootstrap: bool,
) -> pd.DataFrame:
    stats_by_key = {
        (str(row["metric_name"]), row["group_name"]): row for _, row in group_stats.iterrows()
    }
    cuped_by_key = (
        {
            (str(row["metric_name"]), row["group_1"], row["group_2"]): row
            for _, row in cuped_stats.iterrows()
        }
        if cuped_stats is not None
        else {}
    )
    bootstrap_by_key = (
        {
            (str(row["metric_name"]), row["group_1"], row["group_2"]): row
            for _, row in bootstrap_stats.iterrows()
        }
        if bootstrap_stats is not None
        else {}
    )
    rows: list[dict[str, object]] = []
    for test_group, baseline_group in comparisons:
        for metric_definition in metric_definitions:
            metric_name = str(metric_definition["metric_key"])
            baseline = stats_by_key.get((metric_name, baseline_group), pd.Series(dtype=object))
            test = stats_by_key.get((metric_name, test_group), pd.Series(dtype=object))
            row = _build_sql_native_metric_row(
                metric_definition=metric_definition,
                baseline_group=baseline_group,
                test_group=test_group,
                baseline=baseline,
                test=test,
                mde_alpha=mde_alpha,
                mde_power=mde_power,
            )
            if include_cuped:
                _add_sql_native_cuped_fields(
                    row=row,
                    cuped_row=cuped_by_key.get((metric_name, test_group, baseline_group)),
                    metric_name=metric_name,
                    baseline_group=baseline_group,
                    test_group=test_group,
                    mde_alpha=mde_alpha,
                    mde_power=mde_power,
                )
            if include_bootstrap:
                bootstrap_row = bootstrap_by_key.get((metric_name, test_group, baseline_group))
                row["s.e. bootstrap"] = (
                    _coerce_sql_float(bootstrap_row.get("se_bootstrap"))
                    if bootstrap_row is not None
                    else math.nan
                )
                row["bootstrap_adj_p"] = (
                    _coerce_sql_float(bootstrap_row.get("bootstrap_adj_p"))
                    if bootstrap_row is not None
                    else math.nan
                )
            rows.append(row)

    columns = [
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
        "delta_relative",
        "mde_abs",
        "mde_relative",
        "s.e.",
        "p-value",
    ]
    if include_cuped:
        columns.extend(["s.e. CUPED", "p-value CUPED", "mde_abs CUPED", "mde_relative CUPED"])
    if include_bootstrap:
        columns.extend(["s.e. bootstrap", "bootstrap_adj_p"])
    return pd.DataFrame(rows, columns=columns)


def _build_sql_native_metric_row(
    *,
    metric_definition: dict[str, object],
    baseline_group: str,
    test_group: str,
    baseline: pd.Series,
    test: pd.Series,
    mde_alpha: float,
    mde_power: float,
) -> dict[str, object]:
    metric_name = str(metric_definition["metric_key"])
    baseline_mean = _coerce_sql_float(baseline.get("metric_value"))
    test_mean = _coerce_sql_float(test.get("metric_value"))
    baseline_variance = _coerce_sql_float(baseline.get("variance_value"))
    test_variance = _coerce_sql_float(test.get("variance_value"))
    baseline_n = _coerce_sql_int(baseline.get("n"))
    test_n = _coerce_sql_int(test.get("n"))
    is_agg_ratio = (
        metric_definition["kind"] == "ratio"
        and dict(metric_definition["ratio_spec"])["level"] == "agg"
    )
    if is_agg_ratio:
        standard_error = (
            math.sqrt(baseline_variance + test_variance)
            if not math.isnan(baseline_variance) and not math.isnan(test_variance)
            else math.nan
        )
    else:
        standard_error = _compute_group_diff_standard_error(
            baseline_variance=baseline_variance,
            baseline_n=baseline_n,
            test_variance=test_variance,
            test_n=test_n,
        )
    delta_abs = test_mean - baseline_mean if _both_present(test_mean, baseline_mean) else math.nan
    if is_agg_ratio:
        p_value = _compute_normal_p_value(delta_abs=delta_abs, standard_error=standard_error)
    else:
        p_value = _compute_welch_p_value_from_summary(
            delta_abs=delta_abs,
            standard_error=standard_error,
            baseline_variance=baseline_variance,
            baseline_n=baseline_n,
            test_variance=test_variance,
            test_n=test_n,
        )
    mde_abs = _compute_mde_from_standard_error(
        standard_error=standard_error,
        alpha=mde_alpha,
        power=mde_power,
    )
    return {
        "metric_type": str(metric_definition["kind"]),
        "group_1": test_group,
        "group_2": baseline_group,
        "metric_name": metric_name,
        "n0": baseline_n,
        "n1": test_n,
        "outliers_cutoff": _coerce_sql_float(baseline.get("outliers_cutoff")),
        "outliers_n_control": _coerce_sql_int(baseline.get("outliers_n")),
        "outliers_n_test": _coerce_sql_int(test.get("outliers_n")),
        "metric_control": baseline_mean,
        "metric_test": test_mean,
        "variance_control": baseline_variance,
        "variance_test": test_variance,
        "delta_abs": delta_abs,
        "delta_relative": _safe_relative(delta_abs, baseline_mean),
        "mde_abs": mde_abs,
        "mde_relative": _safe_relative(mde_abs, baseline_mean),
        "s.e.": standard_error,
        "p-value": p_value,
    }


def _add_sql_native_cuped_fields(
    *,
    row: dict[str, object],
    cuped_row: pd.Series | None,
    metric_name: str,
    baseline_group: str,
    test_group: str,
    mde_alpha: float,
    mde_power: float,
) -> None:
    if cuped_row is None:
        reason = "SQL CUPED stats are unavailable"
        _warn_sql_native_cuped(metric_name, test_group, baseline_group, reason)
        se = math.nan
        p_value = math.nan
    else:
        pair_n = _coerce_sql_int(cuped_row.get("pair_n"))
        pre_var = _coerce_sql_float(cuped_row.get("pre_var"))
        if pair_n <= 0:
            reason = "no overlapping non-missing experiment/pre-experiment observations"
            _warn_sql_native_cuped(metric_name, test_group, baseline_group, reason)
            se = math.nan
            p_value = math.nan
        elif math.isnan(pre_var) or pre_var <= 0:
            reason = "pre-experiment covariate variance is not positive"
            _warn_sql_native_cuped(metric_name, test_group, baseline_group, reason)
            se = math.nan
            p_value = math.nan
        else:
            baseline_variance = _coerce_sql_float(cuped_row.get("variance_control"))
            test_variance = _coerce_sql_float(cuped_row.get("variance_test"))
            baseline_n = _coerce_sql_int(cuped_row.get("n0"))
            test_n = _coerce_sql_int(cuped_row.get("n1"))
            baseline_mean = _coerce_sql_float(cuped_row.get("metric_control"))
            test_mean = _coerce_sql_float(cuped_row.get("metric_test"))
            delta_abs = (
                test_mean - baseline_mean if _both_present(test_mean, baseline_mean) else math.nan
            )
            se = _compute_group_diff_standard_error(
                baseline_variance=baseline_variance,
                baseline_n=baseline_n,
                test_variance=test_variance,
                test_n=test_n,
            )
            p_value = _compute_welch_p_value_from_summary(
                delta_abs=delta_abs,
                standard_error=se,
                baseline_variance=baseline_variance,
                baseline_n=baseline_n,
                test_variance=test_variance,
                test_n=test_n,
            )
            if math.isnan(p_value) or math.isnan(se):
                _warn_sql_native_cuped(
                    metric_name,
                    test_group,
                    baseline_group,
                    "not enough overlapping observations to run the CUPED t-test",
                )
                p_value = math.nan
                se = math.nan
    mde_abs = _compute_mde_from_standard_error(
        standard_error=se,
        alpha=mde_alpha,
        power=mde_power,
    )
    row["s.e. CUPED"] = se
    row["p-value CUPED"] = p_value
    row["mde_abs CUPED"] = mde_abs
    row["mde_relative CUPED"] = _safe_relative(mde_abs, float(row["metric_control"]))


def _warn_sql_native_cuped(
    metric_name: str,
    test_group: str,
    baseline_group: str,
    reason: str,
) -> None:
    warnings.warn(
        (
            f"Could not compute CUPED p-value for metric '{metric_name}' "
            f"({test_group!r} vs {baseline_group!r}): {reason}."
        ),
        stacklevel=3,
    )


def _compute_welch_p_value_from_summary(
    *,
    delta_abs: float,
    standard_error: float,
    baseline_variance: float,
    baseline_n: int,
    test_variance: float,
    test_n: int,
) -> float:
    statistic = _compute_studentized_statistic(delta_abs, standard_error)
    if math.isnan(statistic) or baseline_n < 2 or test_n < 2:
        return math.nan
    baseline_term = baseline_variance / baseline_n
    test_term = test_variance / test_n
    numerator = (baseline_term + test_term) ** 2
    denominator = (baseline_term**2 / (baseline_n - 1)) + (test_term**2 / (test_n - 1))
    if denominator <= 0 or math.isnan(denominator):
        return math.nan
    degrees = numerator / denominator
    return float(2 * student_t.sf(abs(statistic), degrees))


def _compute_sql_native_metric_tasks(
    *,
    db_key: str,
    tasks: Mapping[str, Mapping[str, Any]],
    defaults: Mapping[str, Any],
    concurrency: int,
    fail_fast: bool,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
    progress: bool,
) -> dict[str, pd.DataFrame | str]:
    task_defs = _validate_sql_native_tasks(tasks, defaults=defaults)
    _validate_concurrency(concurrency)
    _validate_optional_soft_concurrency_cap(soft_concurrency_cap)
    _validate_hard_concurrency_cap(hard_concurrency_cap)
    _validate_progress(progress)
    soft_cap = concurrency if soft_concurrency_cap is None else soft_concurrency_cap
    if min(concurrency, soft_cap) > hard_concurrency_cap:
        raise ValueError(
            "effective concurrency exceeds hard_concurrency_cap "
            f"({min(concurrency, soft_cap)} > {hard_concurrency_cap})."
        )

    if concurrency == 1:
        iterator: Any = task_defs
        progress_bar = tqdm(
            total=len(task_defs),
            desc="compute_test_metrics_sql_native tasks",
            unit="task",
            disable=not progress,
        )
        try:
            results: dict[str, pd.DataFrame | str] = {}
            for name, kwargs, labels in iterator:
                try:
                    results[name] = _run_sql_native_task(db_key, kwargs, labels)
                except BaseException as exc:
                    _annotate_metric_exception(exc, name)
                    if fail_fast:
                        raise
                    results[name] = str(exc)
                finally:
                    progress_bar.update(1)
            return results
        finally:
            progress_bar.close()

    executor: ThreadPoolExecutor | None = None
    shutdown_called = False
    progress_bar = tqdm(
        total=len(task_defs),
        desc="compute_test_metrics_sql_native tasks",
        unit="task",
        disable=not progress,
    )
    try:
        executor = ThreadPoolExecutor(max_workers=min(concurrency, soft_cap))
        future_to_task: dict[Future[pd.DataFrame], tuple[int, str]] = {
            executor.submit(_run_sql_native_task, db_key, kwargs, labels): (index, name)
            for index, (name, kwargs, labels) in enumerate(task_defs)
        }
        results_by_index: dict[int, pd.DataFrame | str] = {}
        for future in as_completed(future_to_task):
            index, name = future_to_task[future]
            try:
                results_by_index[index] = future.result()
            except BaseException as exc:
                _annotate_metric_exception(exc, name)
                if fail_fast:
                    for pending in future_to_task:
                        if pending is not future:
                            pending.cancel()
                    _shutdown_executor(executor, wait=False, cancel_futures=True)
                    shutdown_called = True
                    raise
                results_by_index[index] = str(exc)
            finally:
                progress_bar.update(1)
        return {
            name: results_by_index[index]
            for index, (name, _kwargs, _labels) in enumerate(task_defs)
        }
    finally:
        progress_bar.close()
        if executor is not None and not shutdown_called:
            _shutdown_executor(executor, wait=True, cancel_futures=True)


def _validate_sql_native_tasks(
    tasks: Mapping[str, Mapping[str, Any]],
    *,
    defaults: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    if not isinstance(tasks, Mapping):
        raise TypeError("source task mapping must be a non-empty mapping.")
    if not tasks:
        raise ValueError("source task mapping must not be empty.")
    task_defs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for name, spec in tasks.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Task names must be non-empty strings.")
        if not isinstance(spec, Mapping):
            raise TypeError(f"Task {name!r} must be a mapping.")
        unknown = sorted(set(spec) - _SQL_NATIVE_TASK_FIELDS)
        if unknown:
            raise TypeError(f"Task {name!r} got unexpected field(s): {', '.join(unknown)}.")
        kwargs = dict(defaults)
        kwargs.update(spec)
        if "source" not in kwargs or kwargs["source"] is None:
            raise ValueError(f"Task {name!r} must define source.")
        labels = kwargs.pop("labels", {})
        if labels is None:
            labels = {}
        if not isinstance(labels, Mapping):
            raise TypeError(f"Task {name!r} labels must be a mapping.")
        task_defs.append((name, kwargs, dict(labels)))
    return task_defs


def _run_sql_native_task(
    db_key: str,
    kwargs: Mapping[str, Any],
    labels: Mapping[str, Any],
) -> pd.DataFrame:
    result = _compute_test_metrics_sql_native_single(db_key=db_key, **dict(kwargs))
    if not labels:
        return result
    labeled = result.copy()
    conflicts = [column for column in labels if column in labeled.columns]
    if conflicts:
        fields = ", ".join(conflicts)
        raise ValueError(f"Label column(s) conflict with result columns: {fields}.")
    for index, (column, value) in enumerate(labels.items()):
        labeled.insert(index, column, value)
    return labeled


def _sql_string_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"
