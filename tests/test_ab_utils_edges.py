from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.ab_utils import api as api_module
from analytics_toolkit.ab_utils import bootstrap as bootstrap_module
from analytics_toolkit.ab_utils import ratio as ratio_module
from analytics_toolkit.ab_utils import split as split_module
from analytics_toolkit.ab_utils import sql_native
from analytics_toolkit.ab_utils import stats as stats_module
from analytics_toolkit.ab_utils import validation as validation_module


def test_validate_input_columns_lists_every_missing_column() -> None:
    with pytest.raises(ValueError, match=r"'group_name'.*'user_id'"):
        validation_module._validate_input_columns(
            pd.DataFrame({"metric": [1.0]}),
            group="group_name",
            user_id="user_id",
        )


@pytest.mark.parametrize(
    ("delta", "standard_error"),
    [
        (math.inf, 1.0),
        (-math.inf, 1.0),
        (1.0, math.inf),
        (1.0, -math.inf),
    ],
)
def test_studentized_statistic_rejects_non_finite_inputs(
    delta: float,
    standard_error: float,
) -> None:
    assert math.isnan(stats_module._compute_studentized_statistic(delta, standard_error))


def test_stats_helpers_cover_empty_and_degenerate_samples() -> None:
    assert math.isnan(stats_module._safe_mean(pd.Series(dtype=float)))
    assert math.isnan(stats_module._compute_sample_variance(pd.Series([1.0])))
    assert math.isnan(stats_module._compute_sample_variance(pd.Series([math.nan, math.nan])))
    assert math.isnan(stats_module._compute_group_diff_standard_error(1.0, 0, 1.0, 2))
    assert math.isnan(stats_module._compute_group_diff_standard_error(math.nan, 2, 1.0, 2))
    assert all(
        math.isnan(value)
        for value in stats_module._compute_ttest_stat_and_p_value(
            pd.Series([1.0]), pd.Series([2.0])
        )
    )
    assert math.isnan(
        stats_module._compute_mde_abs(pd.Series([1.0, 1.0]), pd.Series([math.nan, math.nan]))
    )
    statistic, p_value = stats_module._compute_ttest_stat_and_p_value_arrays(
        np.array([1.0, 2.0]), np.array([2.0, 3.0])
    )
    assert math.isfinite(statistic)
    assert 0 <= p_value <= 1


def test_numeric_metric_validation_and_safe_relative_edges() -> None:
    with pytest.raises(TypeError, match="contains non-numeric"):
        stats_module._get_numeric_metric_series(pd.DataFrame({"metric": [1, "bad"]}), "metric")
    assert math.isnan(stats_module._safe_relative(math.nan, 1.0))
    assert math.isnan(stats_module._safe_relative(1.0, math.nan))
    assert math.isnan(stats_module._safe_relative(1.0, 0.0))
    assert stats_module._safe_relative(4.0, 2.0) == 2.0


def test_changed_metric_defaults_preserves_every_explicit_override() -> None:
    pre_exp = pd.DataFrame({"id": [1]})
    ratio_metrics = [{"name": "ratio", "numerator": "a", "denominator": "b"}]

    assert api_module._changed_metric_defaults(
        group="arm",
        control="baseline",
        user_id="id",
        mde_alpha=0.01,
        mde_power=0.9,
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=99,
        bootstrap_random_state=None,
        bootstrap_n_jobs=2,
        bootstrap_progress=True,
        pre_exp_metrics_df=pre_exp,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    ) == {
        "group": "arm",
        "control": "baseline",
        "user_id": "id",
        "mde_alpha": 0.01,
        "mde_power": 0.9,
        "ratio_metrics": ratio_metrics,
        "test_vs_test": False,
        "multiple_comparisons_adjustment": True,
        "multiple_comparisons_adjustment_resamples": 99,
        "bootstrap_random_state": None,
        "bootstrap_n_jobs": 2,
        "bootstrap_progress": True,
        "pre_exp_metrics_df": pre_exp,
        "outliers_quantile": 0.95,
        "outliers_policy": "truncate",
    }


@pytest.mark.parametrize("case", ["null_user", "duplicate_user", "null_group"])
def test_compute_test_metrics_rejects_invalid_identity_columns(case: str) -> None:
    frame = pd.DataFrame(
        {"user_id": [1, 2], "group_name": ["control", "test"], "metric": [1.0, 2.0]}
    )
    if case == "null_user":
        frame.loc[1, "user_id"] = np.nan
    elif case == "duplicate_user":
        frame["user_id"] = [1, 1]
    else:
        frame.loc[1, "group_name"] = None

    with pytest.raises(ValueError, match="must"):
        api_module._compute_test_metrics_dataframe(frame)


