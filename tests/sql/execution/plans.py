from __future__ import annotations

from tests.sql._support.cross_area import (
    labels_module,
    operation_runner_module,
    plans_module,
    pytest,
    query_timing_module,
    sql_module,
)


def test_tracked_sql_operation_logs_finished_preview(capsys) -> None:
    with operation_runner_module.tracked_sql_operation(
        operation_name="unit_operation",
        alias="gp",
        backend="gp",
        phase="phase",
        preview_sql="\n\n  select * from source_table\nwhere id = 1",
    ):
        pass

    output = capsys.readouterr().out
    assert "[unit_operation] [gp/gp] [phase] Starting SQL" in output
    assert "[unit_operation] [gp/gp] [phase] Finished SQL in " in output
    assert "Finished SQL operation unit_operation" not in output
    assert "Finished SQL statement:\nselect * from source_table" in output


def test_operation_runner_remaining_metadata_and_timing_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def plain() -> str:
        return "ok"

    decorated = operation_runner_module.timed_public_sql_function(plain)
    assert operation_runner_module.timed_public_sql_function(decorated) is decorated
    assert decorated() == "ok"

    metadata = plans_module.SqlOperationMetadata()
    assert (
        operation_runner_module.merge_operation_metadata(
            metadata,
            elapsed_seconds=1.5,
            retry_attempts=2,
            read_rows=3,
            statement_count=4,
            operation_status="success",
            query_label="daily",
        )
        is metadata
    )
    assert isinstance(metadata.as_dict(), dict)
    assert (
        metadata.elapsed_seconds,
        metadata.retry_attempts,
        metadata.read_rows,
        metadata.statement_count,
        metadata.operation_status,
        metadata.query_label,
    ) == (1.5, 2, 3, 4, "success", "daily")
    assert operation_runner_module.merge_operation_metadata(metadata) is metadata

    monkeypatch.setattr(
        operation_runner_module.time,
        "perf_counter",
        iter([0.0, 0.9996]).__next__,
    )
    with operation_runner_module.tracked_sql_operation(
        metadata=metadata,
        operation_name="failure",
        alias=None,
        backend=None,
        phase="test",
        preview_sql=" \n\t",
    ):
        pass
    assert metadata.operation_status == "success"
    assert "Finished SQL statement" not in capsys.readouterr().out
    assert operation_runner_module._format_duration(0.9996) == "1 second"


def test_tracked_operation_failure_and_operation_runner_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = plans_module.SqlOperationMetadata()
    error = RuntimeError("broken")
    with pytest.raises(RuntimeError, match="broken"):  # noqa: SIM117 -- py3.8
        with operation_runner_module.tracked_sql_operation(
            metadata=metadata,
            operation_name="failure",
            alias="gp",
            backend="gp",
            phase="test",
        ):
            raise error
    assert metadata.operation_status == "failed"

    annotated: list[tuple[BaseException, object]] = []
    monkeypatch.setattr(
        operation_runner_module,
        "annotate_sql_exception",
        lambda exc, context: annotated.append((exc, context)),
    )
    error = RuntimeError("once")
    with pytest.raises(RuntimeError) as caught:
        operation_runner_module.run_annotated_once(
            operation=lambda: (_ for _ in ()).throw(error),
            context=object(),
        )
    assert caught.value is error
    assert annotated[0][0] is error
    assert (
        operation_runner_module.run_annotated_once(
            operation=lambda: "ok",
            context=object(),
        )
        == "ok"
    )

    rollbacks: list[object] = []
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.rollback_quietly",
        rollbacks.append,
    )
    connection = object()
    operation_runner_module._rollback_quietly(connection)
    assert rollbacks == [connection]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-0.001, "0 seconds"),
        (0, "0 seconds"),
        (0.1234, "0.123 seconds"),
        (1, "1 second"),
        (2, "2 seconds"),
        (90, "1 minute 30 seconds"),
        (3661, "1 hour 1 minute 1 second"),
        (90061, "1 day 1 hour 1 minute 1 second"),
    ],
)
def test_sql_duration_formatter_is_human_readable(
    seconds: float,
    expected: str,
) -> None:
    assert operation_runner_module._format_duration(seconds) == expected


def test_tracked_sql_operation_logs_human_readable_elapsed(
    monkeypatch,
    capsys,
) -> None:
    perf_counter_values = iter([10.0, 100.0])
    monkeypatch.setattr(
        operation_runner_module.time,
        "perf_counter",
        lambda: next(perf_counter_values),
    )

    with operation_runner_module.tracked_sql_operation(
        operation_name="unit_operation",
        alias="gp",
        backend="gp",
        phase="phase",
    ):
        pass

    output = capsys.readouterr().out
    assert ("[unit_operation] [gp/gp] [phase] Finished SQL in 1 minute 30 seconds") in output


def test_tracked_sql_operation_tags_inner_time_print(capsys) -> None:
    with operation_runner_module.tracked_sql_operation(
        operation_name="unit_operation",
        alias="gp",
        backend="gp",
        phase="phase",
    ):
        sql_module.time_print("inner message")

    output = capsys.readouterr().out
    assert "[unit_operation] [gp/gp] [phase] inner message" in output


def test_run_timed_query_inherits_sql_operation_tags(capsys) -> None:
    with operation_runner_module.tracked_sql_operation(
        operation_name="timed_query",
        alias="gp",
        backend="gp",
        phase="read",
    ):
        result = query_timing_module.run_timed_query("gp", lambda: "ok")

    output = capsys.readouterr().out
    assert result == "ok"
    assert ("[timed_query] [gp/gp] [read] Finished SQL query in ") in output


