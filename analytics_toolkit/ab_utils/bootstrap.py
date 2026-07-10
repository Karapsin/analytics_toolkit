from __future__ import annotations

import math
import warnings
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import numpy as np
import pandas as pd
from tqdm import tqdm

from .outliers import (
    _apply_outliers_to_agg_ratio_components,
    _apply_outliers_to_values,
    _compute_outlier_cutoff,
)
from .ratio import (
    _build_ratio_frame_from_arrays,
    _build_ratio_valid_mask_from_arrays,
    _compute_agg_ratio_diff_standard_error,
    _compute_agg_ratio_group_stats_arrays,
)
from .stats import (
    _both_present,
    _compute_group_diff_standard_error,
    _compute_sample_variance,
    _compute_studentized_statistic,
    _get_numeric_metric_series,
)


def _apply_multiple_comparisons_adjustment(
    rows: list[dict[str, object]],
    df: pd.DataFrame,
    group_column: str,
    metric_definitions: list[dict[str, object]],
    comparisons: list[tuple[str, str]],
    resamples: int,
    random_state: int | None,
    n_jobs: int,
    show_progress: bool,
    outlier_contexts: dict[str, dict[str, object]] | None = None,
) -> None:
    if not rows:
        return

    bootstrap_context = _prepare_bootstrap_context(
        df=df,
        group_column=group_column,
        metric_definitions=metric_definitions,
        comparisons=comparisons,
        outlier_contexts=outlier_contexts,
    )
    comparison_index_by_key = {
        (test_group, baseline_group): index
        for index, (test_group, baseline_group) in enumerate(comparisons)
    }
    _replace_observed_statistics_from_rows(
        bootstrap_context=bootstrap_context,
        rows=rows,
        comparison_index_by_key=comparison_index_by_key,
    )
    family_max_statistics, delta_abs_by_comparison = _compute_bootstrap_statistics(
        bootstrap_context=bootstrap_context,
        resamples=resamples,
        random_state=random_state,
        n_jobs=n_jobs,
        show_progress=show_progress,
    )
    _warn_about_discarded_family_resamples(
        bootstrap_context=bootstrap_context,
        family_max_statistics=family_max_statistics,
    )

    for row in rows:
        observed_stat = row.get("_test_stat")
        metric_key = str(row.get("_metric_key"))
        comparison_key = row.get("_comparison_key")
        comparison_index = comparison_index_by_key.get(comparison_key)
        if comparison_index is not None:
            bootstrap_deltas = [
                value
                for value in delta_abs_by_comparison.get((metric_key, comparison_index), [])
                if math.isfinite(value)
            ]
            if len(bootstrap_deltas) >= 2:
                row["s.e. bootstrap"] = float(np.std(bootstrap_deltas, ddof=1))
            else:
                row["s.e. bootstrap"] = math.nan
        else:
            row["s.e. bootstrap"] = math.nan

        if not _is_finite_number(observed_stat):
            row["bootstrap_adj_p"] = math.nan
            continue

        bootstrap_stats = [
            value for value in family_max_statistics.get(metric_key, []) if math.isfinite(value)
        ]
        if not bootstrap_stats:
            row["bootstrap_adj_p"] = math.nan
            continue

        observed_abs_stat = abs(float(observed_stat))
        exceedances = sum(value >= observed_abs_stat for value in bootstrap_stats)
        row["bootstrap_adj_p"] = (1 + exceedances) / (1 + len(bootstrap_stats))


def _replace_observed_statistics_from_rows(
    bootstrap_context: dict[str, object],
    rows: list[dict[str, object]],
    comparison_index_by_key: dict[tuple[str, str], int],
) -> None:
    metric_context_by_key = {
        str(metric_context["metric_key"]): metric_context
        for metric_context in list(bootstrap_context["metric_contexts"])
    }
    comparison_count = len(list(bootstrap_context["comparisons"]))
    observed_deltas = {
        metric_key: [math.nan] * comparison_count for metric_key in metric_context_by_key
    }
    observed_valid = {metric_key: [] for metric_key in metric_context_by_key}

    for row in rows:
        metric_key = str(row.get("_metric_key"))
        comparison_index = comparison_index_by_key.get(row.get("_comparison_key"))
        if metric_key not in metric_context_by_key or comparison_index is None:
            continue
        delta = row.get("delta_abs")
        statistic = row.get("_test_stat")
        standard_error = row.get("s.e.")
        if _is_finite_number(delta):
            observed_deltas[metric_key][comparison_index] = float(delta)
        if (
            _is_finite_number(delta)
            and _is_finite_number(statistic)
            and _is_finite_number(standard_error)
            and float(standard_error) > 0
        ):
            observed_valid[metric_key].append(comparison_index)

    for metric_key, metric_context in metric_context_by_key.items():
        metric_context["observed_deltas"] = observed_deltas[metric_key]
        metric_context["observed_valid_comparisons"] = observed_valid[metric_key]