def test_compute_test_metrics_rejects_missing_metric_and_control() -> None:
    with pytest.raises(ValueError, match="at least one metric"):
        api_module._compute_test_metrics_dataframe(
            pd.DataFrame({"user_id": [1, 2], "group_name": ["control", "test"]})
        )
    with pytest.raises(ValueError, match="Control label"):
        api_module._compute_test_metrics_dataframe(
            pd.DataFrame({"user_id": [1, 2], "group_name": ["a", "b"], "metric": [1.0, 2.0]})
        )


@pytest.mark.parametrize(
    ("alpha", "power", "message"),
    [
        (0.0, 0.8, "mde_alpha"),
        (0.05, 1.0, "mde_power"),
    ],
)
def test_validate_mde_parameters_rejects_boundary_values(
    alpha: float,
    power: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validation_module._validate_mde_parameters(alpha, power)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("null_user", "must not contain missing values"),
        ("duplicate_user", "must contain unique user ids"),
        ("null_group", "must not contain missing values"),
        ("missing_control", "was not found"),
        ("group_mismatch", "must match"),
    ],
)
def test_validate_pre_experiment_dataframe_rejects_invalid_pairs(
    case: str,
    message: str,
) -> None:
    experiment = pd.DataFrame({"user_id": [1, 2], "group_name": ["control", "control"]})
    pre_experiment = experiment.copy()
    if case == "null_user":
        pre_experiment.loc[1, "user_id"] = np.nan
    elif case == "duplicate_user":
        pre_experiment["user_id"] = [1, 1]
    elif case == "null_group":
        pre_experiment.loc[1, "group_name"] = None
    elif case == "missing_control":
        pre_experiment["group_name"] = "test"
    else:
        pre_experiment["group_name"] = ["test", "control"]

    with pytest.raises(ValueError, match=message):
        validation_module._validate_pre_experiment_dataframe(
            experiment,
            pre_experiment,
            group="group_name",
            control="control",
            user_id="user_id",
        )


@pytest.mark.parametrize(
    ("resamples", "error", "message"),
    [
        (True, TypeError, "must be an integer"),
        (0, ValueError, "must be positive"),
    ],
)
def test_validate_multiple_comparisons_rejects_invalid_resamples(
    resamples: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        validation_module._validate_multiple_comparisons_parameters(
            multiple_comparisons_adjustment=True,
            multiple_comparisons_adjustment_resamples=resamples,
            bootstrap_random_state=0,
            bootstrap_n_jobs=1,
            bootstrap_progress=False,
        )


def _ratio_source() -> pd.DataFrame:
    return pd.DataFrame({"num": [1.0, 2.0], "den": [2.0, 4.0]})


@pytest.mark.parametrize(
    ("specs", "reserved", "error", "message"),
    [
        (["bad"], set(), TypeError, "must be a dictionary"),
        (
            [{"name": "r", "numerator": "num", "denominator": "den", "level": "row"}],
            set(),
            ValueError,
            "invalid level",
        ),
        (
            [
                {
                    "name": "r",
                    "numerator": "num",
                    "denominator": "den",
                    "invalid_denominator": "raise",
                }
            ],
            set(),
            ValueError,
            "invalid invalid_denominator",
        ),
        (
            [
                {"name": "r", "numerator": "num", "denominator": "den"},
                {"name": "r", "numerator": "num", "denominator": "den"},
            ],
            set(),
            ValueError,
            "Duplicate ratio metric",
        ),
        (
            [{"name": "r", "numerator": "num", "denominator": "den"}],
            {"num"},
            ValueError,
            "reserved column",
        ),
        (
            [{"name": "r", "numerator": "missing", "denominator": "den"}],
            set(),
            ValueError,
            "missing column",
        ),
        (
            [{"name": "r", "numerator": "num"}],
            set(),
            ValueError,
            "missing required key",
        ),
        (
            [{"name": " ", "numerator": "num", "denominator": "den"}],
            set(),
            ValueError,
            "empty 'name'",
        ),
    ],
)
def test_normalize_ratio_metrics_rejects_invalid_specs(
    specs: list[object],
    reserved: set[str],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        ratio_module._normalize_ratio_metrics(_ratio_source(), specs, reserved)


def test_agg_ratio_linearization_reports_invalid_denominators() -> None:
    values, reason = ratio_module._build_agg_ratio_linearized_values(
        pd.Series([np.nan]),
        pd.Series([np.nan]),
    )
    assert values.isna().all()
    assert reason == "no valid numerator/denominator pairs"

    values, reason = ratio_module._build_agg_ratio_linearized_values(
        pd.Series([1.0, 2.0]),
        pd.Series([-1.0, 0.0]),
    )
    assert values.isna().all()
    assert reason == "aggregate denominator sum is non-positive"


def test_agg_ratio_stats_handle_empty_and_nonpositive_denominators() -> None:
    empty = pd.DataFrame(columns=["numerator", "denominator"])
    assert ratio_module._compute_agg_ratio_group_stats(empty)["n"] == 0
    assert math.isnan(ratio_module._compute_agg_ratio_group_stats(empty)["ratio"])

    nonpositive = pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [-1.0, 0.0]})
    assert math.isnan(ratio_module._compute_agg_ratio_group_stats(nonpositive)["ratio"])
    assert math.isnan(
        ratio_module._compute_agg_ratio_diff_standard_error(
            empty,
            math.nan,
            nonpositive,
            math.nan,
        )
    )


