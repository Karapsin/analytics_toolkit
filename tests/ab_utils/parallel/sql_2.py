from __future__ import annotations

from tests.ab_utils._support.parallel import (
    Any,
    async_sql_module,
    parallel_module,
    pd,
    pytest,
)


def test_parallel_compute_metrics_from_sql_prints_sqls_for_compute_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty_df = pd.DataFrame(columns=["user_id", "group_name", "orders"])

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame | str]:
        del tasks, kwargs
        return {
            "broken:sql": empty_df,
            "broken:pre_exp_sql": empty_df,
        }

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)

    with pytest.raises(ValueError, match="Control label 'control'"):
        parallel_module.compute_metrics_from_sql(
            {
                "broken": {
                    "sql": "select * from experiment_source",
                    "pre_exp_sql": "select * from pre_experiment_source",
                },
            },
            db_key="analytics_prod",
            concurrency=1,
            fail_fast=True,
            progress=False,
        )

    output = capsys.readouterr().out
    assert "task 'broken' failed during metric computation" in output
    assert "Control label 'control' was not found in column 'group_name'" in output
    assert "Experiment SQL:\nselect * from experiment_source" in output
    assert "Pre-experiment SQL:\nselect * from pre_experiment_source" in output


@pytest.mark.parametrize("field", ["df", "pre_exp_df", "pre_exp_metrics_df"])
def test_parallel_compute_metrics_from_sql_rejects_dataframe_inputs(field: str) -> None:
    with pytest.raises(ValueError, match="SQL-backed"):
        parallel_module.compute_metrics_from_sql(
            {
                "task": {
                    "sql": "select 1",
                    field: pd.DataFrame(),
                }
            },
            db_key="analytics_prod",
            progress=False,
        )


def test_parallel_compute_metrics_from_sql_rejects_non_string_start_comment() -> None:
    with pytest.raises(ValueError, match="start_comment"):
        parallel_module.compute_metrics_from_sql(
            {"task": {"sql": "select 1"}},
            db_key="analytics_prod",
            start_comment=1,
            progress=False,
        )


@pytest.mark.parametrize("field", ["df", "pre_exp_df", "pre_exp_metrics_df"])
def test_parallel_compute_metrics_from_sql_rejects_top_level_dataframe_defaults(
    field: str,
) -> None:
    with pytest.raises(ValueError, match="SQL-backed dataframe"):
        parallel_module.compute_metrics_from_sql(
            {"task": {"sql": "select 1"}},
            db_key="analytics_prod",
            progress=False,
            **{field: pd.DataFrame()},
        )


def test_parallel_compute_metrics_from_sql_rejects_unknown_metric_default() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'not_a_metric'"):
        parallel_module.compute_metrics_from_sql(
            {"task": {"sql": "select 1"}},
            db_key="analytics_prod",
            progress=False,
            not_a_metric=True,
        )


def test_parallel_compute_metrics_from_sql_uses_db_key_for_async_read_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment_df = pd.DataFrame({"user_id": [1]})
    metric_df = pd.DataFrame({"task": ["segment"]})
    read_calls: list[dict[str, str]] = []
    compute_calls: list[dict[str, dict[str, Any]]] = []

    def fake_read_sql(*, db_key: str, query: str) -> pd.DataFrame:
        read_calls.append({"db_key": db_key, "query": query})
        return experiment_df

    def fake_parallel_compute_metrics(
        tasks: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        del kwargs
        compute_calls.append(tasks)
        return {"segment": metric_df}

    monkeypatch.setattr(async_sql_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(
        parallel_module,
        "_compute_metric_tasks",
        fake_parallel_compute_metrics,
    )

    result = parallel_module.compute_metrics_from_sql(
        {"segment": {"sql": "select * from experiment"}},
        db_key="analytics_prod",
        concurrency=1,
        progress=False,
    )

    pd.testing.assert_frame_equal(result["segment"], metric_df)
    assert read_calls == [{"db_key": "analytics_prod", "query": "select * from experiment"}]
    assert len(compute_calls) == 1
    assert compute_calls[0]["segment"]["df"] is experiment_df


@pytest.mark.parametrize(
    ("tasks", "expected_exception"),
    [
        ({}, ValueError),
        ([], TypeError),
        ({1: {"sql": "select 1"}}, ValueError),
        ({"": {"sql": "select 1"}}, ValueError),
        ({"task": "not a mapping"}, TypeError),
        ({"task": {}}, ValueError),
        ({"task": {"sql": ""}}, ValueError),
        ({"task": {"sql": "select 1", "pre_exp_sql": ""}}, ValueError),
        ({"task": {"sql": "select 1", "start_comment": 1}}, ValueError),
    ],
)
def test_parallel_compute_metrics_from_sql_validates_task_input(
    tasks: Any,
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception):
        parallel_module.compute_metrics_from_sql(
            tasks,
            db_key="analytics_prod",
            progress=False,
        )


def test_sql_metric_compute_failure_log_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[dict[str, object]] = []
    monkeypatch.setattr(
        parallel_module,
        "_log_sql_metric_compute_failure",
        lambda **kwargs: logged.append(kwargs),
    )
    tasks = [("task", {}, "SELECT metric", None, None)]
    parallel_module._log_sql_metric_compute_failure_from_exception(ValueError("plain"), tasks)
    named = ValueError("compute failed")
    named.analytics_toolkit_metric_task_name = "task"
    parallel_module._log_sql_metric_compute_failure_from_exception(named, tasks)
    unmatched = ValueError("unmatched")
    unmatched.analytics_toolkit_metric_task_name = "other"
    parallel_module._log_sql_metric_compute_failure_from_exception(unmatched, tasks)

    assert logged == [
        {
            "name": "task",
            "error": "compute failed",
            "sql": "SELECT metric",
            "pre_exp_sql": None,
        }
    ]


def test_sql_metric_failure_log_routing_ignores_unrelated_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[dict[str, object]] = []
    monkeypatch.setattr(
        parallel_module,
        "_log_sql_metric_task_failure",
        lambda **kwargs: logged.append(kwargs),
    )
    tasks = [("task", {}, "SELECT metric", "SELECT pre", None)]

    parallel_module._log_sql_metric_task_failure_from_exception(ValueError("plain"), tasks)
    named = ValueError("load failed")
    named.analytics_toolkit_sql_task_name = "task:pre_exp_sql"
    parallel_module._log_sql_metric_task_failure_from_exception(named, tasks)
    unmatched = ValueError("unmatched")
    unmatched.analytics_toolkit_sql_task_name = "other:sql"
    parallel_module._log_sql_metric_task_failure_from_exception(unmatched, tasks)

    assert logged == [
        {
            "name": "task",
            "failed_field": "pre_exp_sql",
            "error": "load failed",
            "sql": "SELECT metric",
            "pre_exp_sql": "SELECT pre",
        }
    ]
    assert parallel_module._sql_read_task_field(None) is None
    assert parallel_module._sql_read_task_field("task:other") is None
