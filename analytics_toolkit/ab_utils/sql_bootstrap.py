from __future__ import annotations

import math
import warnings
from typing import TYPE_CHECKING, cast

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from .planning import (
    _quote_sql_identifier,
    _sql_float_expr,
    _sql_native_adjusted_agg_ratio_denominator_expr,
    _sql_native_adjusted_agg_ratio_numerator_expr,
    _sql_native_adjusted_value_expr,
    _sql_native_cutoff_filter,
    _sql_quantile_expr,
    _sql_var_samp_expr,
    _sql_where_clause,
)

_SMALL_SOURCE_MAX_RESAMPLES_PER_QUERY = 250
_TARGET_SAMPLED_ROWS_PER_QUERY = 5_000_000


def _validate_sql_native_bootstrap_batch_options(
    *,
    row_threshold: int,
    resamples_per_query: int,
) -> None:
    if isinstance(row_threshold, bool) or not isinstance(row_threshold, int):
        raise TypeError("bootstrap_large_source_row_threshold must be an integer.")
    if row_threshold <= 0:
        raise ValueError("bootstrap_large_source_row_threshold must be positive.")
    if isinstance(resamples_per_query, bool) or not isinstance(resamples_per_query, int):
        raise TypeError("bootstrap_large_source_resamples_per_query must be an integer.")
    if resamples_per_query <= 0:
        raise ValueError("bootstrap_large_source_resamples_per_query must be positive.")


