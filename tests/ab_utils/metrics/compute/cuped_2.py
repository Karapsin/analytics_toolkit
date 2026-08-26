from __future__ import annotations

from tests.ab_utils._support.metrics import (
    _assert_cuped_row_matches_frame,
    _manual_agg_ratio_linearized_values,
    _single_metric_row,
    compute_test_metrics,
    math,
    np,
    pd,
    pytest,
)


def test_compute_test_metrics_adds_cuped_p_value_for_ratio_metrics() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [5, 6, 4, 5, 8, 9, 7, 8],
            "impressions": [10, 12, 8, 10, 12, 14, 10, 12],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [4, 5, 3, 4, 6, 7, 5, 6],
            "impressions": [9, 11, 8, 10, 11, 15, 9, 13],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=[
            {
                "name": "ctr_user",
                "numerator": "clicks",
                "denominator": "impressions",
                "level": "user",
            }
        ],
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    ratio_row = result[result["metric_name"] == "ctr_user"].iloc[0]
    assert ratio_row["metric_type"] == "ratio"
    assert not math.isnan(float(ratio_row["s.e. CUPED"]))
    assert not math.isnan(float(ratio_row["p-value CUPED"]))
    assert not math.isnan(float(ratio_row["mde_abs CUPED"]))
    assert not math.isnan(float(ratio_row["mde_relative CUPED"]))


def test_compute_test_metrics_cuped_warns_when_pre_variance_is_not_positive() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 12, 11, 13, 15, 17, 16, 18],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [5] * 8,
        }
    )

    with pytest.warns(UserWarning, match="pre-experiment covariate variance is not positive"):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = _single_metric_row(result, "orders")
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))
    assert math.isnan(float(orders_row["mde_abs CUPED"]))
    assert math.isnan(float(orders_row["mde_relative CUPED"]))


def test_compute_test_metrics_cuped_warns_when_no_overlapping_observations() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 12, 11, 13, 15, 17, 16, 18],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(101, 109)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [8, 9, 10, 11, 12, 13, 14, 15],
        }
    )

    with pytest.warns(
        UserWarning, match="no overlapping non-missing experiment/pre-experiment observations"
    ):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = _single_metric_row(result, "orders")
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))


def test_compute_test_metrics_cuped_warns_when_too_few_usable_observations() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [10.0, np.nan, 14.0, 15.0],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [8.0, 9.0, 12.0, 13.0],
        }
    )

    with pytest.warns(
        UserWarning, match="not enough overlapping observations to run the CUPED t-test"
    ):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = _single_metric_row(result, "orders")
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))


def test_compute_test_metrics_cuped_uses_only_overlapping_nonmissing_users() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4, 5, 6],
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [10.0, 11.0, 16.0, 14.0, 16.0, 16.0],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": [999, 6, 4, 5, 2, 1, 3, 1000],
            "group_name": [
                "control",
                "test",
                "test",
                "test",
                "control",
                "control",
                "control",
                "test",
            ],
            "orders": [16.0, 16.0, 14.0, np.nan, 9.0, 8.0, 16.0, 16.0],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    expected_frame = pd.DataFrame(
        {
            "group_name": ["control", "control", "control", "test", "test"],
            "metric_exp": [10.0, 11.0, 16.0, 14.0, 16.0],
            "metric_pre": [8.0, 9.0, 16.0, 14.0, 16.0],
        }
    )
    _assert_cuped_row_matches_frame(
        _single_metric_row(result, "orders"),
        expected_frame,
        baseline_group="control",
        test_group="test",
    )


def test_compute_test_metrics_cuped_user_ratio_matches_manual_frame() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [4, 5, 6, 9, 8, 14, 10, 18],
            "impressions": [10, 0, 12, 10, 16, 20, -1, 20],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [3, 3, 4, 7, 4, 10, 14, 9],
            "impressions": [10, 6, 0, 10, 10, 20, 20, 0],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=[
            {
                "name": "ctr_user",
                "numerator": "clicks",
                "denominator": "impressions",
                "level": "user",
            }
        ],
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    expected_frame = pd.DataFrame(
        {
            "group_name": ["control", "control", "test", "test"],
            "metric_exp": [0.4, 0.9, 0.5, 0.7],
            "metric_pre": [0.3, 0.7, 0.4, 0.5],
        }
    )
    _assert_cuped_row_matches_frame(
        _single_metric_row(result, "ctr_user"),
        expected_frame,
        baseline_group="control",
        test_group="test",
    )


def test_compute_test_metrics_cuped_agg_ratio_matches_manual_linearization() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [10, 11, 12, 13, 16, 16, 16, 16],
            "impressions": [100, 110, 120, 130, 100, 100, 100, 100],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "clicks": [9, 10, 11, 12, 13, 13, 13, 13],
            "impressions": [90, 100, 110, 120, 100, 100, 100, 100],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=[
            {
                "name": "ctr_agg",
                "numerator": "clicks",
                "denominator": "impressions",
                "level": "agg",
            }
        ],
        test_vs_test=False,
        pre_exp_metrics_df=pre_df,
    )

    expected_frame = pd.DataFrame(
        {
            "group_name": df["group_name"],
            "metric_exp": _manual_agg_ratio_linearized_values(
                df["clicks"].astype(float),
                df["impressions"].astype(float),
            ),
            "metric_pre": _manual_agg_ratio_linearized_values(
                pre_df["clicks"].astype(float),
                pre_df["impressions"].astype(float),
            ),
        }
    )
    _assert_cuped_row_matches_frame(
        _single_metric_row(result, "ctr_agg"),
        expected_frame,
        baseline_group="control",
        test_group="test",
    )


def test_compute_test_metrics_cuped_matches_each_multi_group_comparison() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": ["control"] * 3 + ["test_a"] * 3 + ["test_b"] * 3,
            "orders": [10.0, 11.0, 16.0, 13.0, 15.0, 16.0, 9.0, 12.0, 16.0],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 10)),
            "group_name": ["control"] * 3 + ["test_a"] * 3 + ["test_b"] * 3,
            "orders": [5.0, 7.0, 16.0, 8.0, 9.0, 16.0, 4.0, 6.0, 16.0],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=True,
        pre_exp_metrics_df=pre_df,
    )

    assert result.shape[0] == 3
    for test_group, baseline_group in [
        ("test_a", "control"),
        ("test_b", "control"),
        ("test_a", "test_b"),
    ]:
        comparison_mask = df["group_name"].isin([baseline_group, test_group])
        expected_frame = pd.DataFrame(
            {
                "group_name": df.loc[comparison_mask, "group_name"].to_numpy(),
                "metric_exp": df.loc[comparison_mask, "orders"].to_numpy(dtype=float),
                "metric_pre": pre_df.loc[comparison_mask, "orders"].to_numpy(dtype=float),
            }
        )
        _assert_cuped_row_matches_frame(
            _single_metric_row(result, "orders", group_1=test_group, group_2=baseline_group),
            expected_frame,
            baseline_group=baseline_group,
            test_group=test_group,
        )
