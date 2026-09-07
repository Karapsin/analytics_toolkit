from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.ab_utils import compute_test_metrics
from analytics_toolkit.ab_utils.cuped import _compute_cuped_statistics_from_frame
from analytics_toolkit.ab_utils.outliers import _count_outliers_by_group
from analytics_toolkit.ab_utils.rows import _build_metric_row


@pytest.mark.parametrize("size", [262143, 262144])
def test_nullable_groups_match_object_total_and_segments(size: int) -> None:
    rng = np.random.default_rng(73)
    users = np.arange(size)
    pre_values = rng.integers(1, 100, size=size)
    frame = pd.DataFrame(
        {
            "user_id": users,
            "group_name": pd.array(np.where(users % 2, "daily_offer", "control"), dtype="string"),
            "segment": np.where(users % 4 < 2, "one", "two"),
            "revenue": pd.array(pre_values + rng.integers(0, 30, size=size), dtype="Int64"),
        },
        index=users * 3 + 7,
    )
    pre = pd.DataFrame({"user_id": users, "revenue": pd.array(pre_values, dtype="Int64")})
    pre["group_name"] = frame["group_name"].to_numpy(dtype=object)
    cuped_frame = frame[["group_name", "revenue"]].rename(columns={"revenue": "metric_exp"})
    cuped_frame["metric_pre"] = pre_values
    nullable_stats = _compute_cuped_statistics_from_frame(
        cuped_frame, "group_name", "control", "daily_offer"
    )
    object_stats = _compute_cuped_statistics_from_frame(
        cuped_frame.astype({"group_name": object}), "group_name", "control", "daily_offer"
    )
    assert nullable_stats[2] is None
    assert nullable_stats[:2] == pytest.approx(object_stats[:2])
    frame.iloc[5, frame.columns.get_loc("revenue")] = pd.NA
    pre.iloc[9, pre.columns.get_loc("revenue")] = pd.NA
    pre = pre.sample(frac=1, random_state=19)
    pre.index = np.arange(size) * 5 + 11
    original, original_pre = frame.copy(deep=True), pre.copy(deep=True)
    kwargs = {
        "segment": "segment",
        "pre_exp_metrics_df": pre,
        "test_vs_test": False,
        "outliers_quantile": 1,
    }
    actual = compute_test_metrics(frame, **kwargs)
    expected = compute_test_metrics(frame.astype({"group_name": object}), **kwargs)
    pd.testing.assert_frame_equal(actual, expected)
    assert actual["segment"].tolist() == ["TOTAL", "one", "two"]
    assert np.isfinite(actual[["p-value CUPED", "s.e. CUPED"]].to_numpy(dtype=float)).all()
    for segment in ["one", "two"]:
        isolated = compute_test_metrics(
            frame.loc[frame["segment"].eq(segment)].drop(columns="segment"),
            pre_exp_metrics_df=pre,
            test_vs_test=False,
            outliers_quantile=1,
        )
        pd.testing.assert_frame_equal(
            actual.loc[actual["segment"].eq(segment)]
            .drop(columns="segment")
            .reset_index(drop=True),
            isolated,
        )
    pd.testing.assert_frame_equal(frame, original)
    pd.testing.assert_frame_equal(pre, original_pre)


def test_group_selection_does_not_use_nullable_series_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {
            "group_name": pd.array(["control"] * 3 + ["test"] * 3, dtype="string"),
            "metric_exp": [1.0, 4.0, 2.0, 6.0, 3.0, 8.0],
            "metric_pre": [1.0, 2.0, 3.0, 2.0, 4.0, 3.0],
        },
        index=[9, 3, 7, 1, 6, 2],
    )
    expected = _compute_cuped_statistics_from_frame(frame, "group_name", "control", "test")
    original_eq = pd.Series.__eq__

    def broken_eq(self: pd.Series, other: object) -> pd.Series:
        if isinstance(self.dtype, pd.StringDtype):
            return pd.Series(pd.NA, index=self.index, dtype="boolean")
        return original_eq(self, other)

    monkeypatch.setattr(pd.Series, "__eq__", broken_eq)
    actual = _compute_cuped_statistics_from_frame(frame, "group_name", "control", "test")
    assert actual == expected
    mask = pd.Series([True, False, True, False, True, False], index=frame.index)
    assert _count_outliers_by_group(mask, frame["group_name"], "control", "test") == (2, 1)
    definitions = [
        {"kind": "mean", "metric_key": "metric_exp", "column": "metric_exp"},
        *[
            {
                "kind": "ratio",
                "metric_key": "ratio",
                "ratio_spec": {
                    "name": "ratio",
                    "numerator": "metric_exp",
                    "denominator": "metric_pre",
                    "level": level,
                    "invalid_denominator": "ignore",
                },
            }
            for level in ["user", "agg"]
        ],
    ]
    for definition in definitions:
        kwargs = {
            "group_column": "group_name",
            "baseline_group": "control",
            "test_group": "test",
            "metric_definition": definition,
            "mde_alpha": 0.05,
            "mde_power": 0.8,
        }
        actual_row = _build_metric_row(df=frame, **kwargs)
        expected_row = _build_metric_row(df=frame.astype({"group_name": object}), **kwargs)
        pd.testing.assert_series_equal(pd.Series(actual_row), pd.Series(expected_row))


@pytest.mark.parametrize(
    ("exp", "pre", "reason"),
    [
        ([1.0, 2.0, 3.0, 4.0], [1.0, 1.0, 1.0, 1.0], "covariate variance"),
        ([1.0, 2.0, 3.0, 4.0], [math.nan] * 4, "covariate variance"),
        ([1.0, math.inf, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], "adjusted metric values are not finite"),
        ([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], "t-test result is undefined"),
    ],
)
def test_cuped_failure_reasons(exp: list[float], pre: list[float], reason: str) -> None:
    frame = pd.DataFrame(
        {"group": ["control"] * 2 + ["test"] * 2, "metric_exp": exp, "metric_pre": pre}
    )
    p_value, standard_error, error = _compute_cuped_statistics_from_frame(
        frame, "group", "control", "test"
    )
    assert math.isnan(p_value)
    assert math.isnan(standard_error)
    assert reason in str(error)
