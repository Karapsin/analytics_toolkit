from __future__ import annotations

from tests.ab_utils._support.metrics import (
    RatioMetricSpec,
    _build_ratio_valid_mask,
    _build_sample_metrics_df,
    _compute_agg_ratio_diff_standard_error,
    _compute_agg_ratio_group_stats,
    _compute_agg_ratio_variance,
    _get_numeric_metric_series,
    _single_metric_row,
    compute_test_metrics,
    math,
    pd,
    planning_module,
    pytest,
)


def test_sql_native_query_builders_cover_no_pre_ratio_and_policies() -> None:
    mean_metric = {"kind": "mean", "metric_key": "metric", "column": "metric"}
    no_pre_query = planning_module._build_sql_native_mde_stats_query(
        backend="gp",
        source='"public"."events"',
        sql_where="country = 'US'",
        user_id="user",
        date_column="dt",
        metric_definition=mean_metric,
        aggregation_policies={"metric": "max"},
        outcome_start=pd.Timestamp("2026-01-01"),
        outcome_days=7,
        pre_start=None,
        pre_days=7,
        outliers_quantile=0.99,
        outliers_policy="non_zero_truncate",
    )
    assert "pre_user AS" not in no_pre_query
    assert "CAST(0 AS INTEGER) AS cuped_pair_n" in no_pre_query
    assert 'MAX("metric")' in no_pre_query
    assert "(country = 'US')" in no_pre_query
    assert "value <> 0" in no_pre_query

    ratio_metric = {
        "kind": "ratio",
        "metric_key": "ratio",
        "ratio_spec": {"numerator": "shared", "denominator": "shared", "level": "agg"},
    }
    assert planning_module._sql_native_metric_columns(ratio_metric) == ["shared"]

    user_ratio = {
        "kind": "ratio",
        "metric_key": "ratio",
        "ratio_spec": {"numerator": "num", "denominator": "den", "level": "user"},
    }
    user_ctes = planning_module._build_sql_native_metric_value_ctes(
        prefix="outcome",
        backend="gp",
        user_id="user",
        metric_definition=user_ratio,
        outliers_quantile=0.99,
        outliers_policy="remove",
    )
    assert "CASE WHEN" in user_ctes[0]
    assert "THEN NULL ELSE value END" in user_ctes[2]

    ratio_metric["ratio_spec"] = {"numerator": "num", "denominator": "den", "level": "agg"}
    agg_ctes = planning_module._build_sql_native_metric_value_ctes(
        prefix="outcome",
        backend="gp",
        user_id="user",
        metric_definition=ratio_metric,
        outliers_quantile=0.99,
        outliers_policy="remove",
    )
    assert "THEN NULL ELSE numerator END" in agg_ctes[2]
    assert "THEN NULL ELSE denominator END" in agg_ctes[2]
    assert "CASE WHEN SUM(denominator) > 0" in agg_ctes[3]

    with pytest.raises(AssertionError, match="Unexpected MDE aggregation policy"):
        planning_module._build_sql_native_user_window_cte(
            cte_name="outcome_user",
            backend="gp",
            source='"public"."events"',
            sql_where=None,
            user_id="user",
            date_column="dt",
            columns=["metric"],
            aggregation_policies={"metric": "median"},
            start_date=pd.Timestamp("2026-01-01"),
            days=1,
        )


@pytest.mark.parametrize(
    ("frame", "ratio", "expected_nan"),
    [
        (pd.DataFrame({"numerator": [1.0], "denominator": [1.0]}), 1.0, True),
        (
            pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [1.0, 2.0]}),
            math.nan,
            True,
        ),
        (
            pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [0.0, 0.0]}),
            1.0,
            True,
        ),
        (
            pd.DataFrame({"numerator": [1.0, 3.0], "denominator": [1.0, 1.0]}),
            2.0,
            False,
        ),
    ],
)
def test_aggregate_ratio_unit_variance_edges(
    frame: pd.DataFrame, ratio: float, expected_nan: bool
) -> None:
    variance = planning_module._compute_agg_ratio_unit_variance(frame, ratio)
    assert math.isnan(variance) if expected_nan else variance == pytest.approx(2.0)


def test_aggregate_ratio_unit_variance_handles_nan_centered_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame({"numerator": [1.0, 2.0], "denominator": [1.0, 1.0]})
    monkeypatch.setattr(pd.Series, "var", lambda *_args, **_kwargs: math.nan)
    assert math.isnan(planning_module._compute_agg_ratio_unit_variance(frame, 1.5))