def _plan_sql_native_bootstrap_batches(
    *,
    row_count: int,
    resamples: int,
    large_source_row_threshold: int,
    large_source_resamples_per_query: int,
) -> list[tuple[int, int]]:
    _validate_sql_native_bootstrap_batch_options(
        row_threshold=large_source_row_threshold,
        resamples_per_query=large_source_resamples_per_query,
    )
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise TypeError("row_count must be an integer.")
    if row_count <= 0:
        raise ValueError("row_count must be positive.")
    if isinstance(resamples, bool) or not isinstance(resamples, int):
        raise TypeError("resamples must be an integer.")
    if resamples <= 0:
        raise ValueError("resamples must be positive.")

    dynamic_batch_size = max(
        1,
        min(
            _SMALL_SOURCE_MAX_RESAMPLES_PER_QUERY,
            _TARGET_SAMPLED_ROWS_PER_QUERY // row_count,
        ),
    )
    if row_count >= large_source_row_threshold:
        batch_size = min(dynamic_batch_size, large_source_resamples_per_query)
    else:
        batch_size = dynamic_batch_size

    batches: list[tuple[int, int]] = []
    start = 1
    while start <= resamples:
        count = min(batch_size, resamples - start + 1)
        batches.append((start, count))
        start += count
    return batches


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
    if backend not in {"gp", "trino", "ch"}:
        raise ValueError(f"Unsupported SQL backend: {backend!r}.")
    if isinstance(resamples, bool) or not isinstance(resamples, int):
        raise TypeError("resamples must be an integer.")
    if resamples <= 0:
        raise ValueError("resamples must be positive.")
    if isinstance(resample_start, bool) or not isinstance(resample_start, int):
        raise TypeError("resample_start must be an integer.")
    if resample_start <= 0:
        raise ValueError("resample_start must be positive.")
    if not comparisons or not metric_definitions:
        raise ValueError("Bootstrap SQL requires metrics and comparisons.")

    seed = 0 if random_state is None else int(random_state)
    resample_end = resample_start + resamples - 1
    raw_metric_selects, sampled_metric_selects = _build_raw_metric_selects(
        backend=backend,
        metric_definitions=metric_definitions,
    )
    group_expr = _quote_sql_identifier(group, backend)
    user_expr = _quote_sql_identifier(user_id, backend)
    where_clause = _sql_where_clause(sql_where)
    generator_ctes = _build_resample_generator_ctes(
        backend=backend,
        start=resample_start,
        end=resample_end,
    )
    sampled_row_index = _build_sampled_row_index_expression(backend=backend, seed=seed)

    ctes = [
        f"""
source AS (
    SELECT
        {user_expr} AS user_id,
        {group_expr} AS group_name,
        {", ".join(raw_metric_selects)}
    FROM {source_sql}
    {where_clause}
)
""".strip(),
        """
indexed_source AS (
    SELECT
        source.*,
        ROW_NUMBER() OVER (PARTITION BY group_name ORDER BY user_id) AS row_index,
        COUNT(*) OVER (PARTITION BY group_name) AS group_size
    FROM source
)
""".strip(),
        """
group_sizes AS (
    SELECT group_name, MAX(group_size) AS group_size
    FROM indexed_source
    GROUP BY group_name
)
""".strip(),
        *generator_ctes,
        f"""
sampled AS (
    SELECT
        draws.resample_id AS resample_id,
        indexed_source.group_name AS group_name,
        {", ".join(sampled_metric_selects)}
    FROM draws
    INNER JOIN indexed_source
        ON indexed_source.group_name = draws.group_name
        AND indexed_source.row_index = {sampled_row_index}
)
""".strip(),
    ]

    metric_stats_names: list[str] = []
    for index, metric_definition in enumerate(metric_definitions):
        prefix = f"bootstrap_metric_{index}"
        metric_ctes, stats_name = _build_metric_bootstrap_ctes(
            backend=backend,
            prefix=prefix,
            metric_definition=metric_definition,
            metric_index=index,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
        ctes.extend(metric_ctes)
        metric_stats_names.append(stats_name)

    stats_union = "\n    UNION ALL\n    ".join(
        f"SELECT * FROM {name}" for name in metric_stats_names
    )
    ctes.append(f"all_group_stats AS (\n    {stats_union}\n)")
    ctes.append(
        _build_observed_statistics_cte(
            backend=backend,
            metric_definitions=metric_definitions,
            comparisons=comparisons,
            observed_statistics=observed_statistics or {},
        )
    )
    ctes.extend(_build_bootstrap_summary_ctes(backend=backend))
    with_sql = ",\n".join(ctes)
    variance_expression = _sql_var_samp_expr("finite_delta_star", backend)
    query_settings = "\nSETTINGS join_use_nulls = 1" if backend == "ch" else ""
    return f"""
/* analytics_toolkit_ab_sql_native_bootstrap seed={seed} backend={backend}
   resamples={resample_start}:{resample_end}; compact summaries only. */
WITH {with_sql}
SELECT
    comparison_statistics.metric_name AS metric_name,
    comparison_statistics.group_1 AS group_1,
    comparison_statistics.group_2 AS group_2,
    COUNT(*) AS requested_resamples,
    SUM(CASE WHEN family_valid_stats.family_valid = 1 THEN 1 ELSE 0 END)
        AS valid_family_resamples,
    COUNT(comparison_statistics.finite_delta_star) AS valid_delta_resamples,
    AVG(comparison_statistics.finite_delta_star) AS delta_mean,
    CASE WHEN COUNT(comparison_statistics.finite_delta_star) >= 2
        THEN {variance_expression} * (COUNT(comparison_statistics.finite_delta_star) - 1)
        ELSE 0
    END AS delta_m2,
    SUM(
        CASE WHEN family_valid_stats.family_valid = 1
            AND comparison_statistics.observed_valid = 1
            AND family_valid_stats.family_max_t >= ABS(
                comparison_statistics.observed_delta
                / comparison_statistics.observed_se
            )
            THEN 1 ELSE 0
        END
    ) AS max_t_exceedances
FROM comparison_statistics
INNER JOIN family_valid_stats
    ON family_valid_stats.metric_name = comparison_statistics.metric_name
    AND family_valid_stats.resample_id = comparison_statistics.resample_id
GROUP BY
    comparison_statistics.metric_name,
    comparison_statistics.group_1,
    comparison_statistics.group_2
ORDER BY
    comparison_statistics.metric_name,
    comparison_statistics.group_1,
    comparison_statistics.group_2{query_settings}
""".strip()


def _build_raw_metric_selects(
    *,
    backend: str,
    metric_definitions: Sequence[dict[str, object]],
) -> tuple[list[str], list[str]]:
    raw_selects: list[str] = []
    sampled_selects: list[str] = []
    for index, metric_definition in enumerate(metric_definitions):
        if metric_definition["kind"] == "mean":
            alias = f"metric_{index}_value"
            expression = _sql_float_expr(
                _quote_sql_identifier(str(metric_definition["column"]), backend),
                backend,
            )
            raw_selects.append(f"{expression} AS {alias}")
            sampled_selects.append(f"indexed_source.{alias} AS {alias}")
            continue

        ratio_spec = dict(cast("dict[str, object]", metric_definition["ratio_spec"]))
        numerator_alias = f"metric_{index}_numerator"
        denominator_alias = f"metric_{index}_denominator"
        numerator = _sql_float_expr(
            _quote_sql_identifier(str(ratio_spec["numerator"]), backend),
            backend,
        )
        denominator = _sql_float_expr(
            _quote_sql_identifier(str(ratio_spec["denominator"]), backend),
            backend,
        )
        raw_selects.extend(
            [
                f"{numerator} AS {numerator_alias}",
                f"{denominator} AS {denominator_alias}",
            ]
        )
        sampled_selects.extend(
            [
                f"indexed_source.{numerator_alias} AS {numerator_alias}",
                f"indexed_source.{denominator_alias} AS {denominator_alias}",
            ]
        )
    return raw_selects, sampled_selects


def _build_resample_generator_ctes(*, backend: str, start: int, end: int) -> list[str]:
    count = end - start + 1
    if backend == "gp":
        resample_ids = f"""
resample_ids AS (
    SELECT generated.resample_id AS resample_id
    FROM generate_series({start}, {end}) AS generated(resample_id)
)
""".strip()
        draws = """
draws AS (
    SELECT
        resample_ids.resample_id,
        group_sizes.group_name,
        group_sizes.group_size,
        generated.draw_index
    FROM resample_ids
    CROSS JOIN group_sizes
    CROSS JOIN LATERAL generate_series(1, group_sizes.group_size)
        AS generated(draw_index)
)
""".strip()
    elif backend == "trino":
        resample_ids = f"""
resample_ids AS (
    SELECT generated.resample_id AS resample_id
    FROM UNNEST(sequence({start}, {end})) AS generated(resample_id)
)
""".strip()
        draws = """
draws AS (
    SELECT
        resample_ids.resample_id,
        group_sizes.group_name,
        group_sizes.group_size,
        generated.draw_index
    FROM resample_ids
    CROSS JOIN group_sizes
    CROSS JOIN UNNEST(sequence(CAST(1 AS BIGINT), group_sizes.group_size))
        AS generated(draw_index)
)
""".strip()
    else:
        resample_ids = f"""
resample_ids AS (
    SELECT {start} + number AS resample_id
    FROM numbers({count})
)
""".strip()
        draws = """
draws AS (
    SELECT
        resample_ids.resample_id,
        group_sizes.group_name,
        group_sizes.group_size,
        draw_index
    FROM resample_ids
    CROSS JOIN group_sizes
    ARRAY JOIN range(toUInt64(1), toUInt64(group_sizes.group_size) + 1) AS draw_index
)
""".strip()
    return [resample_ids, draws]


def _build_sampled_row_index_expression(*, backend: str, seed: int) -> str:
    payload = (
        f"CONCAT('{seed}:', CAST(draws.resample_id AS "
        + ("TEXT" if backend == "gp" else "VARCHAR")
        + "), ':', CAST(draws.group_name AS "
        + ("TEXT" if backend == "gp" else "VARCHAR")
        + "), ':', CAST(draws.draw_index AS "
        + ("TEXT" if backend == "gp" else "VARCHAR")
        + "))"
    )
    if backend == "gp":
        return (
            "MOD((('x' || SUBSTRING(MD5("
            f"{payload}"
            "), 1, 8))::bit(32)::bigint), draws.group_size) + 1"
        )
    if backend == "trino":
        return (
            "MOD(BITWISE_AND(FROM_BIG_ENDIAN_64(XXHASH64(TO_UTF8("
            f"{payload}"
            "))), 9223372036854775807), draws.group_size) + 1"
        )
    clickhouse_payload = (
        f"concat('{seed}:', toString(draws.resample_id), ':', "
        "toString(draws.group_name), ':', toString(draws.draw_index))"
    )
    return f"modulo(cityHash64({clickhouse_payload}), toUInt64(draws.group_size)) + 1"


def _build_metric_bootstrap_ctes(
    *,
    backend: str,
    prefix: str,
    metric_definition: dict[str, object],
    metric_index: int,
    outliers_quantile: float,
    outliers_policy: str,
) -> tuple[list[str], str]:
    if metric_definition["kind"] == "mean":
        return _build_value_bootstrap_ctes(
            backend=backend,
            prefix=prefix,
            metric_name=str(metric_definition["metric_key"]),
            value_expression=f"metric_{metric_index}_value",
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )

    ratio_spec = dict(cast("dict[str, object]", metric_definition["ratio_spec"]))
    numerator = f"metric_{metric_index}_numerator"
    denominator = f"metric_{metric_index}_denominator"
    if ratio_spec["level"] == "user":
        value = (
            "CASE WHEN "
            f"{numerator} IS NOT NULL AND {denominator} IS NOT NULL "
            f"AND {denominator} > 0 THEN {numerator} / {denominator} "
            "ELSE NULL END"
        )
        return _build_value_bootstrap_ctes(
            backend=backend,
            prefix=prefix,
            metric_name=str(metric_definition["metric_key"]),
            value_expression=value,
            outliers_quantile=outliers_quantile,
            outliers_policy=outliers_policy,
        )
    return _build_agg_ratio_bootstrap_ctes(
        backend=backend,
        prefix=prefix,
        metric_name=str(metric_definition["metric_key"]),
        numerator_expression=numerator,
        denominator_expression=denominator,
        outliers_quantile=outliers_quantile,
        outliers_policy=outliers_policy,
    )


def _build_value_bootstrap_ctes(
    *,
    backend: str,
    prefix: str,
    metric_name: str,
    value_expression: str,
    outliers_quantile: float,
    outliers_policy: str,
) -> tuple[list[str], str]:
    raw_name = f"{prefix}_raw"
    cutoff_name = f"{prefix}_cutoff"
    prepared_name = f"{prefix}_prepared"
    stats_name = f"{prefix}_stats"
    cutoff_filter = _sql_native_cutoff_filter("value", outliers_policy)
    adjusted_value = _sql_native_adjusted_value_expr(
        value_expression="raw.value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    variance = _sql_var_samp_expr("metric_value", backend)
    return (
        [
            f"""
{raw_name} AS (
    SELECT resample_id, group_name, {value_expression} AS value
    FROM sampled
)
""".strip(),
            f"""
{cutoff_name} AS (
    SELECT
        resample_id,
        {_sql_quantile_expr("value", outliers_quantile, backend)} AS cutoff
    FROM {raw_name}
    WHERE value IS NOT NULL{cutoff_filter}
    GROUP BY resample_id
)
""".strip(),
            f"""
{prepared_name} AS (
    SELECT
        raw.resample_id,
        raw.group_name,
        {adjusted_value} AS metric_value
    FROM {raw_name} AS raw
    LEFT JOIN {cutoff_name} AS cutoff
        ON cutoff.resample_id = raw.resample_id
)
""".strip(),
            f"""
{stats_name} AS (
    SELECT
        {_sql_string_literal(metric_name)} AS metric_name,
        resample_id,
        group_name,
        AVG(metric_value) AS metric_value,
        CASE WHEN COUNT(metric_value) >= 2
            THEN {variance} / COUNT(metric_value)
            ELSE NULL
        END AS variance_component
    FROM {prepared_name}
    GROUP BY resample_id, group_name
)
""".strip(),
        ],
        stats_name,
    )


def _build_agg_ratio_bootstrap_ctes(
    *,
    backend: str,
    prefix: str,
    metric_name: str,
    numerator_expression: str,
    denominator_expression: str,
    outliers_quantile: float,
    outliers_policy: str,
) -> tuple[list[str], str]:
    raw_name = f"{prefix}_raw"
    cutoff_name = f"{prefix}_cutoff"
    prepared_name = f"{prefix}_prepared"
    summary_name = f"{prefix}_summary"
    linearized_name = f"{prefix}_linearized"
    stats_name = f"{prefix}_stats"
    ratio_value = (
        "CASE WHEN "
        f"{numerator_expression} IS NOT NULL AND {denominator_expression} IS NOT NULL "
        f"AND {denominator_expression} > 0 "
        f"THEN {numerator_expression} / {denominator_expression} ELSE NULL END"
    )
    cutoff_filter = _sql_native_cutoff_filter("ratio_value", outliers_policy)
    adjusted_numerator = _sql_native_adjusted_agg_ratio_numerator_expr(
        numerator_expression="raw.numerator",
        denominator_expression="raw.denominator",
        ratio_expression="raw.ratio_value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    adjusted_denominator = _sql_native_adjusted_agg_ratio_denominator_expr(
        denominator_expression="raw.denominator",
        ratio_expression="raw.ratio_value",
        cutoff_expression="cutoff.cutoff",
        outliers_policy=outliers_policy,
    )
    variance = _sql_var_samp_expr("linearized.metric_value", backend)
    return (
        [
            f"""
{raw_name} AS (
    SELECT
        resample_id,
        group_name,
        {numerator_expression} AS numerator,
        {denominator_expression} AS denominator,
        {ratio_value} AS ratio_value
    FROM sampled
)
""".strip(),
            f"""
{cutoff_name} AS (
    SELECT
        resample_id,
        {_sql_quantile_expr("ratio_value", outliers_quantile, backend)} AS cutoff
    FROM {raw_name}
    WHERE ratio_value IS NOT NULL{cutoff_filter}
    GROUP BY resample_id
)
""".strip(),
            f"""
{prepared_name} AS (
    SELECT
        raw.resample_id,
        raw.group_name,
        {adjusted_numerator} AS numerator,
        {adjusted_denominator} AS denominator
    FROM {raw_name} AS raw
    LEFT JOIN {cutoff_name} AS cutoff
        ON cutoff.resample_id = raw.resample_id
)
""".strip(),
            f"""
{summary_name} AS (
    SELECT
        resample_id,
        group_name,
        COUNT(*) AS n,
        AVG(denominator) AS denominator_mean,
        CASE WHEN SUM(denominator) > 0
            THEN SUM(numerator) / SUM(denominator)
            ELSE NULL
        END AS ratio
    FROM {prepared_name}
    WHERE numerator IS NOT NULL AND denominator IS NOT NULL
    GROUP BY resample_id, group_name
)
""".strip(),
            f"""
{linearized_name} AS (
    SELECT
        prepared.resample_id,
        prepared.group_name,
        prepared.numerator - summary.ratio * prepared.denominator AS metric_value
    FROM {prepared_name} AS prepared
    INNER JOIN {summary_name} AS summary
        ON summary.resample_id = prepared.resample_id
        AND summary.group_name = prepared.group_name
    WHERE prepared.numerator IS NOT NULL
        AND prepared.denominator IS NOT NULL
        AND summary.ratio IS NOT NULL
)
""".strip(),
            f"""
{stats_name} AS (
    SELECT
        {_sql_string_literal(metric_name)} AS metric_name,
        summary.resample_id,
        summary.group_name,
        summary.ratio AS metric_value,
        CASE WHEN summary.n >= 2
            AND summary.denominator_mean > 0
            AND summary.ratio IS NOT NULL
            THEN {variance}
                / (summary.n * summary.denominator_mean * summary.denominator_mean)
            ELSE NULL
        END AS variance_component
    FROM {summary_name} AS summary
    LEFT JOIN {linearized_name} AS linearized
        ON linearized.resample_id = summary.resample_id
        AND linearized.group_name = summary.group_name
    GROUP BY
        summary.resample_id,
        summary.group_name,
        summary.n,
        summary.denominator_mean,
        summary.ratio
)
""".strip(),
        ],
        stats_name,
    )


def _build_observed_statistics_cte(
    *,
    backend: str,
    metric_definitions: Sequence[dict[str, object]],
    comparisons: Sequence[tuple[str, str]],
    observed_statistics: Mapping[tuple[str, str, str], tuple[float, float]],
) -> str:
    rows: list[str] = []
    for metric_definition in metric_definitions:
        metric_name = str(metric_definition["metric_key"])
        for test_group, baseline_group in comparisons:
            delta, standard_error = observed_statistics.get(
                (metric_name, test_group, baseline_group),
                (math.nan, math.nan),
            )
            rows.append(
                "SELECT "
                f"{_sql_string_literal(metric_name)} AS metric_name, "
                f"{_sql_string_literal(test_group)} AS group_1, "
                f"{_sql_string_literal(baseline_group)} AS group_2, "
                f"{_sql_float_literal(delta, backend)} AS observed_delta, "
                f"{_sql_float_literal(standard_error, backend)} AS observed_se"
            )
    return "observed_statistics AS (\n    " + "\n    UNION ALL\n    ".join(rows) + "\n)"


def _build_bootstrap_summary_ctes(*, backend: str) -> list[str]:
    observed_delta_finite = _sql_is_finite("observed_delta", backend)
    observed_se_finite = _sql_is_finite("observed_se", backend)
    delta_finite = _sql_is_finite("delta_star", backend)
    se_finite = _sql_is_finite("se_star", backend)
    t_finite = _sql_is_finite("t_star", backend)
    return [
        """
resample_grid AS (
    SELECT
        resample_ids.resample_id,
        observed_statistics.metric_name,
        observed_statistics.group_1,
        observed_statistics.group_2,
        observed_statistics.observed_delta,
        observed_statistics.observed_se
    FROM resample_ids
    CROSS JOIN observed_statistics
)
""".strip(),
        """
comparison_values AS (
    SELECT
        grid.*,
        CASE WHEN test_stats.metric_value IS NOT NULL
            AND control_stats.metric_value IS NOT NULL
            THEN test_stats.metric_value - control_stats.metric_value
            ELSE NULL
        END AS delta_star,
        CASE WHEN test_stats.variance_component IS NOT NULL
            AND control_stats.variance_component IS NOT NULL
            AND test_stats.variance_component + control_stats.variance_component > 0
            THEN SQRT(test_stats.variance_component + control_stats.variance_component)
            ELSE NULL
        END AS se_star
    FROM resample_grid AS grid
    LEFT JOIN all_group_stats AS test_stats
        ON test_stats.metric_name = grid.metric_name
        AND test_stats.resample_id = grid.resample_id
        AND test_stats.group_name = grid.group_1
    LEFT JOIN all_group_stats AS control_stats
        ON control_stats.metric_name = grid.metric_name
        AND control_stats.resample_id = grid.resample_id
        AND control_stats.group_name = grid.group_2
)
""".strip(),
        """
comparison_statistics AS (
    SELECT
        comparison_values.*,
        CASE WHEN {observed_delta_finite}
            AND {observed_se_finite}
            AND observed_se > 0 THEN 1 ELSE 0 END
            AS observed_valid,
        CASE WHEN {delta_finite}
            AND {se_finite}
            AND {observed_delta_finite}
            AND se_star > 0
            THEN (delta_star - observed_delta) / se_star
            ELSE NULL
        END AS t_star,
        CASE WHEN {delta_finite} THEN delta_star ELSE NULL END AS finite_delta_star
    FROM comparison_values
)
""".format(
            observed_delta_finite=observed_delta_finite,
            observed_se_finite=observed_se_finite,
            delta_finite=delta_finite,
            se_finite=se_finite,
        ).strip(),
        """
expected_family_sizes AS (
    SELECT
        metric_name,
        SUM(CASE WHEN {observed_delta_finite}
            AND {observed_se_finite}
            AND observed_se > 0 THEN 1 ELSE 0 END)
            AS expected_comparisons
    FROM observed_statistics
    GROUP BY metric_name
)
""".format(
            observed_delta_finite=observed_delta_finite,
            observed_se_finite=observed_se_finite,
        ).strip(),
        """
family_statistics AS (
    SELECT
        metric_name,
        resample_id,
        SUM(CASE WHEN observed_valid = 1 AND {t_finite} THEN 1 ELSE 0 END)
            AS finite_comparisons,
        MAX(CASE WHEN observed_valid = 1 AND {t_finite}
            THEN ABS(t_star) ELSE NULL END) AS family_max_t
    FROM comparison_statistics
    GROUP BY metric_name, resample_id
)
""".format(t_finite=t_finite).strip(),
        """
family_valid_stats AS (
    SELECT
        family.metric_name,
        family.resample_id,
        family.family_max_t,
        CASE WHEN expected.expected_comparisons > 0
            AND family.finite_comparisons = expected.expected_comparisons
            THEN 1 ELSE 0
        END AS family_valid
    FROM family_statistics AS family
    INNER JOIN expected_family_sizes AS expected
        ON expected.metric_name = family.metric_name
)
""".strip(),
    ]


def _reduce_sql_native_bootstrap_batches(
    *,
    batches: Sequence[tuple[int, pd.DataFrame]],
    observed_statistics: Mapping[tuple[str, str, str], tuple[float, float]],
) -> pd.DataFrame:
    if len(batches) == 1 and {
        "metric_name",
        "group_1",
        "group_2",
        "se_bootstrap",
        "bootstrap_adj_p",
    }.issubset(batches[0][1].columns):
        return batches[0][1][
            ["metric_name", "group_1", "group_2", "se_bootstrap", "bootstrap_adj_p"]
        ].copy()

    required_columns = {
        "metric_name",
        "group_1",
        "group_2",
        "requested_resamples",
        "valid_family_resamples",
        "valid_delta_resamples",
        "delta_mean",
        "delta_m2",
        "max_t_exceedances",
    }
    accumulators: dict[tuple[str, str, str], dict[str, float]] = {
        key: {
            "requested": 0.0,
            "family_n": 0.0,
            "delta_n": 0.0,
            "delta_mean": 0.0,
            "delta_m2": 0.0,
            "exceedances": 0.0,
        }
        for key in observed_statistics
    }
    for batch_size, frame in batches:
        missing = sorted(required_columns - set(frame.columns))
        if missing:
            raise ValueError(
                "SQL bootstrap summary is missing column(s): " + ", ".join(missing) + "."
            )
        rows_by_key: dict[tuple[str, str, str], pd.Series] = {}
        for _, row in frame.iterrows():
            key = (str(row["metric_name"]), str(row["group_1"]), str(row["group_2"]))
            if key in rows_by_key:
                raise ValueError(f"SQL bootstrap summary returned duplicate key {key!r}.")
            rows_by_key[key] = row

        unexpected = sorted(set(rows_by_key) - set(accumulators))
        missing_keys = sorted(set(accumulators) - set(rows_by_key))
        if unexpected:
            raise ValueError(f"SQL bootstrap summary returned unexpected key {unexpected[0]!r}.")
        if missing_keys:
            raise ValueError(f"SQL bootstrap summary is missing expected key {missing_keys[0]!r}.")

        for key, accumulator in accumulators.items():
            accumulator["requested"] += batch_size
            row = rows_by_key[key]
            requested = _summary_nonnegative_int(row.get("requested_resamples"))
            family_n = _summary_nonnegative_int(row.get("valid_family_resamples"))
            delta_n = _summary_nonnegative_int(row.get("valid_delta_resamples"))
            exceedances = _summary_nonnegative_int(row.get("max_t_exceedances"))
            if requested != batch_size:
                raise ValueError("SQL bootstrap summary requested_resamples is inconsistent.")
            if family_n > batch_size or delta_n > batch_size or exceedances > family_n:
                raise ValueError("SQL bootstrap summary counts are inconsistent.")
            accumulator["family_n"] += family_n
            accumulator["exceedances"] += exceedances
            if delta_n == 0:
                continue
            batch_mean = _summary_finite_float(row.get("delta_mean"), "delta_mean")
            batch_m2 = _summary_finite_float(row.get("delta_m2"), "delta_m2")
            if batch_m2 < 0:
                raise ValueError("SQL bootstrap summary delta_m2 must be non-negative.")
            _merge_bootstrap_moments(
                accumulator=accumulator,
                batch_n=delta_n,
                batch_mean=batch_mean,
                batch_m2=batch_m2,
            )

    _warn_discarded_sql_native_bootstrap_replicates(
        accumulators,
        observed_statistics=observed_statistics,
    )
    rows: list[dict[str, object]] = []
    for key, accumulator in accumulators.items():
        metric_name, test_group, baseline_group = key
        delta_n = int(accumulator["delta_n"])
        family_n = int(accumulator["family_n"])
        observed_delta, observed_se = observed_statistics[key]
        se_bootstrap = (
            math.sqrt(accumulator["delta_m2"] / (delta_n - 1)) if delta_n >= 2 else math.nan
        )
        observed_valid = (
            math.isfinite(observed_delta) and math.isfinite(observed_se) and observed_se > 0
        )
        adjusted_p = (
            (1 + accumulator["exceedances"]) / (1 + family_n)
            if observed_valid and family_n > 0
            else math.nan
        )
        rows.append(
            {
                "metric_name": metric_name,
                "group_1": test_group,
                "group_2": baseline_group,
                "se_bootstrap": se_bootstrap,
                "bootstrap_adj_p": adjusted_p,
            }
        )
    return pd.DataFrame(rows)


def _merge_bootstrap_moments(
    *,
    accumulator: dict[str, float],
    batch_n: int,
    batch_mean: float,
    batch_m2: float,
) -> None:
    current_n = int(accumulator["delta_n"])
    if current_n == 0:
        accumulator["delta_n"] = float(batch_n)
        accumulator["delta_mean"] = batch_mean
        accumulator["delta_m2"] = batch_m2
        return
    combined_n = current_n + batch_n
    mean_delta = batch_mean - accumulator["delta_mean"]
    accumulator["delta_mean"] += mean_delta * batch_n / combined_n
    accumulator["delta_m2"] += batch_m2 + mean_delta * mean_delta * current_n * batch_n / combined_n
    accumulator["delta_n"] = float(combined_n)


def _warn_discarded_sql_native_bootstrap_replicates(
    accumulators: Mapping[tuple[str, str, str], Mapping[str, float]],
    *,
    observed_statistics: Mapping[tuple[str, str, str], tuple[float, float]],
) -> None:
    eligible_metrics = {
        key[0]
        for key, (delta, standard_error) in observed_statistics.items()
        if math.isfinite(delta) and math.isfinite(standard_error) and standard_error > 0
    }
    metric_counts: dict[str, tuple[int, int]] = {}
    for key, accumulator in accumulators.items():
        metric_name = key[0]
        if metric_name not in eligible_metrics:
            continue
        requested = int(accumulator["requested"])
        valid = int(accumulator["family_n"])
        previous = metric_counts.get(metric_name)
        if previous is None:
            metric_counts[metric_name] = (requested, valid)
        else:
            metric_counts[metric_name] = (max(previous[0], requested), min(previous[1], valid))
    for metric_name, (requested, valid) in metric_counts.items():
        if valid < requested:
            warnings.warn(
                f"SQL bootstrap discarded {requested - valid} of {requested} family "
                f"replicates for metric {metric_name!r} because at least one "
                "studentized comparison was not finite.",
                RuntimeWarning,
                stacklevel=3,
            )


def _summary_nonnegative_int(value: object) -> int:
    if value is None or pd.isna(value):
        raise ValueError("SQL bootstrap summary count must not be null.")
    normalized = cast("str | int | float", value)
    number = int(normalized)
    if isinstance(normalized, float) and not normalized.is_integer():
        raise ValueError("SQL bootstrap summary count must be an integer.")
    if number < 0:
        raise ValueError("SQL bootstrap summary count must be non-negative.")
    return number


def _summary_finite_float(value: object, field: str) -> float:
    if value is None or pd.isna(value):
        raise ValueError(f"SQL bootstrap summary {field} must be finite.")
    number = float(cast("str | int | float", value))
    if not math.isfinite(number):
        raise ValueError(f"SQL bootstrap summary {field} must be finite.")
    return number


def _sql_float_literal(value: float, backend: str) -> str:
    if math.isfinite(value):
        return format(value, ".17g")
    if backend == "gp":
        return "CAST(NULL AS DOUBLE PRECISION)"
    if backend == "ch":
        return "CAST(NULL AS Nullable(Float64))"
    return "CAST(NULL AS DOUBLE)"


def _sql_is_finite(expression: str, backend: str) -> str:
    if backend == "gp":
        return (
            f"{expression} IS NOT NULL "
            f"AND {expression} <> CAST('NaN' AS DOUBLE PRECISION) "
            f"AND {expression} <> CAST('Infinity' AS DOUBLE PRECISION) "
            f"AND {expression} <> CAST('-Infinity' AS DOUBLE PRECISION)"
        )
    if backend == "ch":
        return f"{expression} IS NOT NULL AND isFinite({expression})"
    return f"{expression} IS NOT NULL AND is_finite({expression})"


def _sql_string_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"
