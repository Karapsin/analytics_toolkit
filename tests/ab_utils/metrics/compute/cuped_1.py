from __future__ import annotations

from tests.ab_utils._support.metrics import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    _build_comparisons,
    _build_metric_definitions,
    _build_metric_row,
    _build_outlier_contexts,
    _compute_cuped_statistics,
    _compute_cuped_statistics_from_frame,
    _compute_mde_from_standard_error,
    _frame_cuped_kwargs,
    _manual_cuped_statistics_from_frame,
    _normalize_ratio_metrics,
    _prepare_cuped_context,
    _prepare_metric_context,
    compute_test_metrics,
    math,
    pd,
    planning_module,
    pytest,
    warnings,
)


def test_prepared_metric_context_matches_legacy_rows_and_cuped() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": [
                "control",
                "control",
                "control",
                "test_a",
                "test_a",
                "test_a",
                "test_b",
                "test_b",
                "test_b",
            ],
            "orders": [10, 11, 12, 13, 14, 15, 16, 17, 18],
            "clicks": [1, 2, 3, 4, 5, 6, 7, 8, 9],
            "impressions": [10, 10, 10, 10, 10, 10, 10, 10, 10],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": [
                "control",
                "control",
                "control",
                "test_a",
                "test_a",
                "test_a",
                "test_b",
                "test_b",
                "test_b",
            ],
            "orders": [8, 9, 11, 12, 13, 14, 15, 16, 17],
            "clicks": [1, 1, 2, 3, 4, 5, 6, 7, 8],
            "impressions": [9, 10, 10, 10, 10, 11, 10, 10, 10],
        }
    )
    ratio_metrics = [
        {"name": "ctr_user", "numerator": "clicks", "denominator": "impressions", "level": "user"},
        {"name": "ctr_agg", "numerator": "clicks", "denominator": "impressions", "level": "agg"},
    ]
    metric_columns = ["orders"]
    metric_definitions = _build_metric_definitions(
        metric_columns,
        _normalize_ratio_metrics(
            df,
            ratio_metrics,
            reserved_columns={"group_name", "user_id"},
        ),
    )
    group_values = df["group_name"].to_numpy()
    group_masks = {
        group_name: group_values == group_name
        for group_name in df["group_name"].drop_duplicates().tolist()
    }
    comparisons = _build_comparisons(
        df["group_name"].drop_duplicates().tolist(),
        "control",
        test_vs_test=True,
    )
    outlier_contexts = _build_outlier_contexts(
        df=df,
        metric_definitions=metric_definitions,
        outliers_quantile=0.9,
        outliers_policy="truncate",
    )
    pre_outlier_contexts = _build_outlier_contexts(
        df=pre_df,
        metric_definitions=metric_definitions,
        outliers_quantile=0.9,
        outliers_policy="truncate",
        allow_missing=True,
    )

    for metric_definition in metric_definitions:
        metric_key = str(metric_definition["metric_key"])
        prepared_metric_context = _prepare_metric_context(
            df=df,
            metric_definition=metric_definition,
            outlier_context=outlier_contexts[metric_key],
        )
        prepared_cuped_context = _prepare_cuped_context(
            df=df,
            pre_exp_metrics_df=pre_df,
            user_id_column="user_id",
            metric_definition=metric_definition,
            outlier_context=outlier_contexts[metric_key],
            pre_outlier_context=pre_outlier_contexts.get(metric_key),
        )

        for test_group, baseline_group in comparisons:
            legacy_row = _build_metric_row(
                df=df,
                group_column="group_name",
                baseline_group=baseline_group,
                test_group=test_group,
                metric_definition=metric_definition,
                mde_alpha=DEFAULT_ALPHA,
                mde_power=DEFAULT_POWER,
                outlier_context=outlier_contexts[metric_key],
            )
            prepared_row = _build_metric_row(
                df=df,
                group_column="group_name",
                baseline_group=baseline_group,
                test_group=test_group,
                metric_definition=metric_definition,
                mde_alpha=DEFAULT_ALPHA,
                mde_power=DEFAULT_POWER,
                outlier_context=outlier_contexts[metric_key],
                prepared_metric_context=prepared_metric_context,
                group_masks=group_masks,
            )
            pd.testing.assert_series_equal(
                pd.Series(prepared_row).sort_index(),
                pd.Series(legacy_row).sort_index(),
                check_dtype=False,
            )

            comparison_frame = df.loc[
                group_masks[test_group] | group_masks[baseline_group],
                ["user_id", "group_name"],
            ].copy()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=UserWarning)
                legacy_cuped = _compute_cuped_statistics(
                    df=df,
                    pre_exp_metrics_df=pre_df,
                    group_column="group_name",
                    user_id_column="user_id",
                    baseline_group=baseline_group,
                    test_group=test_group,
                    metric_definition=metric_definition,
                    outlier_context=outlier_contexts[metric_key],
                    pre_outlier_context=pre_outlier_contexts.get(metric_key),
                )
                prepared_cuped = _compute_cuped_statistics(
                    df=df,
                    pre_exp_metrics_df=pre_df,
                    group_column="group_name",
                    user_id_column="user_id",
                    baseline_group=baseline_group,
                    test_group=test_group,
                    metric_definition=metric_definition,
                    outlier_context=outlier_contexts[metric_key],
                    pre_outlier_context=pre_outlier_contexts.get(metric_key),
                    prepared_cuped_context={
                        **prepared_cuped_context,
                        "comparison_frame": comparison_frame,
                    },
                )
            assert prepared_cuped[0] == pytest.approx(legacy_cuped[0], nan_ok=True)
            assert prepared_cuped[1] == pytest.approx(legacy_cuped[1], nan_ok=True)