def test_agg_ratio_variance_rejects_invalid_mean_and_variance() -> None:
    nonpositive = pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [-1.0, -1.0]})
    assert math.isnan(ratio_module._compute_agg_ratio_variance(nonpositive, 1.0))

    missing = pd.DataFrame({"numerator": [np.nan, np.nan], "denominator": [1.0, 1.0]})
    assert math.isnan(ratio_module._compute_agg_ratio_variance(missing, 1.0))


def test_agg_ratio_array_stats_handle_empty_and_nonpositive_denominators() -> None:
    empty_stats = ratio_module._compute_agg_ratio_group_stats_arrays(
        np.array([], dtype=float),
        np.array([], dtype=float),
    )
    assert empty_stats["n"] == 0
    assert math.isnan(empty_stats["ratio"])

    invalid_stats = ratio_module._compute_agg_ratio_group_stats_arrays(
        np.array([1.0]),
        np.array([0.0]),
    )
    assert invalid_stats["n"] == 1
    assert math.isnan(invalid_stats["ratio"])


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda: split_module._build_group_names(True), TypeError, "must be an integer"),
        (
            lambda: split_module._normalize_group_ratios(50, expected_size=2),
            TypeError,
            "must be a sequence",
        ),
        (
            lambda: split_module._normalize_stratification_cols(b"segment"),
            TypeError,
            "string or a sequence",
        ),
        (
            lambda: split_module._normalize_stratification_cols(7),
            TypeError,
            "string or a sequence",
        ),
        (
            lambda: split_module._normalize_stratification_cols(["segment", 1]),
            TypeError,
            "only strings",
        ),
        (
            lambda: split_module._validate_split_dataframe(
                [],
                split_col="user_id",
                stratification_cols=[],
            ),
            TypeError,
            "pandas DataFrame",
        ),
        (
            lambda: split_module._validate_split_dataframe(
                pd.DataFrame({"user_id": [1]}),
                split_col=1,
                stratification_cols=[],
            ),
            TypeError,
            "split_col must be a string",
        ),
        (lambda: split_module._validate_group_col(1), TypeError, "must be a string"),
        (
            lambda: split_module._validate_group_col("is_mandatory_user"),
            ValueError,
            "conflicts",
        ),
        (lambda: split_module._validate_random_state(True), TypeError, "integer or None"),
        (
            lambda: split_module._normalize_target_sample_size(True, max_size=3),
            TypeError,
            "integer or None",
        ),
        (
            lambda: split_module._normalize_target_sample_size(0, max_size=3),
            ValueError,
            "must be positive",
        ),
        (
            lambda: split_module._normalize_mandatory_users_group(1, ["control", "test_1"]),
            TypeError,
            "must be a string",
        ),
    ],
)
def test_split_helpers_validate_types_and_reserved_names(
    call: Any,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        call()


def test_split_none_defaults_return_full_sample_contracts() -> None:
    assert split_module._normalize_stratification_cols(None) == []
    assert split_module._validate_random_state(None) is None
    assert split_module._normalize_target_sample_size(None, max_size=3) == 3


@pytest.mark.parametrize(
    ("mandatory_df", "error", "message"),
    [
        ([], TypeError, "pandas DataFrame"),
        (pd.DataFrame({"user_id": [1, np.nan]}), ValueError, "missing values"),
    ],
)
def test_mandatory_position_validation_rejects_invalid_frames(
    mandatory_df: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        split_module._get_present_mandatory_positions(
            mandatory_users_df=mandatory_df,
            split_col="user_id",
            id_to_position={1: 0},
        )


def test_missing_stratum_detection_handles_scalar_errors_and_array_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_type_error(_value: object) -> bool:
        raise TypeError

    monkeypatch.setattr(split_module.pd, "isna", raise_type_error)
    assert split_module._is_missing_stratum_value(object()) is False

    monkeypatch.setattr(split_module.pd, "isna", lambda _value: np.array([True]))
    assert split_module._is_missing_stratum_value(object()) is False


def test_sample_and_assignment_helpers_handle_empty_and_invalid_counts() -> None:
    rng = np.random.default_rng(0)
    assert (
        split_module._sample_positions_by_strata(
            [0],
            strata_keys=[("all",)],
            sample_size=0,
            rng=rng,
        )
        == []
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        split_module._sample_positions_by_strata(
            [0],
            strata_keys=[("all",)],
            sample_size=2,
            rng=rng,
        )
    with pytest.raises(ValueError, match="must sum"):
        split_module._assign_positions_to_groups(
            [0],
            group_names=["control"],
            group_counts=[0],
            strata_keys=[("all",)],
            rng=rng,
        )
    assert (
        split_module._assign_positions_to_groups(
            [],
            group_names=["control"],
            group_counts=[0],
            strata_keys=[],
            rng=rng,
        )
        == {}
    )


def test_stratified_count_helpers_fill_or_reject_deficits() -> None:
    rng = np.random.default_rng(0)
    assert (
        split_module._build_stratified_count_matrix(
            stratum_sizes=[],
            group_counts=[0, 0],
            rng=rng,
        )
        == []
    )

    counts = np.zeros((2, 2), dtype=int)
    row_deficits = np.array([1, 1])
    column_deficits = np.array([1, 1])
    split_module._fill_remaining_matrix_deficits(
        floor_counts=counts,
        row_deficits=row_deficits,
        column_deficits=column_deficits,
        rng=rng,
    )
    assert counts.sum(axis=1).tolist() == [1, 1]
    assert counts.sum(axis=0).tolist() == [1, 1]

    with pytest.raises(ValueError, match="Unable to build"):
        split_module._fill_remaining_matrix_deficits(
            floor_counts=np.zeros((1, 1), dtype=int),
            row_deficits=np.array([1]),
            column_deficits=np.array([0]),
            rng=rng,
        )


@pytest.mark.parametrize(
    ("total", "weights", "expected", "message"),
    [
        (-1, [1.0], None, "non-negative"),
        (1, [], None, "must not be empty"),
        (1, [-1.0], None, "non-negative finite"),
        (1, [math.inf], None, "non-negative finite"),
        (1, [0.0], None, "positive sum"),
        (0, [], [], None),
    ],
)
def test_round_counts_validates_degenerate_weights(
    total: int,
    weights: list[float],
    expected: list[int] | None,
    message: str | None,
) -> None:
    if message is None:
        assert split_module._round_counts(total, weights, rng=np.random.default_rng(0)) == expected
        return
    with pytest.raises(ValueError, match=message):
        split_module._round_counts(total, weights, rng=np.random.default_rng(0))


def test_fit_counts_to_capacities_redistributes_and_detects_shortfall() -> None:
    rng = np.random.default_rng(0)
    assert split_module._fit_counts_to_capacities(
        [5, 0],
        capacities=[1, 5],
        total=5,
        rng=rng,
    ) == [1, 4]
    assert split_module._fit_counts_to_capacities(
        [1, 1],
        capacities=[1, 1],
        total=2,
        rng=rng,
    ) == [1, 1]
    with pytest.raises(ValueError, match="Unable to fit"):
        split_module._fit_counts_to_capacities(
            [0],
            capacities=[0],
            total=1,
            rng=rng,
        )


def test_take_random_positions_handles_zero_and_oversized_requests() -> None:
    rng = np.random.default_rng(0)
    assert split_module._take_random_positions([1, 2], 0, rng=rng) == []
    with pytest.raises(ValueError, match="cannot exceed"):
        split_module._take_random_positions([1], 2, rng=rng)


def test_do_split_rejects_non_boolean_compensation() -> None:
    with pytest.raises(TypeError, match="must be a boolean"):
        split_module.do_split(
            pd.DataFrame({"user_id": [1, 2]}),
            compensate_mandatory_users=1,
        )


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


def _sql_source(
    *,
    backend: str = "gp",
    columns: list[str] | None = None,
) -> sql_native._SqlNativeSource:
    resolved_columns = columns or ["user_id", "group_name", "orders"]
    return sql_native._SqlNativeSource(
        backend=backend,
        source_sql='"mart"."ab_source"',
        sql_where=None,
        columns=resolved_columns,
        column_types=dict.fromkeys(resolved_columns, "double precision"),
    )


def _resolve_source_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "db_key": "analytics",
        "source": "mart.ab_source",
        "source_type": "table",
        "sql_where": None,
        "print_queries": False,
        "retry_cnt": 1,
        "timeout_increment": 1,
        "query_label": None,
    }
    kwargs.update(overrides)
    return kwargs


def test_sql_native_single_source_rejects_parallel_concurrency() -> None:
    with pytest.raises(ValueError, match="only when source is a task mapping"):
        sql_native.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            concurrency=2,
        )


def test_sql_native_single_rejects_cross_backend_pre_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = iter([_sql_source(backend="gp"), _sql_source(backend="trino")])
    monkeypatch.setattr(sql_native, "_resolve_sql_native_source", lambda **_kwargs: next(sources))

    with pytest.raises(ValueError, match="same backend"):
        sql_native.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            pre_exp_source="mart.pre_source",
            metric_columns=["orders"],
        )


