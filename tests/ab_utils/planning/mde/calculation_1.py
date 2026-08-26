from __future__ import annotations

from tests.ab_utils._support.metrics import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    RatioMetricSpec,
    _compute_mde_from_standard_error,
    _manual_cuped_adjusted_variance,
    _single_metric_row,
    compute_mde,
    math,
    pd,
    planning_module,
    pytest,
)


def test_mde_aggregation_name_and_query_policy_failures() -> None:
    assert (
        planning_module._validate_mde_aggregation_metric_names(
            values=None, name="metrics", metric_columns={"a"}
        )
        == set()
    )
    with pytest.raises(ValueError, match="duplicates"):
        planning_module._validate_mde_aggregation_metric_names(
            values=["a", "a"], name="metrics", metric_columns={"a"}
        )
    with pytest.raises(ValueError, match="unknown metric"):
        planning_module._validate_mde_aggregation_metric_names(
            values=["b"], name="metrics", metric_columns={"a"}
        )
    with pytest.raises(AssertionError, match="Unexpected MDE aggregation policy"):
        planning_module._build_sql_mde_user_window_query(
            backend="gp",
            source='"public"."events"',
            sql_where=None,
            user_id="user",
            date_column="dt",
            columns=["metric"],
            aggregation_policies={"metric": "median"},
            start_date=pd.Timestamp("2026-01-01"),
            days=7,
        )


def test_compute_mde_estimates_mean_metric_from_user_day_window() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                ]
            ),
            "orders": [
                1.0,
                2.0,
                3.0,
                4.0,
                3.0,
                4.0,
                6.0,
                8.0,
                5.0,
                6.0,
                10.0,
                13.0,
            ],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        control_share=0.6,
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "orders")
    pre_values = pd.Series([3.0, 7.0, 11.0])
    user_values = pd.Series([7.0, 14.0, 23.0])
    expected_variance = float(user_values.var(ddof=1))
    expected_se = math.sqrt((expected_variance / 6) + (expected_variance / 4))
    expected_mde = _compute_mde_from_standard_error(
        standard_error=expected_se,
        alpha=DEFAULT_ALPHA,
        power=DEFAULT_POWER,
    )
    expected_cuped_variance = _manual_cuped_adjusted_variance(
        metric_exp=user_values,
        metric_pre=pre_values,
    )
    expected_cuped_se = math.sqrt((expected_cuped_variance / 6) + (expected_cuped_variance / 4))
    expected_cuped_mde = _compute_mde_from_standard_error(
        standard_error=expected_cuped_se,
        alpha=DEFAULT_ALPHA,
        power=DEFAULT_POWER,
    )
    assert list(result.columns) == [
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
    ]
    assert row["avg"] == pytest.approx(float(user_values.mean()))
    assert row["var"] == pytest.approx(expected_variance)
    assert row["days"] == 2
    assert row["pre_exp_days"] == 2
    assert row["group_size"] == 10
    assert row["control_share"] == pytest.approx(0.6)
    assert row["mde_abs"] == pytest.approx(expected_mde)
    assert row["mde_relative"] == pytest.approx(expected_mde / float(user_values.mean()))
    assert row["mde_abs_cuped"] == pytest.approx(expected_cuped_mde)
    assert row["mde_relative_cuped"] == pytest.approx(
        expected_cuped_mde / float(user_values.mean())
    )


def test_compute_mde_accepts_explicit_list_grids_sorted_unique() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 2),
            "orders": [1.0, 2.0, 3.0, 4.0, 3.0, 4.0, 5.0, 6.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[20, 10, 20],
        exp_days=[2, 1, 1],
        start_dt="2024-01-03",
    )

    assert result["days"].tolist() == [1, 1, 2, 2]
    assert result["group_size"].tolist() == [10, 20, 10, 20]