@pytest.mark.parametrize(
    ("stats", "unavailable", "reason", "variance"),
    [
        ({}, "missing pre window", "missing pre window", math.nan),
        ({"cuped_pair_n": 1}, None, "not enough overlapping", math.nan),
        ({"cuped_pair_n": 2, "cuped_pre_var": 0}, None, "not positive", math.nan),
        ({"cuped_pair_n": 2, "cuped_pre_var": math.nan}, None, "not positive", math.nan),
        (
            {"cuped_pair_n": 2, "cuped_pre_var": 1, "cuped_adjusted_var": math.nan},
            None,
            "not enough adjusted",
            math.nan,
        ),
        (
            {"cuped_pair_n": 2, "cuped_pre_var": 1, "cuped_adjusted_var": 0.25},
            None,
            None,
            0.25,
        ),
    ],
)
def test_resolve_sql_native_cuped_results(
    stats: dict[str, object], unavailable: str | None, reason: str | None, variance: float
) -> None:
    actual_variance, actual_reason = planning_module._resolve_sql_native_cuped_result(
        stats=stats, unavailable_reason=unavailable
    )
    if math.isnan(variance):
        assert math.isnan(actual_variance)
    else:
        assert actual_variance == variance
    assert reason is None if actual_reason is None else reason in actual_reason


def test_frame_cuped_reports_missing_pre_start() -> None:
    variance, reason = planning_module._compute_mde_cuped_variance(
        df=pd.DataFrame({"user": [1], "dt": [pd.Timestamp("2026-01-01")], "metric": [1.0]}),
        date_column="dt",
        user_id="user",
        metric_definition={"kind": "mean", "metric_key": "metric", "column": "metric"},
        outcome_user_metric_df=pd.DataFrame({"user": [1], "metric": [1.0]}),
        outcome_outlier_context=None,
        pre_start_date=None,
        pre_days=1,
        unavailable_reason=None,
        outliers_quantile=0.99,
        outliers_policy="truncate",
        aggregation_policies={"metric": "sum"},
    )
    assert math.isnan(variance)
    assert reason == "pre-experiment window is unavailable"


def test_frame_cuped_reports_missing_frame_and_metric_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _frame_cuped_kwargs()
    kwargs["pre_user_metric_df"] = None
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert reason == "pre-experiment window is unavailable"

    kwargs["pre_user_metric_df"] = pd.DataFrame({"user": [1, 2], "metric": [1.0, 3.0]})
    monkeypatch.setattr(
        planning_module,
        "_build_metric_values_by_user",
        lambda **_kwargs: (pd.DataFrame(), "bad experiment"),
    )
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "experiment metric values are unavailable" in str(reason)

    calls = iter(
        [
            (pd.DataFrame({"user": [1, 2], "metric_exp": [2.0, 4.0]}), None),
            (pd.DataFrame(), "bad pre"),
        ]
    )
    monkeypatch.setattr(
        planning_module, "_build_metric_values_by_user", lambda **_kwargs: next(calls)
    )
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "pre-experiment metric values are unavailable" in str(reason)