def test_sql_native_single_requires_at_least_one_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sql_native,
        "_resolve_sql_native_source",
        lambda **_kwargs: _sql_source(columns=["user_id", "group_name"]),
    )

    with pytest.raises(ValueError, match="At least one metric"):
        sql_native.compute_test_metrics_sql_native("analytics", "mart.ab_source")


def test_resolve_sql_native_source_rejects_missing_and_empty_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="must not be None"):
        sql_native._resolve_sql_native_source(**_resolve_source_kwargs(source=None))
    with pytest.raises(ValueError, match="table name must not be empty"):
        sql_native._resolve_sql_native_source(**_resolve_source_kwargs(source=" "))

    monkeypatch.setattr(
        sql_native.sql_facade,
        "table_info",
        lambda *_args: SimpleNamespace(exists=False),
    )
    with pytest.raises(ValueError, match="does not exist"):
        sql_native._resolve_sql_native_source(**_resolve_source_kwargs())

    monkeypatch.setattr(
        sql_native,
        "get_connection_config",
        lambda _key: SimpleNamespace(backend="gp"),
    )
    with pytest.raises(ValueError, match="source SQL must not be empty"):
        sql_native._resolve_sql_native_source(
            **_resolve_source_kwargs(source=" ; ", source_type="sql")
        )


