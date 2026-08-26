from __future__ import annotations

from tests.ab_utils._support.parallel import (
    Any,
    parallel_module,
    pd,
    pytest,
)


def test_parallel_compute_metrics_from_sql_applies_metric_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_df = pd.DataFrame({"user_id": [1]})
    override_df = pd.DataFrame({"user_id": [2]})
    async_calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    compute_calls: list[tuple[dict[str, dict[str, Any]], dict[str, Any]]] = []

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        async_calls.append((tasks, kwargs))
        return {
            "default:sql": default_df,
            "override:sql": override_df,
        }

    def fake_parallel_compute_metrics(
        tasks: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        compute_calls.append((tasks, kwargs))
        return {
            "default": pd.DataFrame({"task": ["default"]}),
            "override": pd.DataFrame({"task": ["override"]}),
        }

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    monkeypatch.setattr(
        parallel_module,
        "_compute_metric_tasks",
        fake_parallel_compute_metrics,
    )

    result = parallel_module.compute_metrics_from_sql(
        {
            "default": {
                "sql": "select * from default_task",
                "labels": {"segment": "default"},
            },
            "override": {
                "sql": "select * from override_task",
                "outliers_quantile": 0.995,
                "test_vs_test": True,
            },
        },
        db_key="analytics_prod",
        concurrency=2,
        progress=False,
        group="variant",
        test_vs_test=False,
        bootstrap_progress=False,
        outliers_quantile=0.999,
    )

    assert list(result) == ["default", "override"]
    assert len(async_calls) == 1
    assert async_calls[0][1] == {
        "concurrency": 2,
        "fail_fast": True,
        "progress": False,
    }
    assert len(compute_calls) == 1
    metric_tasks, metric_kwargs = compute_calls[0]
    assert metric_kwargs == {
        "metric_defaults": {},
        "concurrency": 2,
        "fail_fast": True,
        "progress": False,
    }

    default_task = dict(metric_tasks["default"])
    assert default_task.pop("df") is default_df
    assert default_task == {
        "group": "variant",
        "test_vs_test": False,
        "bootstrap_progress": False,
        "outliers_quantile": 0.999,
        "labels": {"segment": "default"},
    }

    override_task = dict(metric_tasks["override"])
    assert override_task.pop("df") is override_df
    assert override_task == {
        "group": "variant",
        "test_vs_test": True,
        "bootstrap_progress": False,
        "outliers_quantile": 0.995,
    }


def test_parallel_compute_metrics_from_sql_fail_fast_false_prints_compute_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    experiment_df = pd.DataFrame({"user_id": [1], "group_name": ["test"], "orders": [1]})
    computed = pd.DataFrame({"metric_name": ["orders"]})

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame | str]:
        del tasks, kwargs
        return {
            "ok:sql": experiment_df,
            "broken:sql": experiment_df,
            "broken:pre_exp_sql": experiment_df,
        }

    def fake_parallel_compute_metrics(
        tasks: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame | str]:
        del tasks, kwargs
        return {
            "ok": computed,
            "broken": "Control label 'control' was not found in column 'group_name'.",
        }

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    monkeypatch.setattr(
        parallel_module,
        "_compute_metric_tasks",
        fake_parallel_compute_metrics,
    )

    result = parallel_module.compute_metrics_from_sql(
        {
            "ok": {"sql": "select * from ok_source"},
            "broken": {
                "sql": "select * from experiment_source",
                "pre_exp_sql": "select * from pre_experiment_source",
            },
        },
        db_key="analytics_prod",
        fail_fast=False,
        progress=False,
    )

    pd.testing.assert_frame_equal(result["ok"], computed)
    assert result["broken"] == ("Control label 'control' was not found in column 'group_name'.")
    output = capsys.readouterr().out
    assert "task 'broken' failed during metric computation" in output
    assert "Experiment SQL:\nselect * from experiment_source" in output
    assert "Pre-experiment SQL:\nselect * from pre_experiment_source" in output


