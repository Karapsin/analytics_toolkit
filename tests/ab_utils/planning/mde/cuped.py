from __future__ import annotations

from tests.ab_utils._support.metrics import (
    _single_metric_row,
    compute_mde,
    math,
    pd,
    pytest,
)


def test_compute_mde_uses_explicit_pre_exp_days_for_cuped_window() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 2, 2, 2, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"] * 3),
            "orders": [1.0, 10.0, 10.0, 3.0, 20.0, 30.0, 6.0, 40.0, 60.0],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[12],
        exp_days=[2],
        start_dt="2024-01-02",
        pre_exp_days=1,
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "orders")
    expected_values = pd.Series([20.0, 50.0, 100.0])
    assert row["pre_exp_days"] == 1
    assert row["avg"] == pytest.approx(float(expected_values.mean()))
    assert not math.isnan(float(row["mde_abs_cuped"]))


def test_compute_mde_warns_and_returns_nan_when_cuped_window_is_unavailable() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 2, 2],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02"] * 2),
            "orders": [1.0, 2.0, 3.0, 4.0],
        }
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE"):
        result = compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[2],
            start_dt=None,
            outliers_quantile=1,
        )

    row = _single_metric_row(result, "orders")
    assert row["avg"] == pytest.approx(5.0)
    assert math.isnan(float(row["mde_abs_cuped"]))
    assert math.isnan(float(row["mde_relative_cuped"]))
