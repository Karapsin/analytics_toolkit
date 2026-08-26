from __future__ import annotations

from tests.ab_utils._support.sql_native import (
    _base_stats_from_expected,
    _install_sql_backed_dataframe_fakes,
    _install_sql_native_fakes,
    _metric_df,
    ab_utils,
    metrics_module,
    pd,
    pytest,
    sql_native,
)


def test_compute_test_metrics_sql_native_accepts_raw_sql_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratio_metrics: list[dict[str, object]] = []
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    metadata = pd.DataFrame(columns=["user_id", "group_name", "orders"])
    queries = _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
        metadata_frame=metadata,
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "select user_id, group_name, orders from mart.ab_source",
        source_type="sql",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any("WHERE 1 = 0" in query for query in queries)


def test_compute_test_metrics_sql_native_finalizes_compact_sql_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratio_metrics = [{"name": "ctr", "numerator": "clicks", "denominator": "views"}]
    expected = ab_utils.compute_test_metrics(
        _metric_df(),
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )
    queries = _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any("duplicate_user_rows" in query for query in queries)
    assert not any("SELECT * FROM mart.ab_source" in query for query in queries)


def test_compute_test_metrics_sql_native_is_exported() -> None:
    assert ab_utils.compute_test_metrics_sql_native is sql_native.compute_test_metrics_sql_native
    assert (
        metrics_module.compute_test_metrics_sql_native is sql_native.compute_test_metrics_sql_native
    )


def test_compute_test_metrics_sql_native_task_mapping_adds_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        {
            "segment_a": {
                "source": "mart.ab_source",
                "metric_columns": ["orders"],
                "labels": {"segment": "a"},
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
    )

    assert isinstance(result, dict)
    assert result["segment_a"]["segment"].tolist() == ["a"]
    pd.testing.assert_frame_equal(
        result["segment_a"].drop(columns=["segment"]),
        expected,
    )


def test_compute_test_metrics_sql_native_uses_bootstrap_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    bootstrap = pd.DataFrame(
        [
            {
                "metric_name": "orders",
                "group_1": "test",
                "group_2": "control",
                "se_bootstrap": 0.25,
                "bootstrap_adj_p": 0.125,
            }
        ]
    )
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
        bootstrap_stats=bootstrap,
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        metric_columns=["orders"],
        test_vs_test=False,
        outliers_quantile=1,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=10,
    )

    assert result.loc[0, "s.e. bootstrap"] == pytest.approx(0.25)
    assert result.loc[0, "bootstrap_adj_p"] == pytest.approx(0.125)


def test_metric_entrypoints_three_way_parity_single_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _metric_df()
    ratio_metrics = [
        {
            "name": "ctr_agg",
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "views",
            "level": "user",
        },
    ]
    expected = ab_utils.compute_test_metrics(
        df,
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )
    _install_sql_backed_dataframe_fakes(monkeypatch, df)
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    sql_backed_result = ab_utils.compute_metrics_from_sql(
        {"one": {"sql": "select * from mart.ab_source", "test_vs_test": False}},
        db_key="analytics",
        ratio_metrics=ratio_metrics,
        outliers_quantile=1,
        concurrency=1,
        progress=False,
    )
    sql_native_result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        metric_columns=["orders", "clicks", "views"],
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(sql_backed_result["one"], expected)
    pd.testing.assert_frame_equal(sql_native_result, expected)
    assert list(sql_backed_result["one"].columns) == list(expected.columns)
    assert list(sql_native_result.columns) == list(expected.columns)


def test_metric_entrypoints_three_way_parity_task_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _metric_df()
    ratio_metrics = [
        {
            "name": "ctr_agg",
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "views",
            "level": "user",
        },
    ]
    expected_tasks = ab_utils.compute_test_metrics(
        {
            "segment_a": {
                "df": df,
                "labels": {"segment": "a"},
                "ratio_metrics": ratio_metrics,
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
        concurrency=1,
        progress=False,
    )
    _install_sql_backed_dataframe_fakes(monkeypatch, df)
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected_tasks["segment_a"].drop(columns=["segment"])),
    )

    sql_backed_tasks = ab_utils.compute_metrics_from_sql(
        {
            "segment_a": {
                "sql": "select * from mart.ab_source",
                "labels": {"segment": "a"},
                "test_vs_test": False,
            }
        },
        db_key="analytics",
        ratio_metrics=ratio_metrics,
        outliers_quantile=1,
        concurrency=1,
        progress=False,
    )
    sql_native_tasks = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        {
            "segment_a": {
                "source": "mart.ab_source",
                "metric_columns": ["orders", "clicks", "views"],
                "labels": {"segment": "a"},
                "ratio_metrics": ratio_metrics,
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
        concurrency=1,
        progress=False,
    )

    pd.testing.assert_frame_equal(sql_backed_tasks["segment_a"], expected_tasks["segment_a"])
    pd.testing.assert_frame_equal(sql_native_tasks["segment_a"], expected_tasks["segment_a"])
    assert list(sql_backed_tasks["segment_a"].columns) == list(expected_tasks["segment_a"].columns)
    assert list(sql_native_tasks["segment_a"].columns) == list(expected_tasks["segment_a"].columns)