def test_parallel_compute_metrics_from_sql_fail_fast_false_returns_sql_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ok_df = pd.DataFrame({"user_id": [1]})
    skipped_df = pd.DataFrame({"user_id": [2]})
    computed = pd.DataFrame({"metric_name": ["orders"]})
    compute_calls: list[dict[str, dict[str, Any]]] = []

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame | str]:
        assert kwargs["fail_fast"] is False
        return {
            "ok:sql": ok_df,
            "broken:sql": "database failed",
            "pre_broken:sql": skipped_df,
            "pre_broken:pre_exp_sql": "pre query failed",
        }

    def fake_parallel_compute_metrics(
        tasks: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        compute_calls.append(tasks)
        return {"ok": computed}

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    monkeypatch.setattr(
        parallel_module,
        "_compute_metric_tasks",
        fake_parallel_compute_metrics,
    )

    result = parallel_module.compute_metrics_from_sql(
        {
            "ok": {"sql": "select * from ok"},
            "broken": {"sql": "select * from broken"},
            "pre_broken": {
                "sql": "select * from pre_broken",
                "pre_exp_sql": "select * from pre_exp",
            },
        },
        db_key="analytics_prod",
        fail_fast=False,
        progress=False,
    )

    assert list(result) == ["ok", "broken", "pre_broken"]
    pd.testing.assert_frame_equal(result["ok"], computed)
    assert result["broken"] == "database failed"
    assert result["pre_broken"] == "pre query failed"
    assert len(compute_calls) == 1
    assert list(compute_calls[0]) == ["ok"]
    assert compute_calls[0]["ok"]["df"] is ok_df


def test_parallel_compute_metrics_from_sql_fail_fast_prints_both_sqls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("pre experiment query failed")
    error.analytics_toolkit_sql_task_name = "broken:pre_exp_sql"  # type: ignore[attr-defined]

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame | str]:
        del tasks, kwargs
        raise error

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)

    with pytest.raises(RuntimeError) as exc_info:
        parallel_module.compute_metrics_from_sql(
            {
                "broken": {
                    "sql": "select * from experiment_source",
                    "pre_exp_sql": "select * from pre_experiment_source",
                },
            },
            db_key="analytics_prod",
            fail_fast=True,
            progress=False,
        )

    assert exc_info.value is error
    output = capsys.readouterr().out
    assert "task 'broken' failed while loading pre_exp_sql" in output
    assert "Experiment SQL:\nselect * from experiment_source" in output
    assert "Pre-experiment SQL:\nselect * from pre_experiment_source" in output


