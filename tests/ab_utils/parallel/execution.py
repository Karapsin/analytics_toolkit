from __future__ import annotations

from tests.ab_utils._support.parallel import (
    Any,
    ab_utils_module,
    inspect,
    parallel_module,
    pd,
    pytest,
    threading,
    time,
)


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
            hard_concurrency_cap=5,
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


def test_parallel_compute_metrics_cleans_up_when_progress_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_calls: list[tuple[bool, bool]] = []
    error = RuntimeError("progress setup failed")

    def fail_progress_bar(*, total: int, progress: bool) -> None:
        assert total == 1
        assert progress is False
        raise error

    def record_shutdown(
        _executor: Any,
        *,
        wait: bool,
        cancel_futures: bool,
    ) -> None:
        shutdown_calls.append((wait, cancel_futures))

    monkeypatch.setattr(parallel_module, "_make_progress_bar", fail_progress_bar)
    monkeypatch.setattr(parallel_module, "_shutdown_executor", record_shutdown)
    state_before = parallel_module._CONCURRENCY_STATE.get()

    with pytest.raises(RuntimeError, match="progress setup failed"):
        parallel_module._compute_metric_tasks(
            {"task": {"df": pd.DataFrame()}},
            progress=False,
        )

    assert shutdown_calls == [(True, True)]
    assert parallel_module._CONCURRENCY_STATE.get() is state_before


def test_parallel_compute_metrics_default_hard_cap_rejects_six_workers() -> None:
    with pytest.raises(ValueError, match=r"effective concurrency.*\(6 > 5\)"):
        ab_utils_module.compute_test_metrics(
            {"task": {"df": pd.DataFrame()}},
            concurrency=6,
            progress=False,
        )


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