def test_compute_test_metrics_accepts_ratio_spec_dataclass() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4, 5, 6],
            "group_name": ["control", "control", "control", "test", "test", "test"],
            "clicks": [1, 2, 3, 2, 3, 4],
            "impressions": [10, 10, 10, 10, 10, 10],
        }
    )

    result = compute_test_metrics(
        df,
        ratio_metrics=[
            RatioMetricSpec(
                name="ctr_user",
                numerator="clicks",
                denominator="impressions",
                level="user",
            )
        ],
        test_vs_test=False,
    )

    assert "ctr_user" in set(result["metric_name"])


def test_ratio_metrics_default_to_agg_level() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(
        df,
        ratio_metrics=[{"name": "ctr", "numerator": "clicks", "denominator": "impressions"}],
        test_vs_test=True,
    )

    ratio_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "control")
        & (result["metric_name"] == "ctr")
    ].iloc[0]
    assert ratio_row["metric_type"] == "ratio"
    assert ratio_row["metric_group_2"] == pytest.approx((5 + 3 + 4 + 2) / (10 + 8 + 0 + 4))
    assert ratio_row["metric_group_1"] == pytest.approx((7 + 5 + 6 + 8) / (14 + 10 + 12 + 16))

    numerator = _get_numeric_metric_series(df, "clicks")
    denominator = _get_numeric_metric_series(df, "impressions")
    valid_mask = _build_ratio_valid_mask(numerator=numerator, denominator=denominator, level="agg")
    baseline_frame = pd.DataFrame(
        {
            "numerator": numerator[(df["group_name"] == "control") & valid_mask],
            "denominator": denominator[(df["group_name"] == "control") & valid_mask],
        }
    )
    test_frame = pd.DataFrame(
        {
            "numerator": numerator[(df["group_name"] == "test_a") & valid_mask],
            "denominator": denominator[(df["group_name"] == "test_a") & valid_mask],
        }
    )
    baseline_stats = _compute_agg_ratio_group_stats(baseline_frame)
    test_stats = _compute_agg_ratio_group_stats(test_frame)
    expected_control_variance = _compute_agg_ratio_variance(
        baseline_frame,
        baseline_stats["ratio"],
    )
    expected_test_variance = _compute_agg_ratio_variance(test_frame, test_stats["ratio"])
    assert ratio_row["variance_group_2"] == pytest.approx(expected_control_variance)
    assert ratio_row["variance_group_1"] == pytest.approx(expected_test_variance)
    assert ratio_row["s.e."] == pytest.approx(
        _compute_agg_ratio_diff_standard_error(
            baseline_frame=baseline_frame,
            baseline_ratio=baseline_stats["ratio"],
            test_frame=test_frame,
            test_ratio=test_stats["ratio"],
        )
    )

    test_vs_test_row = result[
        (result["group_1"] == "test_a")
        & (result["group_2"] == "test_b")
        & (result["metric_name"] == "ctr")
    ].iloc[0]
    assert test_vs_test_row["metric_group_1"] == pytest.approx((7 + 5 + 6 + 8) / 52)
    assert test_vs_test_row["metric_group_2"] == pytest.approx((4 + 5 + 3 + 4) / 32)


def test_compute_test_metrics_user_ratio_default_non_zero_truncate() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 11)),
            "group_name": ["control"] * 5 + ["test"] * 5,
            "clicks": [0.0] * 9 + [100.0],
            "impressions": [10.0] * 10,
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

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.8,
    )

    row = _single_metric_row(result, "ctr_user")
    assert row["outliers_cutoff"] == pytest.approx(10.0)
    assert row["outliers_n_group_1"] == 0
    assert row["metric_group_1"] == pytest.approx(2.0)


def test_compute_test_metrics_agg_ratio_default_non_zero_truncate() -> None:
    df = pd.DataFrame(
        {
            "user_id": list(range(1, 11)),
            "group_name": ["control"] * 5 + ["test"] * 5,
            "clicks": [0.0] * 9 + [100.0],
            "impressions": [10.0] * 10,
        }
    )
    ratio_metrics = [{"name": "ctr", "numerator": "clicks", "denominator": "impressions"}]

    result = compute_test_metrics(
        df,
        control="control",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=0.8,
    )

    row = _single_metric_row(result, "ctr")
    assert row["outliers_cutoff"] == pytest.approx(10.0)
    assert row["outliers_n_group_1"] == 0
    assert row["metric_group_1"] == pytest.approx(2.0)
