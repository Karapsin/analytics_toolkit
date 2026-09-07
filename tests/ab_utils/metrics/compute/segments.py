from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.ab_utils import compute_test_metrics


def _segmented_metrics_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": range(1, 13),
            "group_name": ["control"] * 3 + ["test"] * 3 + ["control"] * 3 + ["test"] * 3,
            "country_code": [2] * 6 + [1] * 6,
            "orders": [5.0, 7.0, 6.0, 8.0, 10.0, 9.0, 4.0, 5.0, 6.0, 7.0, 9.0, 8.0],
            "clicks": [2, 3, 2, 4, 5, 4, 1, 2, 2, 3, 4, 3],
            "views": [5, 6, 5, 7, 8, 7, 4, 5, 5, 6, 7, 6],
        }
    )


def _metric_kwargs() -> dict[str, object]:
    return {
        "ratio_metrics": [
            {"name": "ctr", "numerator": "clicks", "denominator": "views"},
        ],
        "test_vs_test": False,
        "outliers_quantile": 1,
    }


def test_compute_test_metrics_returns_total_and_observed_segment_slices() -> None:
    df = _segmented_metrics_df()

    result = compute_test_metrics(df, segment="country_code", **_metric_kwargs())

    assert result.columns[0] == "segment"
    assert result["segment"].drop_duplicates().tolist() == ["TOTAL", 2, 1]
    assert set(result["metric_name"]) == {"orders", "clicks", "views", "ctr"}

    expected_frames = [
        compute_test_metrics(df.drop(columns="country_code"), **_metric_kwargs()),
        compute_test_metrics(
            df.loc[df["country_code"].eq(2)].drop(columns="country_code"),
            **_metric_kwargs(),
        ),
        compute_test_metrics(
            df.loc[df["country_code"].eq(1)].drop(columns="country_code"),
            **_metric_kwargs(),
        ),
    ]
    for label, expected in zip(["TOTAL", 2, 1], expected_frames):
        actual = result.loc[result["segment"].eq(label)].drop(columns="segment")
        pd.testing.assert_frame_equal(actual.reset_index(drop=True), expected)


def test_compute_test_metrics_handles_pandas_string_segments() -> None:
    df = _segmented_metrics_df()
    df["country_code"] = pd.Series(
        ["QR 2/5"] * 6 + ["QR Only"] * 6,
        dtype="string",
    )

    result = compute_test_metrics(df, segment="country_code", **_metric_kwargs())

    assert result["segment"].drop_duplicates().tolist() == [
        "TOTAL",
        "QR 2/5",
        "QR Only",
    ]
    assert result.groupby("segment", sort=False).size().tolist() == [4, 4, 4]


def test_compute_test_metrics_segments_cuped_by_experiment_membership() -> None:
    df = _segmented_metrics_df()[["user_id", "group_name", "country_code", "orders"]]
    pre_exp_df = pd.DataFrame(
        {
            "user_id": range(1, 13),
            "group_name": df["group_name"],
            "orders": [4.0, 6.0, 5.0, 7.0, 8.0, 7.0, 3.0, 4.0, 5.0, 6.0, 7.0, 6.0],
        }
    )

    result = compute_test_metrics(
        df,
        segment="country_code",
        pre_exp_metrics_df=pre_exp_df,
        test_vs_test=False,
        outliers_quantile=1,
    )
    expected = compute_test_metrics(
        df.loc[df["country_code"].eq(2)].drop(columns="country_code"),
        pre_exp_metrics_df=pre_exp_df,
        test_vs_test=False,
        outliers_quantile=1,
    )

    actual = result.loc[result["segment"].eq(2)].drop(columns="segment")
    pd.testing.assert_frame_equal(actual.reset_index(drop=True), expected)
    assert "p-value CUPED" in result.columns

    pre_exp_with_segment = pre_exp_df.assign(country_code=999)
    result_with_pre_segment = compute_test_metrics(
        df,
        segment="country_code",
        pre_exp_metrics_df=pre_exp_with_segment,
        test_vs_test=False,
        outliers_quantile=1,
    )
    pd.testing.assert_frame_equal(result_with_pre_segment, result)


@pytest.mark.parametrize(
    ("segment", "update", "match"),
    [
        ("", {}, "non-empty string"),
        ("missing", {}, "Missing required column"),
        ("group_name", {}, "must name different columns"),
        ("country_code", {"country_code": np.nan}, "must not contain missing values"),
        ("country_code", {"country_code": "TOTAL"}, "total segment label"),
        ("country_code", {"segment": 1.0}, "conflicts with the segment result column"),
    ],
)
def test_compute_test_metrics_validates_segment_input(
    segment: str,
    update: dict[str, object],
    match: str,
) -> None:
    df = _segmented_metrics_df()
    for column, value in update.items():
        if column in df.columns:
            df[column] = df[column].astype(object)
        df.loc[df.index[0], column] = value

    with pytest.raises(ValueError, match=match):
        compute_test_metrics(df, segment=segment, test_vs_test=False)


def test_compute_test_metrics_rejects_ratio_using_segment_column() -> None:
    df = _segmented_metrics_df()

    with pytest.raises(ValueError, match="reserved column 'country_code'"):
        compute_test_metrics(
            df,
            segment="country_code",
            ratio_metrics=[
                {"name": "bad", "numerator": "clicks", "denominator": "country_code"},
            ],
            test_vs_test=False,
        )


def test_compute_test_metrics_fails_when_segment_lacks_a_comparison_group() -> None:
    df = _segmented_metrics_df()
    df.loc[df["country_code"].eq(2), "group_name"] = "control"

    with pytest.raises(ValueError, match="At least one non-control group"):
        compute_test_metrics(df, segment="country_code", test_vs_test=False)