def test_parallel_compute_metrics_from_sql_loads_sql_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment_df = pd.DataFrame({"user_id": [1]})
    pre_exp_df = pd.DataFrame({"user_id": [1], "orders": [3]})
    second_df = pd.DataFrame({"user_id": [2]})
    async_calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    compute_calls: list[tuple[dict[str, dict[str, Any]], dict[str, Any]]] = []

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        async_calls.append((tasks, kwargs))
        return {
            "with_pre:sql": experiment_df,
            "with_pre:pre_exp_sql": pre_exp_df,
            "without_pre:sql": second_df,
        }

    def fake_parallel_compute_metrics(
        tasks: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        compute_calls.append((tasks, kwargs))
        return {
            "with_pre": pd.DataFrame({"task": ["with_pre"]}),
            "without_pre": pd.DataFrame({"task": ["without_pre"]}),
        }

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    monkeypatch.setattr(
        parallel_module,
        "_compute_metric_tasks",
        fake_parallel_compute_metrics,
    )

    result = parallel_module.compute_metrics_from_sql(
        {
            "with_pre": {
                "sql": "select * from experiment_1",
                "pre_exp_sql": "select * from pre_experiment_1",
                "labels": {"segment": "segment1"},
                "test_vs_test": False,
                "bootstrap_progress": False,
            },
            "without_pre": {
                "sql": "select * from experiment_2",
                "labels": {"segment": "segment2"},
                "multiple_comparisons_adjustment": True,
            },
        },
        db_key="analytics_prod",
        concurrency=2,
        fail_fast=False,
        progress=False,
    )

    assert list(result) == ["with_pre", "without_pre"]
    pd.testing.assert_frame_equal(
        result["with_pre"],
        pd.DataFrame({"task": ["with_pre"]}),
    )
    pd.testing.assert_frame_equal(
        result["without_pre"],
        pd.DataFrame({"task": ["without_pre"]}),
    )

    assert len(async_calls) == 1
    sql_tasks, sql_kwargs = async_calls[0]
    assert sql_kwargs == {"concurrency": 2, "fail_fast": False, "progress": False}
    forbidden_sql_task_fields = {
        "connection",
        "connection_type",
        "connection_key",
        "backend",
    }
    assert all("db_key" in task for task in sql_tasks)
    assert all(forbidden_sql_task_fields.isdisjoint(task) for task in sql_tasks)
    assert sql_tasks == [
        {
            "name": "with_pre:sql",
            "type": "read",
            "db_key": "analytics_prod",
            "query": "select * from experiment_1",
        },
        {
            "name": "with_pre:pre_exp_sql",
            "type": "read",
            "db_key": "analytics_prod",
            "query": "select * from pre_experiment_1",
        },
        {
            "name": "without_pre:sql",
            "type": "read",
            "db_key": "analytics_prod",
            "query": "select * from experiment_2",
        },
    ]

    assert len(compute_calls) == 1
    metric_tasks, metric_kwargs = compute_calls[0]
    assert metric_kwargs == {
        "metric_defaults": {},
        "concurrency": 2,
        "fail_fast": False,
        "progress": False,
    }
    assert list(metric_tasks) == ["with_pre", "without_pre"]

    with_pre = dict(metric_tasks["with_pre"])
    assert with_pre.pop("df") is experiment_df
    assert with_pre.pop("pre_exp_df") is pre_exp_df
    assert with_pre == {
        "labels": {"segment": "segment1"},
        "test_vs_test": False,
        "bootstrap_progress": False,
    }

    without_pre = dict(metric_tasks["without_pre"])
    assert without_pre.pop("df") is second_df
    assert without_pre == {
        "labels": {"segment": "segment2"},
        "multiple_comparisons_adjustment": True,
    }


def test_parallel_compute_metrics_from_sql_passes_concurrency_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment_df = pd.DataFrame({"user_id": [1]})
    async_calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    compute_calls: list[tuple[dict[str, dict[str, Any]], dict[str, Any]]] = []

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        async_calls.append((tasks, kwargs))
        return {"segment:sql": experiment_df}

    def fake_parallel_compute_metrics(
        tasks: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        compute_calls.append((tasks, kwargs))
        return {"segment": pd.DataFrame({"task": ["segment"]})}

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    monkeypatch.setattr(
        parallel_module,
        "_compute_metric_tasks",
        fake_parallel_compute_metrics,
    )

    result = parallel_module.compute_metrics_from_sql(
        {
            "segment": {
                "sql": "select * from experiment",
            },
        },
        db_key="analytics_prod",
        concurrency=6,
        soft_concurrency_cap=2,
        hard_concurrency_cap=7,
        progress=False,
    )

    pd.testing.assert_frame_equal(result["segment"], pd.DataFrame({"task": ["segment"]}))
    assert len(async_calls) == 1
    assert async_calls[0][1] == {
        "concurrency": 6,
        "fail_fast": True,
        "progress": False,
        "soft_concurrency_cap": 2,
        "hard_concurrency_cap": 7,
    }
    assert len(compute_calls) == 1
    assert compute_calls[0][1] == {
        "metric_defaults": {},
        "concurrency": 6,
        "fail_fast": True,
        "progress": False,
        "soft_concurrency_cap": 2,
        "hard_concurrency_cap": 7,
    }


def test_parallel_compute_metrics_from_sql_passes_start_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_df = pd.DataFrame({"user_id": [1]})
    override_df = pd.DataFrame({"user_id": [2]})
    override_pre_df = pd.DataFrame({"user_id": [2], "orders": [1]})
    blank_df = pd.DataFrame({"user_id": [3]})
    async_calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        async_calls.append((tasks, kwargs))
        return {
            "default:sql": default_df,
            "override:sql": override_df,
            "override:pre_exp_sql": override_pre_df,
            "blank:sql": blank_df,
        }

    def fake_parallel_compute_metrics(
        tasks: dict[str, dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        return {name: pd.DataFrame({"task": [name]}) for name in tasks}

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    monkeypatch.setattr(
        parallel_module,
        "_compute_metric_tasks",
        fake_parallel_compute_metrics,
    )

    result = parallel_module.compute_metrics_from_sql(
        {
            "default": {
                "sql": "select * from default_task",
            },
            "override": {
                "sql": "select * from override_task",
                "pre_exp_sql": "select * from override_pre_task",
                "start_comment": "-- task comment",
            },
            "blank": {
                "sql": "select * from blank_task",
                "start_comment": "",
            },
        },
        db_key="analytics_prod",
        concurrency=3,
        fail_fast=False,
        start_comment="-- default comment",
        progress=False,
    )

    assert list(result) == ["default", "override", "blank"]
    assert len(async_calls) == 1
    sql_tasks, sql_kwargs = async_calls[0]
    assert sql_kwargs == {
        "concurrency": 3,
        "fail_fast": False,
        "progress": False,
        "start_comment": "-- default comment",
    }
    assert sql_tasks == [
        {
            "name": "default:sql",
            "type": "read",
            "db_key": "analytics_prod",
            "query": "select * from default_task",
        },
        {
            "name": "override:sql",
            "type": "read",
            "db_key": "analytics_prod",
            "query": "select * from override_task",
            "start_comment": "-- task comment",
        },
        {
            "name": "override:pre_exp_sql",
            "type": "read",
            "db_key": "analytics_prod",
            "query": "select * from override_pre_task",
            "start_comment": "-- task comment",
        },
        {
            "name": "blank:sql",
            "type": "read",
            "db_key": "analytics_prod",
            "query": "select * from blank_task",
            "start_comment": "",
        },
    ]


def test_parallel_compute_metrics_from_sql_prints_both_sqls_for_exp_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame | str]:
        del tasks, kwargs
        return {
            "broken:sql": "experiment query failed",
            "broken:pre_exp_sql": pd.DataFrame({"user_id": [1]}),
        }

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)

    result = parallel_module.compute_metrics_from_sql(
        {
            "broken": {
                "sql": "select * from experiment_source",
                "pre_exp_sql": "select * from pre_experiment_source",
            },
        },
        db_key="analytics_prod",
        fail_fast=False,
        progress=False,
    )

    assert result["broken"] == "experiment query failed"
    output = capsys.readouterr().out
    assert "task 'broken' failed while loading sql" in output
    assert "experiment query failed" in output
    assert "Experiment SQL:\nselect * from experiment_source" in output
    assert "Pre-experiment SQL:\nselect * from pre_experiment_source" in output


def test_parallel_compute_metrics_from_sql_prints_both_sqls_for_pre_exp_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame | str]:
        del tasks, kwargs
        return {
            "broken:sql": pd.DataFrame({"user_id": [1]}),
            "broken:pre_exp_sql": "pre experiment query failed",
        }

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)

    result = parallel_module.compute_metrics_from_sql(
        {
            "broken": {
                "sql": "select * from experiment_source",
                "pre_exp_sql": "select * from pre_experiment_source",
            },
        },
        db_key="analytics_prod",
        fail_fast=False,
        progress=False,
    )

    assert result["broken"] == "pre experiment query failed"
    output = capsys.readouterr().out
    assert "task 'broken' failed while loading pre_exp_sql" in output
    assert "pre experiment query failed" in output
    assert "Experiment SQL:\nselect * from experiment_source" in output
    assert "Pre-experiment SQL:\nselect * from pre_experiment_source" in output