def test_sql_native_source_type_and_metric_column_validation() -> None:
    with pytest.raises(ValueError, match="either 'table' or 'sql'"):
        sql_native._normalize_source_type("view")

    with pytest.raises(ValueError, match="Missing required"):
        sql_native._resolve_metric_columns(
            columns=["user_id", "orders"],
            column_types={},
            metric_columns=None,
            ratio_specs=[],
            group="group_name",
            user_id="user_id",
        )
    with pytest.raises(ValueError, match="must not contain duplicates"):
        sql_native._resolve_metric_columns(
            columns=["user_id", "group_name", "orders"],
            column_types={},
            metric_columns=["orders", "orders"],
            ratio_specs=[],
            group="group_name",
            user_id="user_id",
        )
    with pytest.raises(ValueError, match="Missing metric"):
        sql_native._resolve_metric_columns(
            columns=["user_id", "group_name", "orders"],
            column_types={},
            metric_columns=["missing"],
            ratio_specs=[],
            group="group_name",
            user_id="user_id",
        )
    with pytest.raises(ValueError, match="Duplicate metric"):
        sql_native._validate_metric_name_conflicts(
            ["orders"],
            [{"name": "orders"}],
        )

    assert sql_native._is_sql_numeric_type("") is False
    assert sql_native._is_sql_numeric_type("Nullable(Int64)") is True


