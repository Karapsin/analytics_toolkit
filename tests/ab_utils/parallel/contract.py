from __future__ import annotations

from tests.ab_utils._support.parallel import (
    Any,
    _build_metric_parity_df,
    ab_utils_module,
    inspect,
    metrics_module,
    parallel_module,
    pd,
    pytest,
)


@pytest.mark.parametrize(
    "function_name",
    ["compute_test_metrics", "compute_metrics_from_sql", "compute_test_metrics_sql_native"],
)
def test_ab_parallel_apis_default_hard_concurrency_cap_to_five(
    function_name: str,
) -> None:
    function = getattr(ab_utils_module, function_name)
    assert inspect.signature(function).parameters["hard_concurrency_cap"].default == 5


def test_compute_metrics_from_sql_is_exported() -> None:
    assert ab_utils_module.compute_metrics_from_sql is parallel_module.compute_metrics_from_sql
    assert metrics_module.compute_metrics_from_sql is parallel_module.compute_metrics_from_sql


def test_compute_test_metrics_rejects_dataframe_concurrency_above_one() -> None:
    with pytest.raises(ValueError, match="task mapping"):
        ab_utils_module.compute_test_metrics(
            _build_metric_parity_df(),
            concurrency=2,
            progress=False,
        )


def test_metric_entrypoints_match_for_one_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _build_metric_parity_df()
    df["cohort"] = ["first", "first", "second", "second"] * 2
    ratio_metrics = [
        {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    ]
    expected = ab_utils_module.compute_test_metrics(
        df,
        segment="cohort",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    task_result = ab_utils_module.compute_test_metrics(
        {
            "one": {
                "df": df,
                "ratio_metrics": ratio_metrics,
                "test_vs_test": False,
                "outliers_quantile": 1,
                "segment": "cohort",
            }
        },
        concurrency=1,
        progress=False,
    )

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        assert tasks == [
            {
                "name": "one:sql",
                "type": "read",
                "db_key": "analytics",
                "query": "select * from source",
            }
        ]
        assert kwargs == {"concurrency": 1, "fail_fast": True, "progress": False}
        return {"one:sql": df}

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    sql_result = parallel_module.compute_metrics_from_sql(
        {"one": {"sql": "select * from source", "test_vs_test": False}},
        db_key="analytics",
        ratio_metrics=ratio_metrics,
        outliers_quantile=1,
        segment="cohort",
        concurrency=1,
        progress=False,
    )

    pd.testing.assert_frame_equal(task_result["one"], expected)
    pd.testing.assert_frame_equal(sql_result["one"], expected)


def test_removed_parallel_metric_names_are_not_exported() -> None:
    assert not hasattr(ab_utils_module, "parallel_compute_metrics")
    assert not hasattr(metrics_module, "parallel_compute_metrics")
    assert not hasattr(ab_utils_module, "parallel_compute_metrics_from_sql")
    assert not hasattr(metrics_module, "parallel_compute_metrics_from_sql")


def test_task_segment_default_can_be_disabled_per_task() -> None:
    df = _build_metric_parity_df()
    df["cohort"] = "all"

    result = ab_utils_module.compute_test_metrics(
        {
            "segmented": {"df": df, "test_vs_test": False},
            "plain": {
                "df": df.drop(columns="cohort"),
                "segment": None,
                "test_vs_test": False,
            },
        },
        segment="cohort",
        progress=False,
    )

    assert "segment" in result["segmented"].columns
    assert "segment" not in result["plain"].columns
