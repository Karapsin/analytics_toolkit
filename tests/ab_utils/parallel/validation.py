from __future__ import annotations

from tests.ab_utils._support.parallel import (
    Any,
    ab_utils_module,
    parallel_module,
    pd,
    pytest,
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


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_parallel_compute_metrics_validates_progress(progress: Any) -> None:
    with pytest.raises(ValueError, match="progress"):
        ab_utils_module.compute_test_metrics(
            {"task": {"df": pd.DataFrame()}},
            progress=progress,
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
