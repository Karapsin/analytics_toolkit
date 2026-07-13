from __future__ import annotations

import importlib
import inspect
import threading
import time
from typing import Any

import pandas as pd
import pytest

ab_utils_module = importlib.import_module("analytics_toolkit.ab_utils")
metrics_module = importlib.import_module("analytics_toolkit.ab_utils.metrics")
parallel_module = importlib.import_module("analytics_toolkit.ab_utils.parallel")
async_sql_module = importlib.import_module("analytics_toolkit.sql.orchestration.async_sql")


def test_executor_shutdown_falls_back_without_cancel_futures() -> None:
    class LegacyExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[bool, bool | None]] = []

        def shutdown(self, *, wait: bool, cancel_futures: bool | None = None) -> None:
            self.calls.append((wait, cancel_futures))
            if cancel_futures is not None:
                message = "legacy shutdown"
                raise TypeError(message)

    executor = LegacyExecutor()
    parallel_module._shutdown_executor(executor, wait=False, cancel_futures=True)
    assert executor.calls == [(False, True), (False, None)]


def test_nested_concurrency_state_tightens_soft_cap_and_can_raise_hard_cap() -> None:
    active = parallel_module._ConcurrencyState(
        effective_concurrency=2,
        hard_cap=4,
        soft_cap=4,
        semaphores=(),
    )
    token = parallel_module._CONCURRENCY_STATE.set(active)
    try:
        tightened = parallel_module._build_concurrency_state(
            concurrency=2,
            soft_concurrency_cap=2,
            hard_concurrency_cap=10,
        )
        raised = parallel_module._build_concurrency_state(
            concurrency=2,
            soft_concurrency_cap=None,
            hard_concurrency_cap=6,
        )
    finally:
        parallel_module._CONCURRENCY_STATE.reset(token)

    assert tightened.effective_concurrency == 4
    assert tightened.soft_cap == 2
    assert tightened.hard_cap == 4
    assert len(tightened.semaphores) == 1
    assert raised.hard_cap == 6
    assert raised.soft_cap == 4


def test_metric_exception_annotation_tolerates_read_only_exception() -> None:
    class ReadOnlyError(Exception):
        def __setattr__(self, name: str, value: object) -> None:
            if name == "analytics_toolkit_metric_task_name":
                message = "read only"
                raise RuntimeError(message)
            super().__setattr__(name, value)

    error = ReadOnlyError("failure")
    parallel_module._annotate_metric_exception(error, "task")
    assert not hasattr(error, "analytics_toolkit_metric_task_name")


def test_parallel_default_validation_reports_multiple_unknown_fields() -> None:
    with pytest.raises(TypeError, match="unexpected keyword arguments"):
        parallel_module._validate_metric_defaults({"unknown_a": 1, "unknown_b": 2})
    with pytest.raises(TypeError, match="unexpected task defaults"):
        parallel_module._validate_metric_task_defaults({"unknown_a": 1, "unknown_b": 2})


def test_parallel_validation_rejects_nonmapping_and_single_unknown_default() -> None:
    with pytest.raises(TypeError, match="tasks must be a non-empty mapping"):
        parallel_module._validate_tasks([], metric_defaults={})
    with pytest.raises(TypeError, match="unexpected task default"):
        parallel_module._validate_metric_task_defaults({"unknown": 1})


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


def test_compute_metrics_from_sql_is_exported() -> None:
    assert ab_utils_module.compute_metrics_from_sql is parallel_module.compute_metrics_from_sql
    assert metrics_module.compute_metrics_from_sql is parallel_module.compute_metrics_from_sql


def test_removed_parallel_metric_names_are_not_exported() -> None:
    assert not hasattr(ab_utils_module, "parallel_compute_metrics")
    assert not hasattr(metrics_module, "parallel_compute_metrics")
    assert not hasattr(ab_utils_module, "parallel_compute_metrics_from_sql")
    assert not hasattr(metrics_module, "parallel_compute_metrics_from_sql")


@pytest.mark.parametrize(
    "function_name",
    [
        "compute_test_metrics",
        "compute_metrics_from_sql",
    ],
)
def test_parallel_compute_metrics_progress_defaults_to_false(
    function_name: str,
) -> None:
    module = ab_utils_module if function_name == "compute_test_metrics" else parallel_module
    signature = inspect.signature(getattr(module, function_name))

    assert signature.parameters["progress"].default is False
    assert signature.parameters["concurrency"].default == 1


