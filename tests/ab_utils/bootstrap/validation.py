from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    Any,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    bootstrap_module,
    math,
    np,
    pd,
    pytest,
)


def test_bootstrap_context_records_nonfinite_delta_and_no_outlier_context() -> None:
    context = {
        "comparisons": [("test", "control")],
        "metric_contexts": [{"metric_key": "metric"}],
    }
    bootstrap_module._replace_observed_statistics_from_rows(
        context,
        [{"_metric_key": "metric", "_comparison_key": ("test", "control"), "delta_abs": math.nan}],
        {("test", "control"): 0},
    )
    assert context["metric_contexts"][0]["observed_valid_comparisons"] == []

    prepared = bootstrap_module._prepare_bootstrap_context(
        pd.DataFrame({"group": ["control", "test"], "metric": [1.0, 2.0]}),
        "group",
        [{"kind": "mean", "metric_key": "metric", "column": "metric"}],
        [("test", "control")],
    )
    assert prepared["metric_contexts"][0]["outlier_context"] is None


def test_multiple_comparisons_adjustment_handles_unusable_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context: dict[str, object] = {
        "metric_contexts": [
            {"metric_key": "missing_comparison"},
            {"metric_key": "invalid_observed"},
            {"metric_key": "no_family"},
        ],
        "comparisons": [(1, 0)],
    }
    rows: list[dict[str, object]] = [
        {
            "_metric_key": "missing_comparison",
            "_comparison_key": ("other", "control"),
            "_test_stat": 1.0,
            "delta_abs": 1.0,
        },
        {
            "_metric_key": "invalid_observed",
            "_comparison_key": ("test", "control"),
            "_test_stat": math.nan,
            "delta_abs": 1.0,
        },
        {
            "_metric_key": "no_family",
            "_comparison_key": ("test", "control"),
            "_test_stat": 1.0,
            "delta_abs": 1.0,
        },
        {
            "_metric_key": "unknown_metric",
            "_comparison_key": ("test", "control"),
            "_test_stat": 1.0,
            "delta_abs": 1.0,
        },
    ]
    monkeypatch.setattr(bootstrap_module, "_prepare_bootstrap_context", lambda **_kwargs: context)
    monkeypatch.setattr(
        bootstrap_module,
        "_compute_bootstrap_statistics",
        lambda **_kwargs: (
            {
                "missing_comparison": [1.0],
                "invalid_observed": [1.0],
                "no_family": [],
            },
            {
                ("invalid_observed", 0): [1.0, 2.0],
                ("no_family", 0): [1.0],
            },
        ),
    )

    bootstrap_module._apply_multiple_comparisons_adjustment(
        rows=rows,
        df=pd.DataFrame(),
        group_column="group_name",
        metric_definitions=[],
        comparisons=[("test", "control")],
        resamples=2,
        random_state=0,
        n_jobs=1,
        show_progress=False,
    )

    assert math.isnan(float(rows[0]["s.e. bootstrap"]))
    assert rows[0]["bootstrap_adj_p"] == 1.0
    assert rows[1]["s.e. bootstrap"] == pytest.approx(math.sqrt(0.5))
    assert math.isnan(float(rows[1]["bootstrap_adj_p"]))
    assert math.isnan(float(rows[2]["s.e. bootstrap"]))
    assert math.isnan(float(rows[2]["bootstrap_adj_p"]))


def test_multiple_comparisons_adjustment_returns_for_no_rows() -> None:
    bootstrap_module._apply_multiple_comparisons_adjustment(
        rows=[],
        df=pd.DataFrame(),
        group_column="group_name",
        metric_definitions=[],
        comparisons=[],
        resamples=1,
        random_state=0,
        n_jobs=1,
        show_progress=False,
    )


def test_bootstrap_discard_warning_skips_metrics_without_observed_comparisons() -> None:
    context = {
        "metric_contexts": [
            {"metric_key": "unused", "observed_valid_comparisons": []},
            {"metric_key": "orders", "observed_valid_comparisons": [0]},
        ]
    }
    with pytest.warns(RuntimeWarning, match="discarded 1 of 2"):
        bootstrap_module._warn_about_discarded_family_resamples(
            context,
            {"unused": [math.nan], "orders": [1.0, math.nan]},
        )


