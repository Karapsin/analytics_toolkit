from __future__ import annotations

from tests.ab_utils._support.metrics import (
    _build_sample_metrics_df,
    _single_metric_row,
    compute_mde,
    compute_mde_from_sql,
    compute_mde_sql_native,
    compute_test_metrics,
    inspect,
    pd,
    pytest,
)


def test_ab_metric_outlier_policy_defaults_to_non_zero_truncate() -> None:
    assert (
        inspect.signature(compute_test_metrics).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde).parameters["outliers_policy"].default == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde_from_sql).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )
    assert (
        inspect.signature(compute_mde_sql_native).parameters["outliers_policy"].default
        == "non_zero_truncate"
    )


def test_compute_test_metrics_drop_outliers_updates_counts() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 7)),
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "orders": [1, 2, 100, 3, 4, 200],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.8,
        outliers_policy="drop",
    )

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    assert orders_row["outliers_cutoff"] == pytest.approx(float(df["orders"].quantile(0.8)))
    assert orders_row["outliers_n_group_2"] == 0
    assert orders_row["outliers_n_group_1"] == 1
    assert orders_row["n_group_2"] == 3
    assert orders_row["n_group_1"] == 2
    assert orders_row["metric_group_1"] == pytest.approx((3 + 4) / 2)


def test_compute_test_metrics_uses_global_outlier_cutoff_across_groups() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [1, 2, 100, 200],
        }
    )

    result = compute_test_metrics(
        df,
        control="control",
        test_vs_test=False,
        outliers_quantile=0.75,
    )

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    cutoff = float(df["orders"].quantile(0.75))
    assert cutoff == pytest.approx(125.0)
    assert orders_row["outliers_cutoff"] == pytest.approx(cutoff)
    assert orders_row["metric_group_1"] == pytest.approx((100 + cutoff) / 2)


def test_compute_test_metrics_user_ratio_outliers_truncate_and_drop() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "clicks": [1, 2, 3, 100],
            "impressions": [10, 10, 10, 10],
        }
    )
    ratio_metrics = [
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "impressions",
            "level": "user",
        }
    ]

    truncate_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="truncate",
    )
    drop_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="drop",
    )

    cutoff = float(pd.Series([0.1, 0.2, 0.3, 10.0]).quantile(0.75))
    truncate_row = truncate_result[truncate_result["metric_name"] == "ctr_user"].iloc[0]
    drop_row = drop_result[drop_result["metric_name"] == "ctr_user"].iloc[0]
    assert truncate_row["outliers_cutoff"] == pytest.approx(cutoff)
    assert truncate_row["outliers_n_group_1"] == 1
    assert truncate_row["metric_group_1"] == pytest.approx((0.3 + cutoff) / 2)
    assert truncate_row["n_group_1"] == 2
    assert drop_row["metric_group_1"] == pytest.approx(0.3)
    assert drop_row["n_group_1"] == 1


def test_compute_test_metrics_agg_ratio_outliers_drop_and_truncate() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "clicks": [1, 2, 3, 100],
            "impressions": [10, 10, 10, 10],
        }
    )
    ratio_metrics = [{"name": "ctr", "numerator": "clicks", "denominator": "impressions"}]

    truncate_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="truncate",
    )
    drop_result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.75,
        outliers_policy="drop",
    )

    cutoff = float(pd.Series([0.1, 0.2, 0.3, 10.0]).quantile(0.75))
    truncate_row = truncate_result[truncate_result["metric_name"] == "ctr"].iloc[0]
    drop_row = drop_result[drop_result["metric_name"] == "ctr"].iloc[0]
    assert truncate_row["outliers_cutoff"] == pytest.approx(cutoff)
    assert truncate_row["outliers_n_group_1"] == 1
    assert truncate_row["metric_group_1"] == pytest.approx((3 + cutoff * 10) / 20)
    assert truncate_row["n_group_1"] == 2
    assert drop_row["metric_group_1"] == pytest.approx(3 / 10)
    assert drop_row["n_group_1"] == 1


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    [
        (
            {"outliers_quantile": 0},
            ValueError,
            "outliers_quantile must be greater than 0 and less than or equal to 1",
        ),
        ({"outliers_quantile": True}, TypeError, "outliers_quantile must be numeric"),
        ({"outliers_quantile": "0.9"}, TypeError, "outliers_quantile must be numeric"),
        (
            {"outliers_policy": "winsorize"},
            ValueError,
            "outliers_policy must be 'truncate', 'drop', or 'non_zero_truncate'",
        ),
        ({"outliers_policy": None}, TypeError, "outliers_policy must be a string"),
    ],
)
def test_compute_test_metrics_validates_outlier_parameters(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    df = _build_sample_metrics_df()

    with pytest.raises(error_type, match=message):
        compute_test_metrics(df, **kwargs)


def test_compute_test_metrics_accepts_outliers_quantile_one_without_truncation() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4],
            "group_name": ["control", "control", "test", "test"],
            "orders": [10.0, 20.0, 30.0, 1000.0],
        }
    )

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        outliers_quantile=1,
    )

    orders_row = _single_metric_row(result, "orders")
    assert orders_row["outliers_cutoff"] == pytest.approx(1000.0)
    assert orders_row["outliers_n_group_2"] == 0
    assert orders_row["outliers_n_group_1"] == 0
    assert orders_row["metric_group_1"] == pytest.approx(515.0)