def test_read_sql_native_query_delegates_all_execution_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_read(**kwargs: object) -> pd.DataFrame:
        captured.update(kwargs)
        return pd.DataFrame({"value": [1]})

    monkeypatch.setattr(sql_native, "_read_sql_mde_query", fake_read)
    result = sql_native._read_sql_native_query(
        db_key="analytics",
        query="SELECT 1",
        print_queries=True,
        retry_cnt=2,
        timeout_increment=3.5,
        query_label="edge",
    )
    assert result.to_dict("list") == {"value": [1]}
    assert captured == {
        "db_key": "analytics",
        "query": "SELECT 1",
        "print_queries": True,
        "retry_cnt": 2,
        "timeout_increment": 3.5,
        "query_label": "edge",
    }


def _valid_sql_source_stats() -> dict[str, int]:
    return {
        "row_count": 2,
        "null_user_rows": 0,
        "duplicate_user_rows": 0,
        "null_group_rows": 0,
        "control_rows": 1,
        "non_control_group_count": 1,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("row_count", 0, "at least one row"),
        ("null_user_rows", 1, "must not contain missing"),
        ("duplicate_user_rows", 1, "must contain unique"),
        ("null_group_rows", 1, "must not contain missing"),
        ("control_rows", 0, "was not found"),
        ("non_control_group_count", 0, "non-control group"),
    ],
)
def test_validate_sql_native_source_stats_reports_each_contract_failure(
    field: str,
    value: int,
    message: str,
) -> None:
    stats = _valid_sql_source_stats()
    stats[field] = value
    with pytest.raises(ValueError, match=message):
        sql_native._validate_sql_native_source_stats(
            pd.DataFrame([stats]),
            group="group_name",
            control="control",
            user_id="user_id",
        )


def test_validate_sql_native_source_stats_rejects_empty_result() -> None:
    with pytest.raises(ValueError, match="returned no rows"):
        sql_native._validate_sql_native_source_stats(
            pd.DataFrame(),
            group="group_name",
            control="control",
            user_id="user_id",
        )


def test_read_sql_native_groups_requires_group_name_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sql_native,
        "_read_sql_native_query",
        lambda **_kwargs: pd.DataFrame({"wrong": ["control"]}),
    )
    with pytest.raises(ValueError, match="did not return group_name"):
        sql_native._read_sql_native_groups(
            db_key="analytics",
            backend="gp",
            source_sql='"mart"."ab_source"',
            sql_where=None,
            group="group_name",
            print_queries=False,
            retry_cnt=1,
            timeout_increment=1,
            query_label=None,
        )


def test_sql_native_cuped_query_builds_mean_and_ratio_comparisons() -> None:
    metric_definitions = [
        {"kind": "mean", "metric_key": "orders", "column": "orders"},
        {
            "kind": "ratio",
            "metric_key": "ctr",
            "ratio_spec": {
                "numerator": "clicks",
                "denominator": "views",
                "level": "user",
            },
        },
    ]
    query = sql_native._build_sql_native_cuped_query(
        backend="gp",
        source_sql='"mart"."ab_source"',
        sql_where="active",
        pre_source_sql='"mart"."pre_source"',
        pre_sql_where=None,
        group="group_name",
        user_id="user_id",
        comparisons=[("test_a", "control"), ("test_b", "control")],
        metric_definitions=metric_definitions,
        outliers_quantile=0.99,
        outliers_policy="truncate",
    )
    assert query.count("WITH exp_raw AS") == 4
    assert query.count("UNION ALL") == 3
    assert "metric_pre" in query
    assert "test_a" in query
    assert "test_b" in query
    assert "clicks" in query
    assert "views" in query


def test_sql_native_observed_statistics_cover_aggregate_ratio_and_missing_groups() -> None:
    definition = {
        "kind": "ratio",
        "metric_key": "ctr",
        "ratio_spec": {
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
    }
    stats = pd.DataFrame(
        [
            {
                "metric_name": "ctr",
                "group_name": "control",
                "metric_value": 0.1,
                "variance_value": 0.01,
                "n": 10,
            },
            {
                "metric_name": "ctr",
                "group_name": "test",
                "metric_value": 0.2,
                "variance_value": 0.04,
                "n": 10,
            },
        ]
    )
    observed = sql_native._build_sql_native_observed_statistics(
        group_stats=stats,
        metric_definitions=[definition],
        comparisons=[("test", "control"), ("missing", "control")],
    )
    assert observed[("ctr", "test", "control")] == pytest.approx((0.1, math.sqrt(0.05)))
    assert all(math.isnan(value) for value in observed[("ctr", "missing", "control")])


def _mean_group_stats() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "metric_name": "orders",
                "group_name": "control",
                "metric_value": 1.0,
                "variance_value": 1.0,
                "n": 4,
                "outliers_cutoff": 5.0,
                "outliers_n": 0,
            },
            {
                "metric_name": "orders",
                "group_name": "test",
                "metric_value": 2.0,
                "variance_value": 1.0,
                "n": 4,
                "outliers_cutoff": 5.0,
                "outliers_n": 0,
            },
        ]
    )