def test_run_timed_query_overrides_inherited_phase(capsys) -> None:
    with operation_runner_module.tracked_sql_operation(
        operation_name="timed_query",
        alias="gp",
        backend="gp",
        phase=None,
    ):
        result = query_timing_module.run_timed_query(
            "gp",
            lambda: "ok",
            phase="setup",
        )

    output = capsys.readouterr().out
    assert result == "ok"
    assert "[timed_query] [gp/gp] [setup] Finished SQL query in " in output
    assert "[timed_query] [gp/gp] Starting SQL" in output
    assert "[timed_query] [gp/gp] Finished SQL in " in output


@pytest.mark.parametrize(
    ("action_name", "phase"),
    [
        ("superseded-stage inspection", "inspect_superseded_stages"),
        ("snapshot counting", "count_snapshot"),
        ("source-batch reading", "read_source_batch"),
        ("stage-identity validation", "validate_stage_identity"),
        ("ordinal validation", "validate_stage_ordinals"),
    ],
)
def test_run_timed_query_logs_precise_transfer_action_and_phase(
    action_name: str,
    phase: str,
    capsys,
) -> None:
    query_timing_module.run_timed_query(
        "gp",
        lambda: None,
        action_name=action_name,
        phase=phase,
    )

    output = capsys.readouterr().out
    assert f"[gp] [{phase}] Finished {action_name} in " in output


def test_run_timed_query_logs_human_readable_elapsed(
    monkeypatch,
    capsys,
) -> None:
    perf_counter_values = iter([0.0, 3661.0])
    monkeypatch.setattr(
        query_timing_module.time,
        "perf_counter",
        lambda: next(perf_counter_values),
    )

    result = query_timing_module.run_timed_query("gp", lambda: "ok")

    output = capsys.readouterr().out
    assert result == "ok"
    assert "[gp] Finished SQL query in 1 hour 1 minute 1 second" in output


def test_format_plan_returns_stable_multistatement_text() -> None:
    plan = plans_module.SqlPlan(
        operation="copy",
        source_alias="gp",
        target_alias="trino",
        source_backend="gp",
        target_backend="trino",
        source_table="sandbox.source",
        target_table="sandbox.target",
        options={"write_mode": "append", "batch_size": 500},
        metadata=plans_module.SqlOperationMetadata(
            statement_count=2,
            stage_table="sandbox.stage",
        ),
    )
    plan.add(
        "select   id,\nname from sandbox.source",
        alias="gp",
        backend="gp",
        phase="read_source",
        source_table="sandbox.source",
    )
    plan.add(
        "insert into sandbox.target select * from sandbox.stage where long_column = 'abcdef'",
        alias="trino",
        backend="trino",
        phase="insert_target",
        source_table="sandbox.stage",
        target_table="sandbox.target",
    )

    text = plans_module.format_plan(plan, max_sql_chars=57)

    assert text == "\n".join(
        [
            "SqlPlan: copy",
            "Source: alias=gp backend=gp table=sandbox.source",
            "Target: alias=trino backend=trino table=sandbox.target",
            "Metadata: stage_table='sandbox.stage', statement_count=2",
            "Options: batch_size=500, write_mode='append'",
            "Statements:",
            (
                "  1. phase=read_source alias=gp backend=gp "
                "source=sandbox.source target=- "
                "sql=select id, name from sandbox.source"
            ),
            (
                "  2. phase=insert_target alias=trino backend=trino "
                "source=sandbox.stage target=sandbox.target "
                "sql=insert into sandbox.target select * from sandbox.stage..."
            ),
        ]
    )


def test_format_plan_can_omit_sql_previews() -> None:
    plan = plans_module.SqlPlan(operation="execute_sql")
    plan.add("select 1", alias="gp", backend="gp", phase="execute")

    text = sql_module.format_plan(plan, include_sql=False)

    assert "sql=" not in text
    assert "phase=execute alias=gp backend=gp" in text


def test_format_plan_rejects_invalid_max_sql_chars() -> None:
    with pytest.raises(ValueError, match="max_sql_chars"):
        plans_module.format_plan(
            plans_module.SqlPlan(operation="noop"),
            max_sql_chars=0,
        )


def test_plan_empty_properties_mapping_and_short_preview_paths() -> None:
    plan = plans_module.SqlPlan(operation="noop")
    text = plans_module.format_plan(plan)
    assert "Options: <none>" in text
    assert "Statements:\n  <none>" in text
    assert plans_module._format_mapping({}) == "<none>"
    assert plans_module._short_sql_preview("select 123", 3) == "sel"

    result = plans_module.SqlOperationResult(
        rows=0,
        metadata=plans_module.SqlOperationMetadata(
            inserted_rows=2,
            affected_rows=3,
        ),
    )
    assert result.inserted_rows == 2
    assert result.affected_rows == 3
    with pytest.raises(TypeError, match="SqlPlan"):
        plans_module.format_plan(object())  # type: ignore[arg-type]


def test_query_labels_handle_blank_values_and_missing_context_fields() -> None:
    assert labels_module.normalize_query_label(" \t") is None
    assert labels_module.airflow_query_label() is None
    assert labels_module.airflow_query_label(operation="  ") is None
    assert labels_module.airflow_query_label({"dag_id": "direct"}) == ("airflow dag=direct")
    assert labels_module.airflow_query_label({"task": object()}) is None
