from __future__ import annotations

from tests.ab_utils._support.metrics import (
    _build_sample_metrics_df,
    _single_metric_row,
    compute_test_metrics,
    math,
    pd,
    planning_module,
    pytest,
)


def test_compute_test_metrics_prints_progress_logs(capsys: pytest.CaptureFixture[str]) -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [10, 11, 9, 12, 14, 15, 13, 16],
            "clicks": [5, 6, 4, 5, 8, 9, 7, 8],
            "impressions": [10, 12, 8, 10, 12, 14, 10, 12],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": ["control"] * 4 + ["test"] * 4,
            "orders": [8, 10, 6, 11, 12, 12, 10, 15],
            "clicks": [4, 5, 3, 4, 6, 7, 5, 6],
            "impressions": [9, 11, 8, 10, 11, 15, 9, 13],
        }
    )

    result = compute_test_metrics(
        df,
        ratio_metrics=[
            {"name": "ctr", "numerator": "clicks", "denominator": "impressions"},
        ],
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=3,
        bootstrap_progress=False,
        pre_exp_metrics_df=pre_df,
    )

    output = capsys.readouterr().out
    assert "compute_test_metrics: start rows=8 metric_columns=3" in output
    assert "ratio_metrics=True cuped=True bootstrap=True" in output
    assert "compute_test_metrics: setup complete groups=2 comparisons=1 metrics=4" in output
    assert "compute_test_metrics: building outlier contexts" in output
    assert "compute_test_metrics: outlier contexts complete" in output
    assert "compute_test_metrics: comparison test vs control" in output
    assert "compute_test_metrics: metric orders (mean)" in output
    assert "compute_test_metrics: metric ctr (ratio)" in output
    assert "compute_test_metrics: CUPED orders (mean)" in output
    assert "compute_test_metrics: bootstrap adjustment start resamples=3 n_jobs=1" in output
    assert "compute_test_metrics: bootstrap adjustment complete" in output
    assert f"compute_test_metrics: finish rows={len(result)}" in output


def test_compute_test_metrics_warns_when_no_family_resample_is_valid() -> None:
    df = pd.DataFrame(
        {
            "user_id": range(4),
            "group_name": ["control", "control", "test", "test"],
            "value": [0.0, 1.0, 0.2, 1.2],
        }
    )

    with pytest.warns(
        RuntimeWarning,
        match="Bootstrap discarded 1 of 1 resamples for metric 'value'",
    ):
        result = compute_test_metrics(
            df,
            test_vs_test=False,
            multiple_comparisons_adjustment=True,
            multiple_comparisons_adjustment_resamples=1,
            bootstrap_random_state=0,
            outliers_quantile=1,
        )

    assert math.isnan(float(result.iloc[0]["bootstrap_adj_p"]))
    assert math.isnan(float(result.iloc[0]["s.e. bootstrap"]))


def test_compute_test_metrics_aligns_group_position_columns() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(df, test_vs_test=False)

    assert result.columns.tolist()[:14] == [
        "metric_type",
        "group_1",
        "group_2",
        "metric_name",
        "n_group_1",
        "n_group_2",
        "outliers_cutoff",
        "outliers_n_group_1",
        "outliers_n_group_2",
        "metric_group_1",
        "metric_group_2",
        "variance_group_1",
        "variance_group_2",
        "delta_abs",
    ]
    assert result.columns[result.columns.get_loc("mde_relative") + 1] == "s.e."
    assert result.columns[result.columns.get_loc("s.e.") + 1] == "p-value"
    assert {
        "n0",
        "n1",
        "outliers_n_control",
        "outliers_n_test",
        "metric_control",
        "metric_test",
        "variance_control",
        "variance_test",
    }.isdisjoint(result.columns)

    orders_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "orders")
    ].iloc[0]
    orders_cutoff = float(df["orders"].quantile(0.999))
    assert orders_row["metric_type"] == "mean"
    assert orders_row["metric_group_2"] == pytest.approx((10 + 12 + 9) / 3)
    assert orders_row["metric_group_1"] == pytest.approx((13 + orders_cutoff + 11 + 14) / 4)
    assert orders_row["outliers_cutoff"] == pytest.approx(orders_cutoff)
    assert orders_row["outliers_n_group_2"] == 0
    assert orders_row["outliers_n_group_1"] == 1
    control_values = pd.Series([10, 12, 9], dtype=float)
    test_values = pd.Series([13, orders_cutoff, 11, 14], dtype=float)
    expected_control_variance = control_values.var(ddof=1)
    expected_test_variance = test_values.var(ddof=1)
    assert orders_row["variance_group_2"] == pytest.approx(expected_control_variance)
    assert orders_row["variance_group_1"] == pytest.approx(expected_test_variance)
    assert orders_row["s.e."] == pytest.approx(
        math.sqrt((expected_control_variance / 3) + (expected_test_variance / 4))
    )


def test_compute_test_metrics_uses_raw_relative_fields() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(df, test_vs_test=False)

    assert "delta_relative" in result.columns
    assert "mde_relative" in result.columns
    assert "mde_abs CUPED" not in result.columns
    assert "mde_relative CUPED" not in result.columns
    assert "uplift" not in result.columns
    assert "mde_percentage" not in result.columns

    orders_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "orders")
    ].iloc[0]
    orders_cutoff = float(df["orders"].quantile(0.999))
    expected_control = (10 + 12 + 9) / 3
    expected_test = (13 + orders_cutoff + 11 + 14) / 4
    expected_delta_abs = expected_test - expected_control
    assert orders_row["delta_relative"] == pytest.approx(expected_delta_abs / expected_control)


@pytest.mark.parametrize(("value", "expected"), [(None, 0), (math.nan, 0), ("7", 7), (8, 8)])
def test_coerce_sql_int_handles_nulls_and_delegates_to_int(value: object, expected: int) -> None:
    assert planning_module._coerce_sql_int(value) == expected


def test_compute_test_metrics_default_non_zero_truncate_ignores_zeros_for_cutoff() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 11)),
            "group_name": ["control"] * 5 + ["test"] * 5,
            "orders": [0.0] * 9 + [100.0],
        }
    )

    default_result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.8,
    )
    truncate_result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.8,
        outliers_policy="truncate",
    )

    default_row = _single_metric_row(default_result, "orders")
    truncate_row = _single_metric_row(truncate_result, "orders")
    assert default_row["outliers_cutoff"] == pytest.approx(100.0)
    assert default_row["outliers_n_group_1"] == 0
    assert default_row["metric_group_1"] == pytest.approx(20.0)
    assert truncate_row["outliers_cutoff"] == pytest.approx(0.0)
    assert truncate_row["outliers_n_group_1"] == 1
    assert truncate_row["metric_group_1"] == pytest.approx(0.0)


def test_compute_test_metrics_accepts_non_zero_truncate_explicitly() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        outliers_policy="non_zero_truncate",
    )

    assert not result.empty