def test_frame_cuped_overlap_prevariance_adjusted_and_valid_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _frame_cuped_kwargs()
    kwargs["pre_user_metric_df"] = pd.DataFrame({"user": [2, 3], "metric": [1.0, 2.0]})
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "not enough overlapping" in str(reason)

    kwargs = _frame_cuped_kwargs()
    kwargs["pre_user_metric_df"] = pd.DataFrame({"user": [1, 2], "metric": [1.0, 1.0]})
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "not positive" in str(reason)

    kwargs = _frame_cuped_kwargs()
    monkeypatch.setattr(planning_module, "_compute_sample_variance", lambda _values: math.nan)
    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(**kwargs)
    assert math.isnan(variance)
    assert "not enough adjusted" in str(reason)
    monkeypatch.undo()

    variance, reason = planning_module._compute_mde_cuped_variance_from_user_frames(
        **_frame_cuped_kwargs()
    )
    assert math.isfinite(variance)
    assert reason is None


def test_compute_test_metrics_adds_cuped_p_value_for_mean_metrics() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 11, 9, 12, 14, 15, 13, 16],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [8, 10, 6, 11, 12, 12, 10, 15],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    assert result.columns[result.columns.get_loc("p-value") + 1] == "s.e. CUPED"
    assert result.columns[result.columns.get_loc("s.e. CUPED") + 1] == "p-value CUPED"
    assert result.columns[result.columns.get_loc("p-value CUPED") + 1] == "mde_abs CUPED"
    assert result.columns[result.columns.get_loc("mde_abs CUPED") + 1] == "mde_relative CUPED"
    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    expected_mde_abs = _compute_mde_from_standard_error(
        standard_error=orders_row["s.e. CUPED"],
        alpha=DEFAULT_ALPHA,
        power=DEFAULT_POWER,
    )
    assert not math.isnan(float(orders_row["s.e. CUPED"]))
    assert not math.isnan(float(orders_row["p-value CUPED"]))
    assert orders_row["mde_abs CUPED"] == pytest.approx(expected_mde_abs)
    assert orders_row["mde_relative CUPED"] == pytest.approx(
        expected_mde_abs / orders_row["metric_group_2"]
    )


def test_compute_cuped_statistics_from_frame_matches_manual_cuped_welch_ttest() -> None:
    cuped_frame = pd.DataFrame(
        {
            "group_name": ["control"] * 4 + ["test"] * 4,
            "metric_exp": [11.0, 14.0, 13.0, 17.0, 16.0, 19.0, 18.0, 23.0],
            "metric_pre": [4.0, 6.0, 5.0, 9.0, 6.0, 8.0, 7.0, 12.0],
        }
    )

    expected_p_value, expected_standard_error = _manual_cuped_statistics_from_frame(
        cuped_frame=cuped_frame,
        group_column="group_name",
        baseline_group="control",
        test_group="test",
    )
    p_value, standard_error, reason = _compute_cuped_statistics_from_frame(
        cuped_frame=cuped_frame,
        group_column="group_name",
        baseline_group="control",
        test_group="test",
    )

    assert reason is None
    assert standard_error == pytest.approx(expected_standard_error)
    assert p_value == pytest.approx(expected_p_value)


def test_compute_test_metrics_cuped_uses_transformed_values() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 7)),
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [1, 2, 3, 4, 5, 100],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 7)),
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [10, 1, 8, 3, 6, 200],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
        outliers_quantile=0.8,
        outliers_policy="truncate",
    )

    cuped_frame = pd.DataFrame(
        {
            "group_name": df["group_name"],
            "metric_exp": [1.0, 2.0, 3.0, 4.0, 5.0, 5.0],
            "metric_pre": [10.0, 1.0, 8.0, 3.0, 6.0, 10.0],
        }
    )
    expected_p_value, expected_standard_error, reason = _compute_cuped_statistics_from_frame(
        cuped_frame=cuped_frame,
        group_column="group_name",
        baseline_group="control",
        test_group="test",
    )
    assert reason is None

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    assert orders_row["outliers_cutoff"] == pytest.approx(5.0)
    assert orders_row["outliers_n_group_1"] == 1
    assert orders_row["s.e. CUPED"] == pytest.approx(expected_standard_error)
    assert orders_row["p-value CUPED"] == pytest.approx(expected_p_value)