def test_compute_mde_accepts_min_max_step_grids() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2],
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-06",
                ]
                * 2
            ),
            "orders": [
                1.0,
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                7.0,
            ],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        min_group_size=10,
        max_group_size=25,
        group_size_step=10,
        min_days=1,
        max_days=3,
        days_step=2,
        start_dt="2024-01-04",
    )

    assert result["days"].tolist() == [1, 1, 3, 3]
    assert result["group_size"].tolist() == [10, 20, 10, 20]


def test_compute_mde_accepts_max_aggregation_for_mean_metric() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["converted"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        max_agg_metrics=["converted"],
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "converted")
    expected_values = pd.Series([1.0, 0.0, 1.0])
    assert row["avg"] == pytest.approx(float(expected_values.mean()))
    assert row["var"] == pytest.approx(float(expected_values.var(ddof=1)))


def test_compute_mde_default_non_zero_truncate_keeps_sparse_positive_values() -> None:
    user_ids = list(range(1, 11))
    df = pd.DataFrame(
        {
            "user_id": user_ids + user_ids,
            "dt": pd.to_datetime(["2024-01-01"] * 10 + ["2024-01-02"] * 10),
            "revenue": [0.0] * 9 + [100.0] + [0.0] * 9 + [100.0],
        }
    )

    default_result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["revenue"],
        group_sizes=[10],
        exp_days=[1],
        start_dt="2024-01-02",
        outliers_quantile=0.8,
    )
    with pytest.warns(UserWarning, match="pre-experiment covariate variance"):
        truncate_result = compute_mde(
            df,
            user_id="user_id",
            metric_columns=["revenue"],
            group_sizes=[10],
            exp_days=[1],
            start_dt="2024-01-02",
            outliers_quantile=0.8,
            outliers_policy="truncate",
        )

    default_row = _single_metric_row(default_result, "revenue")
    truncate_row = _single_metric_row(truncate_result, "revenue")
    assert default_row["avg"] == pytest.approx(10.0)
    assert default_row["var"] > 0
    assert not math.isnan(float(default_row["mde_abs"]))
    assert truncate_row["avg"] == pytest.approx(0.0)
    assert truncate_row["var"] == pytest.approx(0.0)
    assert math.isnan(float(truncate_row["mde_abs"]))


def test_compute_mde_sum_aggregation_list_makes_other_metrics_use_max() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "orders": [
                1.0,
                2.0,
                3.0,
                4.0,
                3.0,
                4.0,
                6.0,
                8.0,
                5.0,
                6.0,
                10.0,
                13.0,
            ],
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders", "converted"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        sum_agg_metrics=["orders"],
        outliers_quantile=1,
    )

    orders_row = _single_metric_row(result, "orders")
    converted_row = _single_metric_row(result, "converted")
    assert orders_row["avg"] == pytest.approx(float(pd.Series([7.0, 14.0, 23.0]).mean()))
    assert converted_row["avg"] == pytest.approx(float(pd.Series([1.0, 0.0, 1.0]).mean()))


def test_compute_mde_accepts_ratio_spec_dataclass_for_user_ratio() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "clicks": [
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                2.0,
                2.0,
                5.0,
                5.0,
                3.0,
                3.0,
            ],
            "impressions": [10.0] * 12,
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="ctr_user",
                numerator="clicks",
                denominator="impressions",
                level="user",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "ctr_user")
    ratio_values = pd.Series([0.1, 0.2, 0.3])
    assert row["avg"] == pytest.approx(float(ratio_values.mean()))
    assert row["var"] == pytest.approx(float(ratio_values.var(ddof=1)))
    assert not math.isnan(float(row["mde_abs_cuped"]))
    assert not math.isnan(float(row["mde_relative_cuped"]))


def test_compute_mde_applies_aggregation_policy_to_user_ratio_components() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            "visits": [10.0] * 12,
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="conversion_rate",
                numerator="converted",
                denominator="visits",
                level="user",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        max_agg_metrics=["converted"],
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "conversion_rate")
    ratio_values = pd.Series([1.0 / 20.0, 0.0, 1.0 / 20.0])
    assert row["avg"] == pytest.approx(float(ratio_values.mean()))
    assert row["var"] == pytest.approx(float(ratio_values.var(ddof=1)))
