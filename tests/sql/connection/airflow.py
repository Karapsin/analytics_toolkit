from __future__ import annotations

from tests.sql._support.cross_area import (
    cli_module,
    config_module,
    labels_module,
    pytest,
    sql_module,
)


def test_airflow_query_label_builds_deterministic_label() -> None:
    label = labels_module.airflow_query_label(
        dag_id="daily-dag",
        task_id="refresh.table",
        run_id="scheduled__2026-06-04T00:00:00+00:00",
        try_number=2,
        operation="load target",
    )

    assert label == (
        "airflow dag=daily-dag task=refresh.table "
        "run=scheduled__2026-06-04T00:00:00+00:00 try=2 op=load target"
    )
    assert sql_module.airflow_query_label is labels_module.airflow_query_label


def test_airflow_query_label_reads_task_instance_context() -> None:
    class TaskInstance:
        dag_id = "dag_ctx"
        task_id = "task_ctx"
        run_id = "run_ctx"
        try_number = 3

    label = labels_module.airflow_query_label({"ti": TaskInstance()})

    assert label == "airflow dag=dag_ctx task=task_ctx run=run_ctx try=3"


def test_airflow_query_label_explicit_fields_override_context() -> None:
    class TaskInstance:
        dag_id = "context_dag"
        task_id = "context_task"
        run_id = "context_run"
        try_number = 1

    label = labels_module.airflow_query_label(
        {"task_instance": TaskInstance()},
        task_id="explicit_task",
        operation="step;drop*/",
    )

    assert label == (
        "airflow dag=context_dag task=explicit_task run=context_run try=1 op=step_drop_/"
    )


def test_airflow_query_label_is_sanitized_and_length_limited() -> None:
    label = labels_module.airflow_query_label(
        dag_id="dag",
        task_id="task",
        run_id="run",
        operation="x" * 500,
    )

    assert label is not None
    assert len(label) == 200
    assert ";" not in labels_module.airflow_query_label(operation="unsafe;label")
    assert labels_module.apply_query_label("select 1", label).startswith(
        "/* analytics_toolkit query_label=airflow dag=dag task=task run=run op="
    )


def test_airflow_query_label_validates_context_type() -> None:
    with pytest.raises(TypeError, match="context"):
        labels_module.airflow_query_label(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_validate_connections_and_cli_output(capsys) -> None:
    results = config_module.validate_connections(["gp", "missing"])

    assert results[0].valid is True
    assert results[0].backend == "gp"
    assert results[1].valid is False
    assert results[1].connection_key == "missing"

    exit_code = cli_module.main(["sql", "support-matrix"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Backend" in captured.out
    assert "trino" in captured.out


def test_cli_validate_reports_errors(capsys) -> None:
    exit_code = cli_module.main(["sql", "validate", "missing"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR missing" in captured.out