def _warn_about_discarded_family_resamples(
    bootstrap_context: dict[str, object],
    family_max_statistics: dict[str, list[float]],
) -> None:
    for metric_context in list(bootstrap_context["metric_contexts"]):
        observed_valid = list(metric_context.get("observed_valid_comparisons", []))
        if not observed_valid:
            continue
        metric_key = str(metric_context["metric_key"])
        statistics = family_max_statistics.get(metric_key, [])
        discarded = sum(not math.isfinite(value) for value in statistics)
        if discarded:
            warnings.warn(
                f"Bootstrap discarded {discarded} of {len(statistics)} resamples for metric "
                f"'{metric_key}' because at least one observed-valid comparison had a "
                "non-finite studentized statistic.",
                RuntimeWarning,
                stacklevel=3,
            )


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float, np.integer, np.floating)) and math.isfinite(float(value))


def _prepare_bootstrap_context(
    df: pd.DataFrame,
    group_column: str,
    metric_definitions: list[dict[str, object]],
    comparisons: list[tuple[str, str]],
    outlier_contexts: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    group_values = df[group_column].to_numpy()
    group_names = list(dict.fromkeys(group_values.tolist()))
    group_code_by_name = {name: code for code, name in enumerate(group_names)}
    group_codes = np.array([group_code_by_name[value] for value in group_values], dtype=np.int16)

    metric_contexts: list[dict[str, object]] = []
    for metric_definition in metric_definitions:
        metric_key = str(metric_definition["metric_key"])
        outlier_context = metric_definition.get("_outlier_context")
        if outlier_context is None:
            outlier_context = (outlier_contexts or {}).get(metric_key)
        if outlier_context is not None:
            outlier_context = dict(outlier_context)

        if metric_definition["kind"] == "mean":
            values = _get_numeric_metric_series(df, str(metric_definition["column"]))
            metric_contexts.append(
                {
                    "kind": "mean",
                    "metric_key": metric_key,
                    "values": values.to_numpy(dtype=float),
                    "outlier_context": outlier_context,
                }
            )
            continue

        ratio_spec = dict(metric_definition["ratio_spec"])
        numerator = _get_numeric_metric_series(df, ratio_spec["numerator"])
        denominator = _get_numeric_metric_series(df, ratio_spec["denominator"])
        numerator_array = numerator.to_numpy(dtype=float)
        denominator_array = denominator.to_numpy(dtype=float)
        ratio_context: dict[str, object] = {
            "kind": "ratio",
            "metric_key": metric_key,
            "level": ratio_spec["level"],
            "numerator": numerator_array,
            "denominator": denominator_array,
            "outlier_context": outlier_context,
        }
        if ratio_spec["level"] == "user":
            valid_mask = _build_ratio_valid_mask_from_arrays(
                numerator=numerator_array,
                denominator=denominator_array,
                level="user",
            )
            ratio_values = np.full(df.shape[0], np.nan, dtype=float)
            ratio_values[valid_mask] = numerator_array[valid_mask] / denominator_array[valid_mask]
            ratio_context["values"] = ratio_values
        metric_contexts.append(ratio_context)

    context: dict[str, object] = {
        "group_codes": group_codes,
        "group_indices": [
            np.flatnonzero(group_codes == group_code) for group_code in range(len(group_names))
        ],
        "metric_contexts": metric_contexts,
        "comparisons": [
            (group_code_by_name[test_group], group_code_by_name[baseline_group])
            for test_group, baseline_group in comparisons
        ],
    }
    _set_observed_statistics_from_context(context)
    return context


def _set_observed_statistics_from_context(bootstrap_context: dict[str, object]) -> None:
    group_codes = np.asarray(bootstrap_context["group_codes"], dtype=np.int16)
    sample_indices = np.arange(group_codes.shape[0], dtype=np.int64)
    comparisons = list(bootstrap_context["comparisons"])
    for metric_context in list(bootstrap_context["metric_contexts"]):
        sampled_metric = _prepare_sampled_metric_context(
            metric_context=metric_context,
            sample_indices=sample_indices,
            recompute_outliers=False,
        )
        observed_deltas: list[float] = []
        observed_valid_comparisons: list[int] = []
        for comparison_index, (test_group_code, baseline_group_code) in enumerate(comparisons):
            delta, standard_error = _compute_metric_delta_and_standard_error(
                sampled_metric=sampled_metric,
                sampled_group_codes=group_codes,
                baseline_group_code=baseline_group_code,
                test_group_code=test_group_code,
            )
            observed_deltas.append(delta)
            statistic = _compute_studentized_statistic(delta, standard_error)
            if math.isfinite(statistic):
                observed_valid_comparisons.append(comparison_index)
        metric_context["observed_deltas"] = observed_deltas
        metric_context["observed_valid_comparisons"] = observed_valid_comparisons


def _compute_bootstrap_family_max_statistics(
    bootstrap_context: dict[str, object],
    resamples: int,
    random_state: int | None,
    n_jobs: int,
    show_progress: bool,
) -> dict[str, list[float]]:
    family_max_statistics, _ = _compute_bootstrap_statistics(
        bootstrap_context=bootstrap_context,
        resamples=resamples,
        random_state=random_state,
        n_jobs=n_jobs,
        show_progress=show_progress,
    )
    return family_max_statistics


def _compute_bootstrap_statistics(
    bootstrap_context: dict[str, object],
    resamples: int,
    random_state: int | None,
    n_jobs: int,
    show_progress: bool,
) -> tuple[dict[str, list[float]], dict[tuple[str, int], list[float]]]:
    metric_keys = [
        str(metric_context["metric_key"])
        for metric_context in list(bootstrap_context["metric_contexts"])
    ]
    family_max_statistics: dict[str, list[float]] = {metric_key: [] for metric_key in metric_keys}
    comparison_count = len(list(bootstrap_context["comparisons"]))
    delta_abs_by_comparison: dict[tuple[str, int], list[float]] = {
        (metric_key, comparison_index): []
        for metric_key in metric_keys
        for comparison_index in range(comparison_count)
    }

    batch_sizes = _split_resamples_into_batches(resamples, n_jobs=n_jobs)
    if not batch_sizes:
        return family_max_statistics, delta_abs_by_comparison

    replicate_seeds = np.random.SeedSequence(random_state).spawn(resamples)
    seed_batches = _split_seed_sequences_into_batches(replicate_seeds, batch_sizes)
    if n_jobs == 1 or len(batch_sizes) == 1:
        batch_result = _compute_bootstrap_statistics_batch(
            bootstrap_context=bootstrap_context,
            resamples=batch_sizes[0],
            rng_or_seed=seed_batches[0],
            progress_position=0 if show_progress else None,
        )
        _extend_bootstrap_statistics(
            family_max_statistics=family_max_statistics,
            delta_abs_by_comparison=delta_abs_by_comparison,
            batch_result=batch_result,
        )
        return family_max_statistics, delta_abs_by_comparison

    try:
        batch_results = _compute_bootstrap_statistics_in_executor(
            executor_cls=ProcessPoolExecutor,
            bootstrap_context=bootstrap_context,
            batch_sizes=batch_sizes,
            child_sequences=seed_batches,
            n_jobs=n_jobs,
            show_progress=show_progress,
        )
    except (NotImplementedError, PermissionError, OSError):
        batch_results = _compute_bootstrap_statistics_in_executor(
            executor_cls=ThreadPoolExecutor,
            bootstrap_context=bootstrap_context,
            batch_sizes=batch_sizes,
            child_sequences=seed_batches,
            n_jobs=n_jobs,
            show_progress=show_progress,
        )

    for batch_result in batch_results:
        _extend_bootstrap_statistics(
            family_max_statistics=family_max_statistics,
            delta_abs_by_comparison=delta_abs_by_comparison,
            batch_result=batch_result,
        )

    return family_max_statistics, delta_abs_by_comparison


def _split_seed_sequences_into_batches(
    replicate_seeds: list[np.random.SeedSequence],
    batch_sizes: list[int],
) -> list[list[np.random.SeedSequence]]:
    batches: list[list[np.random.SeedSequence]] = []
    start = 0
    for batch_size in batch_sizes:
        stop = start + batch_size
        batches.append(replicate_seeds[start:stop])
        start = stop
    return batches


def _extend_bootstrap_statistics(
    family_max_statistics: dict[str, list[float]],
    delta_abs_by_comparison: dict[tuple[str, int], list[float]],
    batch_result: tuple[dict[str, list[float]], dict[tuple[str, int], list[float]]],
) -> None:
    batch_family_max_statistics, batch_delta_abs_by_comparison = batch_result
    for metric_key, values in batch_family_max_statistics.items():
        family_max_statistics[metric_key].extend(values)
    for comparison_key, values in batch_delta_abs_by_comparison.items():
        delta_abs_by_comparison[comparison_key].extend(values)


def _compute_bootstrap_statistics_in_executor(
    executor_cls: type[ProcessPoolExecutor] | type[ThreadPoolExecutor],
    bootstrap_context: dict[str, object],
    batch_sizes: list[int],
    child_sequences: list[np.random.SeedSequence] | list[list[np.random.SeedSequence]],
    n_jobs: int,
    show_progress: bool,
) -> list[tuple[dict[str, list[float]], dict[tuple[str, int], list[float]]]]:
    _validate_parallel_batches(batch_sizes, child_sequences)
    with executor_cls(max_workers=n_jobs) as executor:
        futures = [
            executor.submit(
                _compute_bootstrap_statistics_batch,
                bootstrap_context,
                batch_size,
                child_sequence,
                index if show_progress else None,
            )
            for index, (batch_size, child_sequence) in enumerate(zip(batch_sizes, child_sequences))
        ]
        return [future.result() for future in futures]


def _compute_bootstrap_family_max_statistics_in_executor(
    executor_cls: type[ProcessPoolExecutor] | type[ThreadPoolExecutor],
    bootstrap_context: dict[str, object],
    batch_sizes: list[int],
    child_sequences: list[np.random.SeedSequence] | list[list[np.random.SeedSequence]],
    n_jobs: int,
    show_progress: bool,
) -> list[dict[str, list[float]]]:
    _validate_parallel_batches(batch_sizes, child_sequences)
    with executor_cls(max_workers=n_jobs) as executor:
        futures = [
            executor.submit(
                _compute_bootstrap_family_max_statistics_batch,
                bootstrap_context,
                batch_size,
                child_sequence,
                index if show_progress else None,
            )
            for index, (batch_size, child_sequence) in enumerate(zip(batch_sizes, child_sequences))
        ]
        return [future.result() for future in futures]


def _validate_parallel_batches(
    batch_sizes: list[int],
    child_sequences: list[np.random.SeedSequence] | list[list[np.random.SeedSequence]],
) -> None:
    if len(batch_sizes) != len(child_sequences):
        raise ValueError("Batch sizes and child seed sequences must have the same length.")


def _split_resamples_into_batches(resamples: int, n_jobs: int) -> list[int]:
    if resamples <= 0:
        return []
    batch_count = min(resamples, max(1, n_jobs))
    base_batch_size, remainder = divmod(resamples, batch_count)
    return [
        base_batch_size + (1 if batch_index < remainder else 0)
        for batch_index in range(batch_count)
        if base_batch_size + (1 if batch_index < remainder else 0) > 0
    ]


def _compute_bootstrap_family_max_statistics_batch(
    bootstrap_context: dict[str, object],
    resamples: int,
    rng_or_seed: (np.random.Generator | np.random.SeedSequence | list[np.random.SeedSequence]),
    progress_position: int | None = None,
) -> dict[str, list[float]]:
    family_max_statistics, _ = _compute_bootstrap_statistics_batch(
        bootstrap_context=bootstrap_context,
        resamples=resamples,
        rng_or_seed=rng_or_seed,
        progress_position=progress_position,
    )
    return family_max_statistics


def _compute_bootstrap_statistics_batch(
    bootstrap_context: dict[str, object],
    resamples: int,
    rng_or_seed: (np.random.Generator | np.random.SeedSequence | list[np.random.SeedSequence]),
    progress_position: int | None = None,
) -> tuple[dict[str, list[float]], dict[tuple[str, int], list[float]]]:
    group_codes = np.asarray(bootstrap_context["group_codes"], dtype=np.int16)
    group_indices = [
        np.asarray(indices, dtype=np.int64)
        for indices in bootstrap_context.get(
            "group_indices",
            [np.flatnonzero(group_codes == code) for code in np.unique(group_codes)],
        )
    ]
    comparisons = list(bootstrap_context["comparisons"])
    metric_contexts = list(bootstrap_context["metric_contexts"])

    family_max_statistics: dict[str, list[float]] = {
        str(metric_context["metric_key"]): [] for metric_context in metric_contexts
    }
    delta_abs_by_comparison: dict[tuple[str, int], list[float]] = {
        (str(metric_context["metric_key"]), comparison_index): []
        for metric_context in metric_contexts
        for comparison_index in range(len(comparisons))
    }
    replicate_rngs = _build_replicate_generators(resamples, rng_or_seed)

    iterator = range(resamples)
    if progress_position is not None:
        iterator = tqdm(
            iterator,
            total=resamples,
            desc="bootstrap",
            position=progress_position,
            leave=(progress_position == 0),
        )

    for replicate_index in iterator:
        sample_indices = _sample_indices_within_groups(
            group_indices=group_indices,
            rng=replicate_rngs[replicate_index],
        )
        sampled_group_codes = group_codes[sample_indices]
        iteration_max_stats, iteration_delta_abs = _compute_metric_family_statistics_from_indices(
            metric_contexts=metric_contexts,
            sampled_group_codes=sampled_group_codes,
            sample_indices=sample_indices,
            comparisons=comparisons,
        )
        for metric_key, max_stat in iteration_max_stats.items():
            family_max_statistics[metric_key].append(max_stat)
        for comparison_key, delta_abs in iteration_delta_abs.items():
            delta_abs_by_comparison[comparison_key].append(delta_abs)

    return family_max_statistics, delta_abs_by_comparison


def _build_replicate_generators(
    resamples: int,
    rng_or_seed: (np.random.Generator | np.random.SeedSequence | list[np.random.SeedSequence]),
) -> list[np.random.Generator]:
    if isinstance(rng_or_seed, np.random.Generator):
        return [rng_or_seed] * resamples
    if isinstance(rng_or_seed, list):
        if len(rng_or_seed) != resamples:
            raise ValueError("The number of replicate seeds must match resamples.")
        return [np.random.default_rng(seed) for seed in rng_or_seed]
    return [np.random.default_rng(seed) for seed in rng_or_seed.spawn(resamples)]


def _sample_indices_within_groups(
    group_indices: list[np.ndarray],
    rng: np.random.Generator,
) -> np.ndarray:
    return np.concatenate(
        [rng.choice(indices, size=indices.shape[0], replace=True) for indices in group_indices]
    )


def _compute_metric_family_max_statistics_from_indices(
    metric_contexts: list[dict[str, object]],
    sampled_group_codes: np.ndarray,
    sample_indices: np.ndarray,
    comparisons: list[tuple[int, int]],
) -> dict[str, float]:
    max_statistics, _ = _compute_metric_family_statistics_from_indices(
        metric_contexts=metric_contexts,
        sampled_group_codes=sampled_group_codes,
        sample_indices=sample_indices,
        comparisons=comparisons,
    )
    return max_statistics


def _compute_metric_family_statistics_from_indices(
    metric_contexts: list[dict[str, object]],
    sampled_group_codes: np.ndarray,
    sample_indices: np.ndarray,
    comparisons: list[tuple[int, int]],
) -> tuple[dict[str, float], dict[tuple[str, int], float]]:
    max_statistics: dict[str, float] = {}
    delta_abs_by_comparison: dict[tuple[str, int], float] = {}
    for metric_context in metric_contexts:
        metric_key = str(metric_context["metric_key"])
        sampled_metric = _prepare_sampled_metric_context(
            metric_context=metric_context,
            sample_indices=sample_indices,
            recompute_outliers=True,
        )
        observed_deltas = list(metric_context.get("observed_deltas", [0.0] * len(comparisons)))
        observed_valid = set(
            metric_context.get(
                "observed_valid_comparisons",
                range(len(comparisons)),
            )
        )
        comparison_statistics: list[float] = []
        family_valid = bool(observed_valid)
        for comparison_index, (test_group_code, baseline_group_code) in enumerate(comparisons):
            delta, standard_error = _compute_metric_delta_and_standard_error(
                sampled_metric=sampled_metric,
                sampled_group_codes=sampled_group_codes,
                baseline_group_code=baseline_group_code,
                test_group_code=test_group_code,
            )
            delta_abs_by_comparison[(metric_key, comparison_index)] = delta
            if comparison_index not in observed_valid:
                continue
            observed_delta = float(observed_deltas[comparison_index])
            statistic = _compute_studentized_statistic(
                delta - observed_delta,
                standard_error,
            )
            if math.isfinite(statistic):
                comparison_statistics.append(abs(statistic))
            else:
                family_valid = False
        if family_valid and len(comparison_statistics) == len(observed_valid):
            max_statistics[metric_key] = max(comparison_statistics)
        else:
            max_statistics[metric_key] = math.nan
    return max_statistics, delta_abs_by_comparison


def _prepare_sampled_metric_context(
    metric_context: dict[str, object],
    sample_indices: np.ndarray,
    *,
    recompute_outliers: bool,
) -> dict[str, object]:
    outlier_context = metric_context.get("outlier_context")
    if metric_context["kind"] == "mean" or metric_context.get("level") == "user":
        sampled_values = np.asarray(metric_context["values"], dtype=float)[sample_indices]
        sampled_outlier_context = _build_sampled_outlier_context(
            values=sampled_values,
            outlier_context=outlier_context,
            recompute_outliers=recompute_outliers,
        )
        transformed_values, _ = _apply_outliers_to_values(
            pd.Series(sampled_values),
            sampled_outlier_context,
        )
        return {
            "kind": metric_context["kind"],
            "level": metric_context.get("level"),
            "values": transformed_values.to_numpy(dtype=float),
        }

    sampled_numerator = np.asarray(metric_context["numerator"], dtype=float)[sample_indices]
    sampled_denominator = np.asarray(metric_context["denominator"], dtype=float)[sample_indices]
    ratio_values = np.full(sample_indices.shape[0], np.nan, dtype=float)
    cutoff_candidates = (
        ~np.isnan(sampled_numerator) & ~np.isnan(sampled_denominator) & (sampled_denominator > 0)
    )
    ratio_values[cutoff_candidates] = (
        sampled_numerator[cutoff_candidates] / sampled_denominator[cutoff_candidates]
    )
    sampled_outlier_context = _build_sampled_outlier_context(
        values=ratio_values,
        outlier_context=outlier_context,
        recompute_outliers=recompute_outliers,
    )
    transformed_numerator, transformed_denominator, _ = _apply_outliers_to_agg_ratio_components(
        numerator=pd.Series(sampled_numerator),
        denominator=pd.Series(sampled_denominator),
        outlier_context=sampled_outlier_context,
    )
    numerator_array = transformed_numerator.to_numpy(dtype=float)
    denominator_array = transformed_denominator.to_numpy(dtype=float)
    return {
        "kind": "ratio",
        "level": "agg",
        "numerator": numerator_array,
        "denominator": denominator_array,
        "valid_mask": _build_ratio_valid_mask_from_arrays(
            numerator=numerator_array,
            denominator=denominator_array,
            level="agg",
        ),
    }


def _build_sampled_outlier_context(
    values: np.ndarray,
    outlier_context: object,
    *,
    recompute_outliers: bool,
) -> dict[str, object] | None:
    if not isinstance(outlier_context, dict):
        return None
    sampled_context = dict(outlier_context)
    if recompute_outliers and "quantile" in sampled_context:
        sampled_context["cutoff"] = _compute_outlier_cutoff(
            pd.Series(values),
            outliers_quantile=float(sampled_context["quantile"]),
            outliers_policy=str(sampled_context["policy"]),
        )
    return sampled_context


def _compute_metric_delta_and_standard_error(
    sampled_metric: dict[str, object],
    sampled_group_codes: np.ndarray,
    baseline_group_code: int,
    test_group_code: int,
) -> tuple[float, float]:
    baseline_mask = sampled_group_codes == baseline_group_code
    test_mask = sampled_group_codes == test_group_code

    if sampled_metric["kind"] == "mean" or sampled_metric.get("level") == "user":
        sampled_values = np.asarray(sampled_metric["values"], dtype=float)
        baseline_values = sampled_values[baseline_mask & ~np.isnan(sampled_values)]
        test_values = sampled_values[test_mask & ~np.isnan(sampled_values)]
        delta = _compute_mean_delta_from_arrays(baseline_values, test_values)
        standard_error = _compute_group_diff_standard_error(
            baseline_variance=_compute_sample_variance(pd.Series(baseline_values)),
            baseline_n=int(baseline_values.shape[0]),
            test_variance=_compute_sample_variance(pd.Series(test_values)),
            test_n=int(test_values.shape[0]),
        )
        return delta, standard_error

    sampled_numerator = np.asarray(sampled_metric["numerator"], dtype=float)
    sampled_denominator = np.asarray(sampled_metric["denominator"], dtype=float)
    sampled_valid_mask = np.asarray(sampled_metric["valid_mask"], dtype=bool)
    baseline_valid_mask = baseline_mask & sampled_valid_mask
    test_valid_mask = test_mask & sampled_valid_mask

    baseline_stats = _compute_agg_ratio_group_stats_arrays(
        sampled_numerator[baseline_valid_mask],
        sampled_denominator[baseline_valid_mask],
    )
    test_stats = _compute_agg_ratio_group_stats_arrays(
        sampled_numerator[test_valid_mask],
        sampled_denominator[test_valid_mask],
    )
    if not _both_present(test_stats["ratio"], baseline_stats["ratio"]):
        return math.nan, math.nan
    delta = test_stats["ratio"] - baseline_stats["ratio"]
    standard_error = _compute_agg_ratio_diff_standard_error(
        baseline_frame=_build_ratio_frame_from_arrays(
            sampled_numerator[baseline_valid_mask],
            sampled_denominator[baseline_valid_mask],
        ),
        baseline_ratio=baseline_stats["ratio"],
        test_frame=_build_ratio_frame_from_arrays(
            sampled_numerator[test_valid_mask],
            sampled_denominator[test_valid_mask],
        ),
        test_ratio=test_stats["ratio"],
    )
    return delta, standard_error


def _compute_metric_test_statistic_from_indices(
    metric_context: dict[str, object],
    sampled_group_codes: np.ndarray,
    sample_indices: np.ndarray,
    baseline_group_code: int,
    test_group_code: int,
) -> float:
    statistic, _ = _compute_metric_statistic_and_delta_from_indices(
        metric_context=metric_context,
        sampled_group_codes=sampled_group_codes,
        sample_indices=sample_indices,
        baseline_group_code=baseline_group_code,
        test_group_code=test_group_code,
    )
    return statistic


def _compute_metric_statistic_and_delta_from_indices(
    metric_context: dict[str, object],
    sampled_group_codes: np.ndarray,
    sample_indices: np.ndarray,
    baseline_group_code: int,
    test_group_code: int,
) -> tuple[float, float]:
    sampled_metric = _prepare_sampled_metric_context(
        metric_context=metric_context,
        sample_indices=sample_indices,
        recompute_outliers=True,
    )
    delta, standard_error = _compute_metric_delta_and_standard_error(
        sampled_metric=sampled_metric,
        sampled_group_codes=sampled_group_codes,
        baseline_group_code=baseline_group_code,
        test_group_code=test_group_code,
    )
    return _compute_studentized_statistic(delta, standard_error), delta


def _compute_mean_delta_from_arrays(
    baseline_values: np.ndarray,
    test_values: np.ndarray,
) -> float:
    if baseline_values.shape[0] == 0 or test_values.shape[0] == 0:
        return math.nan
    return float(np.mean(test_values) - np.mean(baseline_values))