def test_finalize_sql_native_result_fills_missing_cuped_and_bootstrap_fields() -> None:
    with pytest.warns(UserWarning, match="SQL CUPED stats are unavailable"):
        result = sql_native._finalize_sql_native_metric_result(
            group_stats=_mean_group_stats(),
            cuped_stats=None,
            bootstrap_stats=None,
            metric_definitions=[{"kind": "mean", "metric_key": "orders", "column": "orders"}],
            comparisons=[("test", "control")],
            mde_alpha=0.05,
            mde_power=0.8,
            include_cuped=True,
            include_bootstrap=True,
        )
    assert math.isnan(result.loc[0, "s.e. CUPED"])
    assert math.isnan(result.loc[0, "s.e. bootstrap"])
    assert math.isnan(result.loc[0, "bootstrap_adj_p"])


@pytest.mark.parametrize(
    ("cuped_row", "message"),
    [
        (None, "stats are unavailable"),
        (pd.Series({"pair_n": 0, "pre_var": 1.0}), "no overlapping"),
        (pd.Series({"pair_n": 2, "pre_var": 0.0}), "variance is not positive"),
        (
            pd.Series(
                {
                    "pair_n": 2,
                    "pre_var": 1.0,
                    "variance_control": 1.0,
                    "variance_test": 1.0,
                    "n0": 1,
                    "n1": 1,
                    "metric_control": 1.0,
                    "metric_test": 2.0,
                }
            ),
            "not enough overlapping",
        ),
    ],
)
def test_add_sql_native_cuped_fields_warns_for_unusable_summaries(
    cuped_row: pd.Series | None,
    message: str,
) -> None:
    row: dict[str, object] = {"metric_control": 1.0}
    with pytest.warns(UserWarning, match=message):
        sql_native._add_sql_native_cuped_fields(
            row=row,
            cuped_row=cuped_row,
            metric_name="orders",
            baseline_group="control",
            test_group="test",
            mde_alpha=0.05,
            mde_power=0.8,
        )
    assert math.isnan(float(row["s.e. CUPED"]))
    assert math.isnan(float(row["p-value CUPED"]))


def test_add_sql_native_cuped_fields_computes_valid_summary() -> None:
    row: dict[str, object] = {"metric_control": 1.0}
    sql_native._add_sql_native_cuped_fields(
        row=row,
        cuped_row=pd.Series(
            {
                "pair_n": 8,
                "pre_var": 1.0,
                "variance_control": 1.0,
                "variance_test": 1.0,
                "n0": 4,
                "n1": 4,
                "metric_control": 1.0,
                "metric_test": 2.0,
            }
        ),
        metric_name="orders",
        baseline_group="control",
        test_group="test",
        mde_alpha=0.05,
        mde_power=0.8,
    )
    assert row["s.e. CUPED"] == pytest.approx(math.sqrt(0.5))
    assert math.isfinite(float(row["p-value CUPED"]))
    assert math.isfinite(float(row["mde_abs CUPED"]))


def test_welch_summary_rejects_invalid_samples_and_zero_denominator() -> None:
    assert math.isnan(
        sql_native._compute_welch_p_value_from_summary(
            delta_abs=1.0,
            standard_error=math.nan,
            baseline_variance=1.0,
            baseline_n=4,
            test_variance=1.0,
            test_n=4,
        )
    )
    assert math.isnan(
        sql_native._compute_welch_p_value_from_summary(
            delta_abs=1.0,
            standard_error=1.0,
            baseline_variance=1.0,
            baseline_n=1,
            test_variance=1.0,
            test_n=4,
        )
    )
    assert math.isnan(
        sql_native._compute_welch_p_value_from_summary(
            delta_abs=1.0,
            standard_error=1.0,
            baseline_variance=0.0,
            baseline_n=4,
            test_variance=0.0,
            test_n=4,
        )
    )