def _build_metric_parity_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": list(range(1, 9)),
            "group_name": [
                "control",
                "control",
                "control",
                "control",
                "test",
                "test",
                "test",
                "test",
            ],
            "orders": [1.0, 2.0, 3.0, 4.0, 2.0, 3.0, 4.0, 5.0],
            "clicks": [1.0, 2.0, 1.0, 3.0, 2.0, 3.0, 2.0, 4.0],
            "views": [10.0, 12.0, 8.0, 15.0, 11.0, 14.0, 9.0, 16.0],
        }
    )


def test_metric_entrypoints_match_for_one_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _build_metric_parity_df()
    ratio_metrics = [
        {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    ]
    expected = ab_utils_module.compute_test_metrics(
        df,
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
        concurrency=1,
        progress=False,
    )

    pd.testing.assert_frame_equal(task_result["one"], expected)
    pd.testing.assert_frame_equal(sql_result["one"], expected)


def test_compute_test_metrics_rejects_dataframe_concurrency_above_one() -> None:
    with pytest.raises(ValueError, match="task mapping"):
        ab_utils_module.compute_test_metrics(
            _build_metric_parity_df(),
            concurrency=2,
            progress=False,
        )


def test_parallel_compute_metrics_runs_tasks_and_preserves_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        time.sleep(kwargs["delay"])
        return pd.DataFrame({"metric_name": [kwargs["metric"]]})

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    result = ab_utils_module.compute_test_metrics(
        {
            "slow": {"df": pd.DataFrame(), "metric": "first", "delay": 0.05},
            "fast": {"df": pd.DataFrame(), "metric": "second", "delay": 0.0},
        },
        concurrency=2,
        progress=False,
    )

    assert list(result) == ["slow", "fast"]
    pd.testing.assert_frame_equal(
        result["slow"],
        pd.DataFrame({"metric_name": ["first"]}),
    )
    pd.testing.assert_frame_equal(
        result["fast"],
        pd.DataFrame({"metric_name": ["second"]}),
    )


def test_parallel_compute_metrics_maps_pre_exp_df_and_honors_task_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    pre_df = pd.DataFrame({"user_id": [1]})

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        calls.append(kwargs)
        return pd.DataFrame({"metric_name": [kwargs["metric_name"]]})

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    ab_utils_module.compute_test_metrics(
        {
            "with_pre": {
                "df": pd.DataFrame({"user_id": [1]}),
                "pre_exp_df": pre_df,
                "metric_name": "orders",
                "test_vs_test": False,
                "bootstrap_progress": False,
            }
        },
        progress=False,
    )

    assert len(calls) == 1
    call = calls[0]
    pd.testing.assert_frame_equal(call.pop("df"), pd.DataFrame({"user_id": [1]}))
    assert call.pop("pre_exp_metrics_df") is pre_df
    assert "pre_exp_df" not in call
    assert call == {
        "metric_name": "orders",
        "test_vs_test": False,
        "bootstrap_progress": False,
    }


def test_parallel_compute_metrics_inserts_labels_as_leading_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_result = pd.DataFrame({"metric_name": ["orders", "gmv"], "p-value": [0.1, 0.2]})

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        return raw_result

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    result = ab_utils_module.compute_test_metrics(
        {
            "segment_1": {
                "df": pd.DataFrame(),
                "labels": {"segment": "segment1", "country": "RU"},
            }
        },
        progress=False,
    )

    expected = pd.DataFrame(
        {
            "segment": ["segment1", "segment1"],
            "country": ["RU", "RU"],
            "metric_name": ["orders", "gmv"],
            "p-value": [0.1, 0.2],
        }
    )
    pd.testing.assert_frame_equal(result["segment_1"], expected)
    assert list(raw_result.columns) == ["metric_name", "p-value"]


def test_parallel_compute_metrics_limits_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    active_tasks = 0
    max_active_tasks = 0

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        nonlocal active_tasks, max_active_tasks
        with lock:
            active_tasks += 1
            max_active_tasks = max(max_active_tasks, active_tasks)
        time.sleep(0.05)
        with lock:
            active_tasks -= 1
        return pd.DataFrame({"metric_name": [kwargs["metric_name"]]})

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    tasks = {
        f"task_{index}": {"df": pd.DataFrame(), "metric_name": f"metric_{index}"}
        for index in range(6)
    }

    result = ab_utils_module.compute_test_metrics(
        tasks,
        concurrency=2,
        progress=False,
    )

    assert list(result) == [f"task_{index}" for index in range(6)]
    assert max_active_tasks == 2


def test_parallel_compute_metrics_soft_cap_limits_worker_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    active_tasks = 0
    max_active_tasks = 0

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        nonlocal active_tasks, max_active_tasks
        with lock:
            active_tasks += 1
            max_active_tasks = max(max_active_tasks, active_tasks)
        time.sleep(0.05)
        with lock:
            active_tasks -= 1
        return pd.DataFrame({"metric_name": [kwargs["metric_name"]]})

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    tasks = {
        f"task_{index}": {"df": pd.DataFrame(), "metric_name": f"metric_{index}"}
        for index in range(6)
    }

    result = ab_utils_module.compute_test_metrics(
        tasks,
        concurrency=6,
        soft_concurrency_cap=2,
        progress=False,
    )

    assert list(result) == [f"task_{index}" for index in range(6)]
    assert max_active_tasks == 2


def test_parallel_compute_metrics_hard_cap_rejects_unthrottled_concurrency() -> None:
    tasks = {f"task_{index}": {"df": pd.DataFrame()} for index in range(11)}

    with pytest.raises(
        ValueError,
        match=("effective concurrency exceeds hard_concurrency_cap.*soft_concurrency_cap"),
    ):
        ab_utils_module.compute_test_metrics(
            tasks,
            concurrency=11,
            progress=False,
        )


def test_parallel_compute_metrics_lower_soft_cap_avoids_hard_cap_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    active_tasks = 0
    max_active_tasks = 0

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        nonlocal active_tasks, max_active_tasks
        with lock:
            active_tasks += 1
            max_active_tasks = max(max_active_tasks, active_tasks)
        time.sleep(0.05)
        with lock:
            active_tasks -= 1
        return pd.DataFrame({"metric_name": [kwargs["metric_name"]]})

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    tasks = {
        f"task_{index}": {"df": pd.DataFrame(), "metric_name": f"metric_{index}"}
        for index in range(11)
    }

    result = ab_utils_module.compute_test_metrics(
        tasks,
        concurrency=11,
        soft_concurrency_cap=5,
        hard_concurrency_cap=10,
        progress=False,
    )

    assert list(result) == [f"task_{index}" for index in range(11)]
    assert max_active_tasks == 5


def test_parallel_compute_metrics_updates_progress_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame({"metric_name": [kwargs["metric_name"]]})

    monkeypatch.setattr(parallel_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    ab_utils_module.compute_test_metrics(
        {
            "first": {"df": pd.DataFrame(), "metric_name": "first"},
            "second": {"df": pd.DataFrame(), "metric_name": "second"},
        },
        progress=True,
    )

    assert len(progress_bars) == 1
    progress_bar = progress_bars[0]
    assert progress_bar.kwargs == {
        "total": 2,
        "desc": "compute_test_metrics tasks",
        "unit": "task",
        "disable": False,
    }
    assert progress_bar.updates == [1, 1]
    assert progress_bar.closed


def test_parallel_compute_metrics_fail_fast_raises_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("metric failed")

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        raise error

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    with pytest.raises(RuntimeError) as exc_info:
        ab_utils_module.compute_test_metrics(
            {
                "broken": {"df": pd.DataFrame()},
                "also_broken": {"df": pd.DataFrame()},
            },
            concurrency=1,
            fail_fast=True,
            progress=False,
        )

    assert exc_info.value is error


def test_parallel_compute_metrics_fail_fast_false_returns_exception_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("metric failed")

    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        if kwargs["metric_name"] == "broken":
            raise error
        return pd.DataFrame({"metric_name": [kwargs["metric_name"]]})

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    result = ab_utils_module.compute_test_metrics(
        {
            "ok": {"df": pd.DataFrame(), "metric_name": "ok"},
            "broken": {"df": pd.DataFrame(), "metric_name": "broken"},
            "ok_2": {"df": pd.DataFrame(), "metric_name": "ok_2"},
        },
        fail_fast=False,
        progress=False,
    )

    pd.testing.assert_frame_equal(result["ok"], pd.DataFrame({"metric_name": ["ok"]}))
    assert result["broken"] == str(error)
    pd.testing.assert_frame_equal(
        result["ok_2"],
        pd.DataFrame({"metric_name": ["ok_2"]}),
    )


@pytest.mark.parametrize(
    ("tasks", "expected_exception"),
    [
        ({}, ValueError),
        ([], TypeError),
        ({1: {"df": pd.DataFrame()}}, ValueError),
        ({"": {"df": pd.DataFrame()}}, ValueError),
        ({"task": "not a mapping"}, TypeError),
        ({"task": {}}, ValueError),
    ],
)
def test_parallel_compute_metrics_validates_task_input(
    tasks: Any,
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception):
        ab_utils_module.compute_test_metrics(tasks, progress=False)


@pytest.mark.parametrize(
    "labels",
    [
        ["not", "a", "mapping"],
        {"": "segment1"},
        {1: "segment1"},
        {"segment": ["segment1"]},
        {"segment": {"name": "segment1"}},
    ],
)
def test_parallel_compute_metrics_validates_labels(labels: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="labels|label"):
        ab_utils_module.compute_test_metrics(
            {"task": {"df": pd.DataFrame(), "labels": labels}},
            progress=False,
        )


def test_parallel_compute_metrics_rejects_label_result_column_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_compute_test_metrics(**kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame({"metric_name": ["orders"]})

    monkeypatch.setattr(
        parallel_module,
        "_compute_test_metrics_dataframe",
        fake_compute_test_metrics,
    )

    with pytest.raises(ValueError, match="conflict"):
        ab_utils_module.compute_test_metrics(
            {
                "task": {
                    "df": pd.DataFrame(),
                    "labels": {"metric_name": "orders"},
                }
            },
            progress=False,
        )


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_parallel_compute_metrics_validates_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        ab_utils_module.compute_test_metrics(
            {"task": {"df": pd.DataFrame()}},
            concurrency=concurrency,
            progress=False,
        )


@pytest.mark.parametrize("soft_concurrency_cap", [0, -1, True, 1.5])
def test_parallel_compute_metrics_validates_soft_concurrency_cap(
    soft_concurrency_cap: Any,
) -> None:
    with pytest.raises(ValueError, match="soft_concurrency_cap"):
        ab_utils_module.compute_test_metrics(
            {"task": {"df": pd.DataFrame()}},
            soft_concurrency_cap=soft_concurrency_cap,
            progress=False,
        )


@pytest.mark.parametrize("hard_concurrency_cap", [0, -1, True, 1.5])
def test_parallel_compute_metrics_validates_hard_concurrency_cap(
    hard_concurrency_cap: Any,
) -> None:
    with pytest.raises(ValueError, match="hard_concurrency_cap"):
        ab_utils_module.compute_test_metrics(
            {"task": {"df": pd.DataFrame()}},
            hard_concurrency_cap=hard_concurrency_cap,
            progress=False,
        )


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_parallel_compute_metrics_validates_progress(progress: Any) -> None:
    with pytest.raises(ValueError, match="progress"):
        ab_utils_module.compute_test_metrics(
            {"task": {"df": pd.DataFrame()}},
            progress=progress,
        )


def test_parallel_compute_metrics_rejects_ambiguous_pre_exp_aliases() -> None:
    with pytest.raises(ValueError, match="pre_exp_df"):
        ab_utils_module.compute_test_metrics(
            {
                "task": {
                    "df": pd.DataFrame(),
                    "pre_exp_df": pd.DataFrame(),
                    "pre_exp_metrics_df": pd.DataFrame(),
                }
            },
            progress=False,
        )


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


def test_parallel_compute_metrics_from_sql_rejects_non_string_start_comment() -> None:
    with pytest.raises(ValueError, match="start_comment"):
        parallel_module.compute_metrics_from_sql(
            {"task": {"sql": "select 1"}},
            db_key="analytics_prod",
            start_comment=1,
            progress=False,
        )


def test_parallel_compute_metrics_from_sql_rejects_unknown_metric_default() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'not_a_metric'"):
        parallel_module.compute_metrics_from_sql(
            {"task": {"sql": "select 1"}},
            db_key="analytics_prod",
            progress=False,
            not_a_metric=True,
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
