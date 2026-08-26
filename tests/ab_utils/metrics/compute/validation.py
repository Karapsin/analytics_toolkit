from __future__ import annotations

from tests.ab_utils._support.metrics import (
    compute_test_metrics,
    math,
    pd,
    pytest,
    warnings,
)


def test_compute_test_metrics_rejects_infinite_observed_standard_error() -> None:
    df = pd.DataFrame(
        {
            "user_id": range(4),
            "group_name": ["control", "control", "test", "test"],
            "value": [1e308, -1e308, 1e308, -1e308],
        }
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = compute_test_metrics(
            df,
            test_vs_test=False,
            multiple_comparisons_adjustment=True,
            multiple_comparisons_adjustment_resamples=9,
            bootstrap_random_state=1,
            outliers_quantile=1,
        )

    row = result.iloc[0]
    assert math.isinf(float(row["s.e."]))
    assert math.isnan(float(row["bootstrap_adj_p"]))


def test_compute_test_metrics_warns_and_sets_nan_when_pre_metric_is_missing() -> None:
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
            "gmv": [100, 110, 90, 120, 140, 150, 130, 160],
        }
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED p-value for metric 'orders'"):
        result = compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )

    orders_row = result[result["metric_name"] == "orders"].iloc[0]
    assert math.isnan(float(orders_row["s.e. CUPED"]))
    assert math.isnan(float(orders_row["p-value CUPED"]))


def test_compute_test_metrics_validates_pre_experiment_group_assignments() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 5)),
            "group_name": ["control", "control", "test", "test"],
            "orders": [10, 11, 14, 15],
        }
    )
    pre_df = pd.DataFrame(
        {
            "user_id": list(range(1, 5)),
            "group_name": ["control", "test", "test", "test"],
            "orders": [8, 9, 12, 13],
        }
    )

    with pytest.raises(ValueError, match="must match between df and pre_exp_metrics_df"):
        compute_test_metrics(
            df,
            control="control",
            test_vs_test=False,
            pre_exp_metrics_df=pre_df,
        )