def test_sql_native_task_runner_captures_errors_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_db_key: str, kwargs: Any, _labels: Any) -> pd.DataFrame:
        if kwargs["source"] == "bad":
            error = ValueError("bad source")
            raise error
        return pd.DataFrame({"value": [kwargs["source"]]})

    monkeypatch.setattr(sql_native, "_run_sql_native_task", fake_run)
    result = sql_native._compute_sql_native_metric_tasks(
        db_key="analytics",
        tasks={"good": {"source": "good"}, "bad": {"source": "bad"}},
        defaults={},
        concurrency=1,
        fail_fast=False,
        soft_concurrency_cap=None,
        hard_concurrency_cap=4,
        progress=False,
    )
    assert result["good"].to_dict("list") == {"value": ["good"]}
    assert result["bad"] == "bad source"

    with pytest.raises(ValueError, match="bad source"):
        sql_native._compute_sql_native_metric_tasks(
            db_key="analytics",
            tasks={"bad": {"source": "bad"}},
            defaults={},
            concurrency=1,
            fail_fast=True,
            soft_concurrency_cap=None,
            hard_concurrency_cap=4,
            progress=False,
        )


def test_sql_native_task_runner_handles_parallel_results_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_db_key: str, kwargs: Any, _labels: Any) -> pd.DataFrame:
        if kwargs["source"] == "bad":
            error = ValueError("bad source")
            raise error
        return pd.DataFrame({"value": [kwargs["source"]]})

    monkeypatch.setattr(sql_native, "_run_sql_native_task", fake_run)
    result = sql_native._compute_sql_native_metric_tasks(
        db_key="analytics",
        tasks={"first": {"source": "one"}, "bad": {"source": "bad"}},
        defaults={},
        concurrency=2,
        fail_fast=False,
        soft_concurrency_cap=2,
        hard_concurrency_cap=2,
        progress=False,
    )
    assert list(result) == ["first", "bad"]
    assert result["first"].to_dict("list") == {"value": ["one"]}
    assert result["bad"] == "bad source"

    with pytest.raises(ValueError, match="bad source"):
        sql_native._compute_sql_native_metric_tasks(
            db_key="analytics",
            tasks={"first": {"source": "one"}, "bad": {"source": "bad"}},
            defaults={},
            concurrency=2,
            fail_fast=True,
            soft_concurrency_cap=2,
            hard_concurrency_cap=2,
            progress=False,
        )


def test_sql_native_task_runner_enforces_effective_hard_cap() -> None:
    with pytest.raises(ValueError, match="exceeds hard_concurrency_cap"):
        sql_native._compute_sql_native_metric_tasks(
            db_key="analytics",
            tasks={"one": {"source": "one"}},
            defaults={},
            concurrency=3,
            fail_fast=True,
            soft_concurrency_cap=3,
            hard_concurrency_cap=2,
            progress=False,
        )


@pytest.mark.parametrize(
    ("tasks", "error", "message"),
    [
        ([], TypeError, "non-empty mapping"),
        ({}, ValueError, "must not be empty"),
        ({1: {"source": "one"}}, ValueError, "non-empty strings"),
        ({"one": "bad"}, TypeError, "must be a mapping"),
        ({"one": {"source": "one", "unknown": 1}}, TypeError, "unexpected field"),
        ({"one": {"source": None}}, ValueError, "must define source"),
        ({"one": {"source": "one", "labels": []}}, TypeError, "labels must be a mapping"),
    ],
)
def test_validate_sql_native_tasks_rejects_invalid_mappings(
    tasks: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        sql_native._validate_sql_native_tasks(tasks, defaults={})


def test_validate_sql_native_tasks_normalizes_none_labels() -> None:
    assert sql_native._validate_sql_native_tasks(
        {"one": {"source": "one", "labels": None}},
        defaults={},
    ) == [("one", {"source": "one"}, {})]


def test_run_sql_native_task_handles_no_labels_and_label_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame({"metric_name": ["orders"]})
    monkeypatch.setattr(
        sql_native,
        "_compute_test_metrics_sql_native_single",
        lambda **_kwargs: frame,
    )
    assert sql_native._run_sql_native_task("analytics", {}, {}) is frame
    with pytest.raises(ValueError, match="conflict with result columns"):
        sql_native._run_sql_native_task(
            "analytics",
            {},
            {"metric_name": "override"},
        )


def test_sql_string_literal_escapes_quotes() -> None:
    assert sql_native._sql_string_literal("O'Brien") == "'O''Brien'"