def test_prepare_bootstrap_context_copies_external_outlier_context() -> None:
    external = {"orders": {"quantile": 0.5, "policy": "truncate", "cutoff": 2.0}}
    context = bootstrap_module._prepare_bootstrap_context(
        df=pd.DataFrame(
            {
                "group_name": ["control", "control", "test", "test"],
                "orders": [1.0, 2.0, 3.0, 4.0],
            }
        ),
        group_column="group_name",
        metric_definitions=[{"kind": "mean", "metric_key": "orders", "column": "orders"}],
        comparisons=[("test", "control")],
        outlier_contexts=external,
    )
    metric_context = next(iter(context["metric_contexts"]))
    assert metric_context["outlier_context"] == external["orders"]
    assert metric_context["outlier_context"] is not external["orders"]


def test_bootstrap_family_wrapper_returns_only_max_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_module,
        "_compute_bootstrap_statistics",
        lambda **_kwargs: ({"orders": [1.0]}, {("orders", 0): [2.0]}),
    )
    assert bootstrap_module._compute_bootstrap_family_max_statistics(
        bootstrap_context={},
        resamples=1,
        random_state=0,
        n_jobs=1,
        show_progress=False,
    ) == {"orders": [1.0]}


def test_bootstrap_statistics_handles_zero_resamples() -> None:
    family, deltas = bootstrap_module._compute_bootstrap_statistics(
        bootstrap_context={
            "metric_contexts": [{"metric_key": "orders"}],
            "comparisons": [(1, 0)],
        },
        resamples=0,
        random_state=0,
        n_jobs=2,
        show_progress=False,
    )
    assert family == {"orders": []}
    assert deltas == {("orders", 0): []}


def test_bootstrap_statistics_falls_back_to_thread_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executors: list[type[ProcessPoolExecutor | ThreadPoolExecutor]] = []

    def fake_executor(**kwargs: Any) -> list[tuple[dict[str, list[float]], dict[Any, list[float]]]]:
        executor_cls = kwargs["executor_cls"]
        executors.append(executor_cls)
        if executor_cls is ProcessPoolExecutor:
            raise OSError
        return [({"orders": [1.0, 2.0]}, {("orders", 0): [3.0, 4.0]})]

    monkeypatch.setattr(
        bootstrap_module, "_compute_bootstrap_statistics_in_executor", fake_executor
    )
    family, deltas = bootstrap_module._compute_bootstrap_statistics(
        bootstrap_context={
            "metric_contexts": [{"metric_key": "orders"}],
            "comparisons": [(1, 0)],
        },
        resamples=2,
        random_state=0,
        n_jobs=2,
        show_progress=False,
    )
    assert executors == [ProcessPoolExecutor, ThreadPoolExecutor]
    assert family == {"orders": [1.0, 2.0]}
    assert deltas == {("orders", 0): [3.0, 4.0]}


def test_bootstrap_family_executor_and_batch_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_batch_wrapper = bootstrap_module._compute_bootstrap_family_max_statistics_batch
    monkeypatch.setattr(
        bootstrap_module,
        "_compute_bootstrap_family_max_statistics_batch",
        lambda _context, count, _seed, _position: {"orders": [float(count)]},
    )
    results = bootstrap_module._compute_bootstrap_family_max_statistics_in_executor(
        executor_cls=ThreadPoolExecutor,
        bootstrap_context={},
        batch_sizes=[1, 2],
        child_sequences=[np.random.SeedSequence(1), np.random.SeedSequence(2)],
        n_jobs=2,
        show_progress=True,
    )
    assert results == [{"orders": [1.0]}, {"orders": [2.0]}]

    monkeypatch.setattr(
        bootstrap_module,
        "_compute_bootstrap_family_max_statistics_batch",
        original_batch_wrapper,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_compute_bootstrap_statistics_batch",
        lambda **_kwargs: ({"orders": [3.0]}, {("orders", 0): [4.0]}),
    )
    assert bootstrap_module._compute_bootstrap_family_max_statistics_batch(
        bootstrap_context={},
        resamples=1,
        rng_or_seed=np.random.SeedSequence(0),
    ) == {"orders": [3.0]}


def test_bootstrap_batch_and_seed_helpers_validate_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        bootstrap_module._validate_parallel_batches(
            [1, 1],
            [np.random.SeedSequence(0)],
        )
    assert bootstrap_module._split_resamples_into_batches(0, n_jobs=2) == []

    generator = np.random.default_rng(0)
    assert bootstrap_module._build_replicate_generators(2, generator) == [generator, generator]
    with pytest.raises(ValueError, match="must match resamples"):
        bootstrap_module._build_replicate_generators(2, [np.random.SeedSequence(0)])
    generated = bootstrap_module._build_replicate_generators(2, np.random.SeedSequence(0))
    assert len(generated) == 2
    assert all(isinstance(item, np.random.Generator) for item in generated)


def test_bootstrap_family_statistics_skip_unobserved_comparisons() -> None:
    metric_context = {
        "kind": "mean",
        "metric_key": "orders",
        "values": np.array([1.0, 2.0, 3.0, 4.0]),
        "observed_deltas": [math.nan, 1.0],
        "observed_valid_comparisons": [1],
    }
    max_stats, deltas = bootstrap_module._compute_metric_family_statistics_from_indices(
        metric_contexts=[metric_context],
        sampled_group_codes=np.array([0, 0, 1, 1], dtype=np.int16),
        sample_indices=np.arange(4),
        comparisons=[(1, 0), (1, 0)],
    )
    assert max_stats["orders"] == pytest.approx(math.sqrt(2))
    assert set(deltas) == {("orders", 0), ("orders", 1)}

    metric_context["observed_valid_comparisons"] = []
    max_stats = bootstrap_module._compute_metric_family_max_statistics_from_indices(
        metric_contexts=[metric_context],
        sampled_group_codes=np.array([0, 0, 1, 1], dtype=np.int16),
        sample_indices=np.arange(4),
        comparisons=[(1, 0)],
    )
    assert math.isnan(max_stats["orders"])


def test_bootstrap_sampled_outlier_context_is_recomputed_only_for_dicts() -> None:
    assert (
        bootstrap_module._build_sampled_outlier_context(
            np.array([1.0]),
            None,
            recompute_outliers=True,
        )
        is None
    )
    context = bootstrap_module._build_sampled_outlier_context(
        np.array([1.0, 9.0]),
        {"quantile": 0.5, "policy": "truncate", "cutoff": 9.0},
        recompute_outliers=True,
    )
    assert context is not None
    assert context["cutoff"] == pytest.approx(5.0)


def test_bootstrap_agg_ratio_and_statistic_helpers_propagate_missing_groups() -> None:
    sampled_metric = {
        "kind": "ratio",
        "level": "agg",
        "numerator": np.array([1.0, 2.0]),
        "denominator": np.array([0.0, 0.0]),
        "valid_mask": np.array([False, False]),
    }
    delta, standard_error = bootstrap_module._compute_metric_delta_and_standard_error(
        sampled_metric=sampled_metric,
        sampled_group_codes=np.array([0, 1], dtype=np.int16),
        baseline_group_code=0,
        test_group_code=1,
    )
    assert math.isnan(delta)
    assert math.isnan(standard_error)

    metric_context = {
        "kind": "mean",
        "metric_key": "orders",
        "values": np.array([1.0, 2.0, 3.0, 4.0]),
    }
    statistic = bootstrap_module._compute_metric_test_statistic_from_indices(
        metric_context=metric_context,
        sampled_group_codes=np.array([0, 0, 1, 1], dtype=np.int16),
        sample_indices=np.arange(4),
        baseline_group_code=0,
        test_group_code=1,
    )
    assert statistic == pytest.approx(2 * math.sqrt(2))
    assert math.isnan(
        bootstrap_module._compute_mean_delta_from_arrays(
            np.array([], dtype=float),
            np.array([1.0]),
        )
    )
