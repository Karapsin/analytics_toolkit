from __future__ import annotations

import inspect
import importlib
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from tests.sql_fakes import (
    FakeClickHouseClient,
    FakeClickHouseResult,
    FakeDbapiConnection,
)


capabilities_module = importlib.import_module("analytics_toolkit.sql.core.capabilities")
identifiers_module = importlib.import_module("analytics_toolkit.sql.core.identifiers")
config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")
transfer_api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
models_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.models")
backend_registry_module = importlib.import_module("analytics_toolkit.sql.backends")
ddl_create_table_module = importlib.import_module("analytics_toolkit.sql.ddl.api")
ch_ctas_module = importlib.import_module("analytics_toolkit.sql.dml.table.ch_create_table_as")
operation_runner_module = importlib.import_module(
    "analytics_toolkit.sql.execution.operation_runner"
)
query_timing_module = importlib.import_module("analytics_toolkit.sql.execution.query_timing")
plans_module = importlib.import_module("analytics_toolkit.sql.execution.plans")
labels_module = importlib.import_module("analytics_toolkit.sql.execution.labels")
table_info_module = importlib.import_module("analytics_toolkit.sql.metadata.table_info")
sql_module = importlib.import_module("analytics_toolkit.sql")
cli_module = importlib.import_module("analytics_toolkit.cli")


class RoutingCursor:
    def __init__(self, connection: "RoutingDbapiConnection") -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []
        self.close_calls = 0

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.connection.executed.append(sql)
        self.connection.executed_params.append(params)
        self.rows = self.connection.resolve(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def close(self) -> None:
        self.close_calls += 1


class RoutingDbapiConnection:
    def __init__(
        self,
        resolver,
    ) -> None:
        self.resolver = resolver
        self.executed: list[str] = []
        self.executed_params: list[tuple[Any, ...] | None] = []
        self.close_calls = 0

    def cursor(self) -> RoutingCursor:
        return RoutingCursor(self)

    def resolve(
        self,
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        return self.resolver(sql, params)

    def close(self) -> None:
        self.close_calls += 1


class InspectableClickHouseClient:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.queries: list[str] = []
        self.close_calls = 0

    def query(self, sql: str) -> FakeClickHouseResult:
        self.queries.append(sql)
        if sql.startswith("EXISTS TABLE "):
            return FakeClickHouseResult([(1 if self.exists else 0,)])
        if sql.startswith("DESCRIBE TABLE "):
            return FakeClickHouseResult(
                [
                    ("id", "UInt64"),
                    ("name", "String"),
                ]
            )
        if sql.startswith("SELECT count()"):
            return FakeClickHouseResult([(17,)])
        return FakeClickHouseResult([])

    def close(self) -> None:
        self.close_calls += 1


def test_backend_support_matrix_includes_write_modes() -> None:
    rows = capabilities_module.support_matrix_rows()

    assert {row["backend"] for row in rows} == set(backend_registry_module.get_backend_names())
    for row in rows:
        assert "truncate_insert" in row["write_modes"]
        assert "upsert" in row["write_modes"]
        assert (
            capabilities_module.validate_write_mode(
                row["backend"],
                "upsert",
            )
            == "upsert"
        )


def test_capability_directory_lists_lazy_exports() -> None:
    assert "BackendCapability" in capabilities_module.__dir__()
    assert "WriteMode" in dir(capabilities_module)


def test_table_identifier_preserves_qualified_parts_and_quotes() -> None:
    identifier = identifiers_module.parse_table_identifier(
        'sandbox."Target Table"',
        "gp",
    )

    assert identifier.parts == ("sandbox", "Target Table")
    assert identifier.with_relation_suffix("_stage").render("gp") == (
        'sandbox."Target Table_stage"'
    )
    assert identifier.render_quoted("ch") == "`sandbox`.`Target Table`"


@pytest.mark.parametrize(
    ("backend", "table_name"),
    [
        ("gp", "sandbox.events"),
        ("gp", '"sandbox"."Target Table"'),
        ("trino", "catalog.schema.events"),
        ("trino", '"catalog"."schema"."Target Table"'),
        ("ch", "sandbox.events"),
        ("ch", "`sandbox`.`Target Table`"),
    ],
)
def test_table_identifier_round_trips_rendered_parts(
    backend: str,
    table_name: str,
) -> None:
    identifier = identifiers_module.parse_table_identifier(table_name, backend)
    rendered = identifier.render(backend)
    reparsed = identifiers_module.parse_table_identifier(rendered, backend)

    assert reparsed.parts == identifier.parts
    assert reparsed.quoted == identifier.quoted


def test_public_sql_type_aliases_are_exported() -> None:
    assert sql_module.BackendName is not None
    assert sql_module.ConnectionKey is str
    assert sql_module.SqlText is str
    assert sql_module.TableName is str
    assert sql_module.SqlTaskType is not None


def test_public_sql_facade_exports_refactored_helpers() -> None:
    assert sql_module.async_sql is not None
    assert sql_module.parallel_sql is not None
    assert sql_module.show_tables is not None
    assert sql_module.table_info is not None
    assert sql_module.format_plan is plans_module.format_plan
    assert sql_module.BACKEND_CAPABILITIES is capabilities_module.BACKEND_CAPABILITIES


@pytest.mark.parametrize(
    "function_name",
    [
        "drop_partitions",
        "drop_tables",
        "create_sql_table",
        "ch_reconfigure_table",
        "execute",
        "gp_create_partitions",
        "load_df",
        "transfer",
    ],
)
def test_public_mutating_sql_helpers_accept_dry_run_plan_options(
    function_name: str,
) -> None:
    signature = inspect.signature(getattr(sql_module, function_name))

    assert "dry_run" in signature.parameters
    assert "return_sql" in signature.parameters


@pytest.mark.parametrize(
    "function_name",
    [
        "async_sql",
        "execute_read",
        "execute",
        "load_df",
        "parallel_sql",
        "transfer",
    ],
)
def test_public_sql_progress_defaults_to_false(function_name: str) -> None:
    signature = inspect.signature(getattr(sql_module, function_name))

    assert signature.parameters["progress"].default is False


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


def test_public_sql_function_logs_total_elapsed_for_dry_run(capsys) -> None:
    plan = sql_module.load_df(
        "gp",
        "sandbox.scores",
        pd.DataFrame({"user_id": [1], "score": [10]}),
        write_mode="truncate_insert",
        dry_run=True,
    )

    output = capsys.readouterr().out
    assert plan.operation == "load_df"
    assert "[load_df] [timing] Finished SQL function in " in output


def test_execute_dry_run_public_timing_uses_optional_time_print_kwargs(
    capsys,
) -> None:
    plan = sql_module.execute("ch", "select 1", dry_run=True)

    output = capsys.readouterr().out
    assert isinstance(plan, plans_module.SqlPlan)
    assert plan.operation == "execute_sql"
    assert "[execute_sql] [timing] Finished SQL function in " in output


def test_drop_tables_dry_run_public_timing_uses_optional_time_print_kwargs(
    capsys,
) -> None:
    plan = sql_module.drop_tables(
        "ch",
        "sandbox.events",
        dry_run=True,
    )

    output = capsys.readouterr().out
    assert isinstance(plan, plans_module.SqlPlan)
    assert plan.operation == "drop_tables"
    assert "[drop_tables] [timing] Finished SQL function in " in output


def test_table_info_gp_reads_columns_and_skips_row_count_by_default(
    monkeypatch,
) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if sql == "SELECT to_regclass(%s)":
            assert params == ("sandbox.events",)
            return [("sandbox.events",)]
        if "information_schema.columns" in sql:
            assert params == ("sandbox", "events")
            return [
                ("id", "bigint", "int8", None, None),
                ("amount", "numeric", "numeric", 12, 2),
            ]
        if "COUNT" in sql:
            pytest.fail("count SQL should not run by default")
        return []

    connection = RoutingDbapiConnection(resolver)
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: connection,
    )

    info = table_info_module.table_info("gp", "sandbox.events")

    assert info.connection_key == "gp"
    assert info.backend == "gp"
    assert info.table == "sandbox.events"
    assert info.exists is True
    assert info.columns == {"id": "BIGINT", "amount": "NUMERIC(12, 2)"}
    assert info.row_count is None
    assert info.as_dict()["columns"] == info.columns
    frame = info.to_frame()
    assert frame["column_name"].tolist() == ["id", "amount"]
    assert frame["column_type"].tolist() == ["BIGINT", "NUMERIC(12, 2)"]
    assert connection.close_calls == 1


def test_table_info_row_count_when_requested(monkeypatch) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if sql == "SELECT to_regclass(%s)":
            return [("sandbox.events",)]
        if "information_schema.columns" in sql:
            return [("id", "bigint", "int8", None, None)]
        if sql == "SELECT COUNT(*) FROM sandbox.events":
            return [(42,)]
        return []

    connection = RoutingDbapiConnection(resolver)
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: connection,
    )

    info = table_info_module.table_info(
        "gp",
        "sandbox.events",
        include_row_count=True,
    )

    assert info.row_count == 42
    assert "SELECT COUNT(*) FROM sandbox.events" in connection.executed


def test_table_info_missing_table_skips_columns_and_row_count(monkeypatch) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if sql == "SELECT to_regclass(%s)":
            return []
        pytest.fail(f"unexpected SQL for missing table: {sql}")

    connection = RoutingDbapiConnection(resolver)
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: connection,
    )

    info = table_info_module.table_info(
        "gp",
        "sandbox.missing",
        include_row_count=True,
    )

    assert info.exists is False
    assert info.columns == {}
    assert info.row_count is None
    assert info.to_frame().iloc[0]["column_name"] is None


def test_table_info_trino_resolves_unqualified_and_schema_qualified_names(
    monkeypatch,
) -> None:
    def resolver(
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> list[tuple[Any, ...]]:
        if "information_schema.tables" in sql:
            return [(1,)]
        if "information_schema.columns" in sql:
            return [("id", "bigint"), ("score", "double")]
        if sql == "SELECT COUNT(*) FROM iceberg.sandbox.events":
            return [(9,)]
        return []

    first_connection = RoutingDbapiConnection(resolver)
    second_connection = RoutingDbapiConnection(resolver)
    connections = iter([first_connection, second_connection])
    monkeypatch.setattr(
        table_info_module,
        "get_sql_connection",
        lambda key: next(connections),
    )

    unqualified = table_info_module.table_info(
        "trino",
        "events",
        include_row_count=True,
    )
    schema_qualified = table_info_module.table_info("trino", "mart.events")

    assert unqualified.resolved_table == "iceberg.sandbox.events"
    assert unqualified.row_count == 9
    assert first_connection.executed_params[:2] == [
        ("sandbox", "events"),
        ("sandbox", "events"),
    ]
    assert schema_qualified.resolved_table == "iceberg.mart.events"
    assert second_connection.executed_params[:2] == [
        ("mart", "events"),
        ("mart", "events"),
    ]


def test_table_info_clickhouse_includes_shard_table(monkeypatch) -> None:
    client = InspectableClickHouseClient()
    monkeypatch.setattr(table_info_module, "get_sql_connection", lambda key: client)

    info = table_info_module.table_info(
        "ch",
        "analytics.events",
        include_row_count=True,
    )

    assert info.exists is True
    assert info.columns == {"id": "UInt64", "name": "String"}
    assert info.row_count == 17
    assert info.shard_table == "analytics.events_shard"
    assert info.resolved_table is None
    assert client.queries == [
        "EXISTS TABLE analytics.events",
        "DESCRIBE TABLE analytics.events",
        "SELECT count() FROM analytics.events",
    ]
    assert client.close_calls == 1


def test_table_info_validates_boolean_and_blank_table_name() -> None:
    with pytest.raises(ValueError, match="include_row_count"):
        table_info_module.table_info("gp", "events", include_row_count=1)
    with pytest.raises(table_info_module.InvalidSqlInputError, match="Table name"):
        table_info_module.table_info("gp", "  ")

    info = table_info_module.SqlTableInfo(
        connection_key="gp",
        backend="gp",
        table="events",
        exists=False,
        columns={},
        row_count=None,
        resolved_table=None,
        shard_table=None,
    )
    assert info.to_frame().loc[0, "column_name"] is None


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


def test_query_labels_handle_blank_values_and_missing_context_fields() -> None:
    assert labels_module.normalize_query_label(" \t") is None
    assert labels_module.airflow_query_label() is None
    assert labels_module.airflow_query_label(operation="  ") is None
    assert labels_module.airflow_query_label({"dag_id": "direct"}) == ("airflow dag=direct")
    assert labels_module.airflow_query_label({"task": object()}) is None


def test_load_df_dry_run_returns_ordered_labeled_plan() -> None:
    plan = load_df_module.load_df(
        "gp",
        "sandbox.scores",
        pd.DataFrame({"user_id": [1], "score": [10]}),
        write_mode="truncate_insert",
        dry_run=True,
        query_label="daily scores",
        gp_insert_chunk_size=5000,
    )

    assert plan.operation == "load_df"
    assert plan.target_alias == "gp"
    assert plan.options["gp_insert_chunk_size"] == 5000
    assert [statement.phase for statement in plan.statements] == [
        "clear_target",
        "create_target",
        "load_data",
        "analyze",
        "count_target",
    ]
    assert plan.sqls[0].startswith("/* analytics_toolkit query_label=daily scores */")
    assert "TRUNCATE TABLE sandbox.scores" in plan.sqls[0]


def test_load_df_dry_run_uses_table_schema() -> None:
    plan = load_df_module.load_df(
        "gp",
        "sandbox.scores",
        pd.DataFrame({"user_id": [1], "score": [10]}),
        dry_run=True,
        table_schema={"user_id": "TEXT", "score": "NUMERIC(8, 2)"},
    )

    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert plan.options["table_schema"] == {
        "user_id": "TEXT",
        "score": "NUMERIC(8, 2)",
    }
    assert '"user_id" TEXT' in create_sql
    assert '"score" NUMERIC(8, 2)' in create_sql


def test_load_df_upsert_dry_run_uses_backend_specific_sql() -> None:
    df = pd.DataFrame(
        {
            "id": [1],
            "sub_id": [None],
            "score": [10],
        }
    )

    gp_plan = load_df_module.load_df(
        "gp",
        "sandbox.scores",
        df,
        write_mode="upsert",
        key_columns=["id", "sub_id"],
        dry_run=True,
    )
    assert any("DELETE FROM sandbox.scores AS target_dst" in sql for sql in gp_plan.sqls)
    assert any("USING sandbox.scores__stage__dry_run AS stage_src" in sql for sql in gp_plan.sqls)
    assert any(
        'target_dst."sub_id" IS NULL AND stage_src."sub_id" IS NULL' in sql for sql in gp_plan.sqls
    )
    assert any(
        'INSERT INTO sandbox.scores ("id", "sub_id", "score") '
        'SELECT "id", "sub_id", "score" FROM' in sql
        for sql in gp_plan.sqls
    )

    trino_plan = load_df_module.load_df(
        "trino",
        "sandbox.scores",
        df,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        dry_run=True,
    )
    assert any("sandbox.scores__upsert_final__dry_run" in sql for sql in trino_plan.sqls)
    assert any("SELECT target_dst." in sql for sql in trino_plan.sqls)
    assert any("DROP PARTITION" in sql for sql in trino_plan.sqls)
    assert not any(sql.startswith("MERGE INTO") for sql in trino_plan.sqls)


def test_transfer_upsert_dry_run_uses_delete_insert_or_merge() -> None:
    gp_plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        table_schema={"id": "BIGINT", "score": "INTEGER"},
        dry_run=True,
    )
    assert any("DELETE FROM sandbox.scores AS target_dst" in sql for sql in gp_plan.sqls)
    assert any(
        'INSERT INTO sandbox.scores ("id", "score") SELECT CAST("id" AS BIGINT)' in sql
        for sql in gp_plan.sqls
    )

    trino_plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        table_schema={"id": "BIGINT", "score": "INTEGER"},
        dry_run=True,
    )
    assert any("sandbox.scores__upsert_final__dry_run" in sql for sql in trino_plan.sqls)
    assert any("DROP PARTITION" in sql for sql in trino_plan.sqls)
    assert not any(sql.startswith("MERGE INTO") for sql in trino_plan.sqls)

    ch_plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select id, score from source_table",
        to_table="analytics.scores",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        table_schema={"id": "UInt64", "score": "Int64"},
        ch_cluster="analytics",
        dry_run=True,
    )
    assert any(
        "ALTER TABLE analytics.scores_shard ON CLUSTER analytics DROP PARTITION" in sql
        for sql in ch_plan.sqls
    )
    assert not any(sql.startswith("DELETE FROM analytics.scores") for sql in ch_plan.sqls)
    assert any("INSERT INTO analytics.scores" in sql for sql in ch_plan.sqls)


def test_transfer_upsert_dry_run_infers_source_columns_without_table_schema() -> None:
    trino_plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        dry_run=True,
    )

    final_insert_sql = next(
        sql
        for sql in trino_plan.sqls
        if sql.startswith('INSERT INTO sandbox.scores__upsert_final__dry_run ("id", "score")')
        and 'SELECT "id", "score" FROM' in sql
    )
    assert 'SELECT CAST("id" AS BIGINT)' not in final_insert_sql
    assert 'SELECT "id", "score" FROM' in final_insert_sql

    gp_plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select id, score from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        dry_run=True,
    )

    assert any(
        'INSERT INTO sandbox.scores ("id", "score") SELECT "id", "score" FROM' in sql
        for sql in gp_plan.sqls
    )


def test_transfer_upsert_dry_run_uses_placeholder_for_unknown_source_columns() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="trino",
        to_db="gp",
        from_sql="select * from source_table",
        to_table="sandbox.scores",
        write_mode="upsert",
        key_columns=["id"],
        dry_run=True,
    )

    assert any("<source query columns>" in sql for sql in plan.sqls)
    assert not any('INSERT INTO sandbox.scores ("id")' in sql for sql in plan.sqls)


def test_load_df_passes_table_schema_to_create_sql_table(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    create_calls: list[dict[str, object]] = []

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: create_calls.append(kwargs),
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    inserted_rows = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1, 2], "amount": [1.5, 2.5]}),
        retry_cnt=1,
        timeout_increment=0,
        table_schema={"id": "TEXT", "amount": "NUMERIC(10, 2)"},
    )

    assert inserted_rows == 2
    assert create_calls[0]["table_schema"] == {
        "id": "TEXT",
        "amount": "NUMERIC(10, 2)",
    }


def test_load_df_return_metadata_preserves_rows_default_path(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    df = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_df_module, "count_table_rows", lambda *args, **kwargs: 5)

    result = load_df_module.load_df(
        "gp",
        "sandbox.target",
        df,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 2
    assert result.metadata.source_rows == 2
    assert result.metadata.inserted_rows == 2
    assert result.metadata.final_target_rows == 5


def test_load_df_upsert_requires_key_columns() -> None:
    with pytest.raises(ValueError, match="key_columns"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            write_mode="upsert",
            dry_run=True,
        )


def test_transfer_upsert_requires_key_columns() -> None:
    with pytest.raises(ValueError, match="key_columns"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            write_mode="upsert",
            dry_run=True,
        )


def test_load_df_rejects_invalid_gp_insert_chunk_size() -> None:
    with pytest.raises(ValueError, match="gp_insert_chunk_size"):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            gp_insert_chunk_size=0,
            dry_run=True,
        )

    with pytest.raises(ValueError, match="db_key has type 'gp'"):
        load_df_module.load_df(
            "trino",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            gp_insert_chunk_size=100,
            dry_run=True,
        )


def test_read_sql_prefixes_query_label(monkeypatch, capsys) -> None:
    connection = FakeDbapiConnection(
        rows=[(1,)],
        description=[("value",)],
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda key: connection)

    result = read_sql_module.read_sql(
        "gp",
        "select 1 as value",
        retry_cnt=1,
        timeout_increment=0,
        query_label="unit-test",
    )

    output = capsys.readouterr().out
    assert result["value"].tolist() == [1]
    assert "Executing query:" not in output
    assert "[read_sql] [gp/gp] [read] Finished SQL query in " in output
    assert ("Finished SQL statement:\n/* analytics_toolkit query_label=unit-test */") in output
    assert connection.executed[0].startswith("/* analytics_toolkit query_label=unit-test */")


def test_read_sql_return_metadata_preserves_dataframe(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        rows=[(1,), (2,)],
        description=[("value",)],
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda key: connection)

    result = read_sql_module.read_sql(
        "gp",
        "select value from source_table",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
        query_label="metadata-read",
        return_metadata=True,
    )

    assert result.rows == 2
    assert result.data["value"].tolist() == [1, 2]
    assert result.metadata.read_rows == 2
    assert result.metadata.statement_count == 1
    assert result.metadata.retry_attempts == 1
    assert result.metadata.elapsed_seconds >= 0
    assert result.metadata.operation_status == "success"
    assert result.metadata.query_label == "metadata-read"


def test_read_sql_with_metadata_delegates_to_shared_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    calls: list[dict[str, object]] = []

    def fake_impl(**kwargs: object) -> object:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(read_sql_module, "_read_sql_impl", fake_impl)
    result = read_sql_module.read_sql_with_metadata(
        "gp",
        "select 1",
        print_queries=True,
        retry_cnt=2,
        timeout_increment=0.5,
        query_label="metadata",
    )
    assert result is expected
    assert calls == [
        {
            "db_key": "gp",
            "query": "select 1",
            "print_queries": True,
            "retry_cnt": 2,
            "timeout_increment": 0.5,
            "query_label": "metadata",
            "return_metadata": True,
            "output_type": "df",
        }
    ]


def test_execute_sql_dry_run_does_not_open_connection(monkeypatch) -> None:
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = execute_sql_module.execute_sql(
        "trino",
        "select 1; select 2",
        dry_run=True,
        query_label="dry-exec",
    )

    assert plan.operation == "execute_sql"
    assert plan.target_alias == "trino"
    assert [statement.phase for statement in plan.statements] == [
        "execute",
        "execute",
    ]
    assert plan.options["print_queries"] is False
    assert "random_sleep_seconds" not in plan.options
    assert plan.metadata.statement_count == 2
    assert sum("query_label=dry-exec" in sql for sql in plan.sqls) == 1


def test_execute_sql_trino_executes_split_statements_in_order(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: connection,
    )

    execute_sql_module.execute_sql(
        "trino",
        "select 1; select 2; select 3",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert connection.executed == ["select 1", "select 2", "select 3"]
    assert connection.close_calls == 1


def test_execute_sql_logs_elapsed_for_each_statement_by_default(
    monkeypatch,
    capsys,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: connection,
    )

    execute_sql_module.execute_sql(
        "trino",
        "select 1; select 2",
        retry_cnt=1,
        timeout_increment=0,
    )

    output = capsys.readouterr().out
    assert "Executing query:" not in output
    assert output.count("[execute_sql] [trino/trino] [execute] Finished SQL query in ") == 2
    assert "Finished SQL statement:\nselect 1; select 2" in output
    assert "[execute_sql] [trino/trino] [close] Closing connection" in output


def test_execute_sql_progress_false_suppresses_statement_bar(
    monkeypatch,
    capsys,
) -> None:
    progress_bars: list[object] = []

    class FakeTqdm:
        def __init__(self, values, **kwargs) -> None:
            progress_bars.append((list(values), kwargs))
            self.values = values

        def __iter__(self):
            return iter(self.values)

    client = FakeClickHouseClient()
    monkeypatch.setattr(execute_sql_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: client,
    )

    execute_sql_module.execute_sql(
        "ch",
        "select 1; select 2",
        retry_cnt=1,
        timeout_increment=0,
        progress=False,
    )

    output = capsys.readouterr().out
    assert progress_bars == []
    assert client.commands == ["select 1", "select 2"]
    assert "Finished SQL statement:\nselect 1; select 2" in output


def test_execute_and_read_validation_and_direct_helper_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(execute_sql_module.InvalidSqlInputError):
        execute_sql_module._build_execute_sql_options(
            db_key="gp",
            query="  ",
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            retry_cnt=1,
            timeout_increment=0,
            query_label=None,
            dry_run=False,
            return_sql=False,
            return_metadata=False,
            progress=False,
        )
    with pytest.raises(read_sql_module.InvalidSqlInputError):
        read_sql_module._build_read_sql_options(
            db_key="gp",
            query=" ",
            print_queries=False,
            retry_cnt=1,
            timeout_increment=0,
            query_label=None,
            return_metadata=False,
            output_type="df",
        )
    with pytest.raises(read_sql_module.InvalidSqlInputError, match="exactly one"):
        read_sql_module._build_read_sql_options(
            db_key="gp",
            query="select 1; select 2",
            print_queries=False,
            retry_cnt=1,
            timeout_increment=0,
            query_label=None,
            return_metadata=False,
            output_type="df",
        )

    commands: list[str] = []
    execute_sql_module._execute_ch_statement(
        type("Client", (), {"command": lambda self, sql: commands.append(sql)})(),
        "select 1",
    )
    execute_sql_module._execute_trino_statement(
        type("Cursor", (), {"execute": lambda self, sql: commands.append(sql)})(),
        "select 2",
    )
    assert commands == ["select 1", "select 2"]

    printed: list[str] = []
    monkeypatch.setattr(execute_sql_module, "time_print", printed.append)
    execute_sql_module._maybe_print_query("select 1; select 2", True, True)
    execute_sql_module._maybe_print_query("select 3", True, False)
    execute_sql_module._maybe_print_query(" ; ", True, True)
    assert printed == [
        "Executing query:\nselect 1",
        "Executing query:\nselect 3",
        "Executing query:\n",
    ]

    printed.clear()
    monkeypatch.setattr(read_sql_module, "time_print", printed.append)
    read_sql_module._maybe_print_query("select 1", True)
    read_sql_module._maybe_print_query(" ; ", True)
    assert printed == ["Executing query:\nselect 1", "Executing query:\n;"]


def test_execute_statement_progress_wraps_multiple_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_tqdm(values, **kwargs):
        calls.append((list(values), kwargs))
        return values

    monkeypatch.setattr(execute_sql_module, "tqdm", fake_tqdm)
    assert list(
        execute_sql_module._iterate_statements_with_progress(
            ["select 1", "select 2"],
            "gp",
            progress=True,
        )
    ) == ["select 1", "select 2"]
    assert calls == [(["select 1", "select 2"], {"desc": "gp statements", "unit": "stmt"})]


def test_execute_sql_clickhouse_executes_split_statements_in_order(monkeypatch) -> None:
    client = FakeClickHouseClient()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: client,
    )

    execute_sql_module.execute_sql(
        "ch",
        "CREATE TABLE tmp (id UInt64); INSERT INTO tmp VALUES (1); DROP TABLE tmp",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert client.commands == [
        "CREATE TABLE tmp (id UInt64)",
        "INSERT INTO tmp VALUES (1)",
        "DROP TABLE tmp",
    ]
    assert client.close_calls == 1


def test_execute_sql_rejects_removed_random_sleep_seconds() -> None:
    with pytest.raises(TypeError, match="random_sleep_seconds"):
        execute_sql_module.execute_sql(
            "trino",
            "select 1",
            random_sleep_seconds=None,
        )


def test_execute_sql_return_metadata_reports_attempt_and_statement_count(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = execute_sql_module.execute_sql(
        "gp",
        "select 1",
        print_queries=False,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows is None
    assert result.metadata.statement_count == 1
    assert result.metadata.retry_attempts == 1
    assert result.metadata.elapsed_seconds >= 0
    assert result.metadata.operation_status == "success"


def test_execute_read_return_metadata_preserves_dataframe(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        rows=[(1, "ok")],
        description=[("id",), ("status",)],
    )
    monkeypatch.setattr(
        execute_read_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = execute_read_module.execute_read(
        "gp",
        "CREATE TEMP TABLE tmp AS SELECT 1; SELECT id, status FROM tmp",
        print_queries=False,
        gp_break_query=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 1
    assert result.data["status"].tolist() == ["ok"]
    assert result.metadata.read_rows == 1
    assert result.metadata.statement_count == 2
    assert result.metadata.operation_status == "success"


def test_execute_read_rejects_removed_random_sleep_seconds() -> None:
    with pytest.raises(TypeError, match="random_sleep_seconds"):
        execute_read_module.execute_read(
            "trino",
            "select 1",
            random_sleep_seconds=None,
        )


def test_transfer_dry_run_includes_source_stage_and_target_steps() -> None:
    signature = inspect.signature(sql_module.transfer)

    assert "replace_target_table" not in signature.parameters
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        dry_run=True,
        query_label="copy-target",
    )

    assert plan.operation == "transfer_table"
    assert plan.source_alias == "gp"
    assert plan.target_alias == "trino"
    assert plan.options["adaptive_batch_size"] is True
    assert plan.options["min_batch_size"] == 1_000
    assert plan.options["max_batch_size"] == 400_000
    assert plan.options["target_batch_seconds"] == 10.0
    assert plan.statements[0].phase == "read_source"
    assert "query_label=copy-target" in plan.statements[0].sql
    assert plan.statements[-1].phase == "drop_stage"


def test_transfer_rejects_removed_replace_target_table_argument() -> None:
    with pytest.raises(TypeError, match="replace_target_table"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            replace_target_table=True,
        )


def test_transfer_table_dry_run_uses_table_schema() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id, amount from source_table",
        to_table="sandbox.target",
        dry_run=True,
        table_schema={"id": "VARCHAR", "amount": "DECIMAL(10, 2)"},
    )

    create_sqls = [
        statement.sql
        for statement in plan.statements
        if statement.phase in {"create_stage", "create_target"}
    ]
    assert plan.options["table_schema"] == {
        "id": "VARCHAR",
        "amount": "DECIMAL(10, 2)",
    }
    assert len(create_sqls) == 2
    assert all('"id" VARCHAR' in sql for sql in create_sqls)
    assert all('"amount" DECIMAL(10, 2)' in sql for sql in create_sqls)


def test_transfer_table_logs_source_sql_preview(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        transfer_api_module,
        "run_transfer_attempt",
        lambda **kwargs: 3,
    )

    result = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="\n\nselect id from source_table",
        to_table="sandbox.target",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        progress=False,
    )

    output = capsys.readouterr().out
    assert result == 3
    assert "[transfer_table] [trino/trino] [transfer] Finished SQL in " in output
    assert "Finished SQL statement:\nselect id from source_table" in output


def test_transfer_return_metadata_includes_row_count_validation(monkeypatch) -> None:
    def fake_run_transfer_attempt(**kwargs: Any) -> int:
        object.__setattr__(
            kwargs["options"],
            "row_count_result",
            models_module.TransferRowCountResult(
                expected_source_rows=3,
                streamed_rows=3,
                stage_rows=3,
                row_count_validated=True,
            ),
        )
        return 3

    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", fake_run_transfer_attempt)
    monkeypatch.setattr(transfer_api_module, "count_table_rows", lambda *args, **kwargs: 3)

    result = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        return_metadata=True,
    )

    assert result.metadata.expected_source_rows == 3
    assert result.metadata.streamed_rows == 3
    assert result.metadata.stage_rows == 3
    assert result.metadata.row_count_validated is True


def test_create_sql_table_logs_generated_sql_preview(monkeypatch, capsys) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        lambda _key: connection,
    )

    ddl_create_table_module.create_sql_table(
        db_key="gp",
        table_name="sandbox.created_table",
        df=pd.DataFrame({"id": [1]}),
        retry_cnt=1,
        timeout_increment=0,
    )

    output = capsys.readouterr().out
    assert "[create_sql_table] [gp/gp] [create_target] Finished SQL in " in output
    assert "Finished SQL statement:\nCREATE TABLE sandbox.created_table" in output
    assert connection.executed[0].startswith("CREATE TABLE sandbox.created_table")


def test_create_sql_table_only_generate_sql_accepts_schema_without_dataframe(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        lambda _key: pytest.fail("connection should not be opened"),
    )

    ddl = ddl_create_table_module.create_sql_table(
        db_key="gp",
        table_name="sandbox.schema_only",
        table_schema={"user_id": "BIGINT", "score": "DOUBLE PRECISION"},
        only_generate_sql=True,
    )

    assert "CREATE TABLE sandbox.schema_only" in ddl
    assert '"user_id" BIGINT' in ddl
    assert '"score" DOUBLE PRECISION' in ddl


def test_create_sql_table_accepts_schema_without_dataframe(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    opened_keys: list[str] = []

    def fake_get_sql_connection(db_key: str) -> FakeDbapiConnection:
        opened_keys.append(db_key)
        return connection

    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        fake_get_sql_connection,
    )

    ddl_create_table_module.create_sql_table(
        db_key="gp_sandbox",
        table_name="sandbox.schema_only",
        table_schema={"user_id": "BIGINT", "score": "DOUBLE PRECISION"},
        retry_cnt=1,
        timeout_increment=0,
    )

    assert opened_keys == ["gp_sandbox"]
    assert connection.executed[0].startswith("CREATE TABLE sandbox.schema_only")
    assert '"user_id" BIGINT' in connection.executed[0]
    assert connection.close_calls == 1


def test_create_sql_table_dry_run_and_return_sql_do_not_open_connection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ddl_create_table_module,
        "get_sql_connection",
        lambda _key: pytest.fail("connection should not be opened"),
    )

    dry_run_plan = ddl_create_table_module.create_sql_table(
        db_key="gp",
        table_name="sandbox.schema_only",
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )
    return_sql_plan = ddl_create_table_module.create_sql_table(
        db_key="gp",
        table_name="sandbox.schema_only",
        table_schema={"id": "BIGINT"},
        return_sql=True,
    )

    assert dry_run_plan.sqls == return_sql_plan.sqls
    assert dry_run_plan.sqls[0].startswith("CREATE TABLE sandbox.schema_only")


def test_load_df_clickhouse_dry_run_preserves_lifecycle_order_and_cluster() -> None:
    plan = load_df_module.load_df(
        "ch",
        "analytics.events",
        pd.DataFrame({"dt": ["2024-01-01"], "id": [1]}),
        write_mode="truncate_insert",
        dry_run=True,
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_cluster="analytics",
    )

    assert plan.statements[0].phase == "clear_target"
    assert plan.sqls[0] == ("TRUNCATE TABLE IF EXISTS analytics.events_shard ON CLUSTER analytics")
    assert plan.sqls[1] == "TRUNCATE TABLE IF EXISTS analytics.events"
    assert plan.statements[2].phase == "create_target"
    assert plan.sqls[2].startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
    assert "ON CLUSTER analytics" in plan.sqls[2]


def test_load_df_clickhouse_only_shard_dry_run_uses_local_target() -> None:
    plan = load_df_module.load_df(
        "ch",
        "analytics.events",
        pd.DataFrame({"dt": ["2024-01-01"], "id": [1]}),
        write_mode="truncate_insert",
        ch_only_shard=True,
        dry_run=True,
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    assert plan.sqls[0] == "TRUNCATE TABLE IF EXISTS analytics.events"
    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert create_sql.startswith("CREATE TABLE IF NOT EXISTS analytics.events")
    assert "ENGINE = ReplicatedMergeTree" in create_sql
    assert "PARTITION BY `dt`" in create_sql
    assert "ORDER BY (`dt`, `id`)" in create_sql
    assert "_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER" not in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)


def test_transfer_clickhouse_dry_run_preserves_drop_pair_cluster() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select id from source_table",
        to_table="analytics.events",
        dry_run=True,
        ch_cluster="analytics",
    )

    drop_sqls = [statement.sql for statement in plan.statements if statement.phase == "drop_target"]
    assert drop_sqls == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_shard",
        "DROP TABLE IF EXISTS analytics.events ON CLUSTER analytics",
        "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER analytics",
    ]


def test_transfer_clickhouse_only_shard_dry_run_uses_local_target_sql() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="ch",
        from_sql="select dt, id from source_table",
        to_table="analytics.events",
        ch_only_shard=True,
        dry_run=True,
        table_schema={"dt": "Date", "id": "UInt64"},
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    drop_sqls = [statement.sql for statement in plan.statements if statement.phase == "drop_target"]
    assert drop_sqls == ["DROP TABLE IF EXISTS analytics.events"]
    target_create_sql = [
        statement.sql
        for statement in plan.statements
        if statement.phase == "create_target" and statement.target_table == "analytics.events"
    ][0]
    assert target_create_sql.startswith("CREATE TABLE IF NOT EXISTS analytics.events")
    assert "ENGINE = ReplicatedMergeTree" in target_create_sql
    assert "PARTITION BY `dt`" in target_create_sql
    assert "ORDER BY (`dt`, `id`)" in target_create_sql
    assert "_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER" not in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)


def test_create_sql_table_from_sql_clickhouse_dry_run_uses_shared_plan_steps() -> None:
    plan = ddl_create_table_module.create_sql_table(
        "ch",
        "analytics.events",
        sql="select id from source_table",
        source_db="gp",
        drop_target_if_exists=True,
        insert_data=True,
        dry_run=True,
        ch_cluster="analytics",
    )

    assert [statement.phase for statement in plan.statements] == [
        "inspect_source_schema",
        "drop_target",
        "drop_target",
        "drop_target",
        "drop_target",
        "create_target",
        "insert_data",
    ]
    assert plan.sqls[3] == "DROP TABLE IF EXISTS analytics.events ON CLUSTER analytics"


def test_create_sql_table_from_sql_clickhouse_only_shard_dry_run_uses_local_target() -> None:
    plan = ddl_create_table_module.create_sql_table(
        "ch",
        "analytics.events",
        sql="select dt, id from source_table",
        source_db="gp",
        drop_target_if_exists=True,
        insert_data=True,
        dry_run=True,
        ch_only_shard=True,
        partition_by=["dt"],
        order_by=["dt", "id"],
        ch_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    assert [statement.phase for statement in plan.statements] == [
        "inspect_source_schema",
        "drop_target",
        "create_target",
        "insert_data",
    ]
    assert plan.sqls[1] == "DROP TABLE IF EXISTS analytics.events"
    assert plan.sqls[2] == "CREATE TABLE analytics.events (<source query schema>)"
    assert "_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER" not in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)


def test_ch_create_table_as_dry_run_uses_lifecycle_drop_order() -> None:
    plan = ch_ctas_module.ch_create_table_as(
        "ch",
        "analytics.events",
        "select 1 as id",
        dry_run=True,
        ch_cluster="analytics",
    )

    assert plan.sqls[:4] == [
        "DROP TABLE IF EXISTS analytics.events",
        "DROP TABLE IF EXISTS analytics.events_shard",
        "DROP TABLE IF EXISTS analytics.events ON CLUSTER analytics",
        "DROP TABLE IF EXISTS analytics.events_shard ON CLUSTER analytics",
    ]


def test_ch_create_table_as_only_shard_dry_run_uses_local_target() -> None:
    plan = ch_ctas_module.ch_create_table_as(
        "ch",
        "analytics.events",
        "select 1 as id",
        dry_run=True,
        table_schema={"id": "UInt64"},
        ch_only_shard=True,
        ch_cluster="analytics",
    )

    assert plan.options["ch_only_shard"] is True
    assert [statement.phase for statement in plan.statements] == [
        "drop_target",
        "create_target",
        "insert_target",
    ]
    assert plan.sqls[0] == "DROP TABLE IF EXISTS analytics.events"
    assert plan.sqls[1].startswith("CREATE OR REPLACE TABLE analytics.events")
    assert "ENGINE = ReplicatedMergeTree" in plan.sqls[1]
    assert "_shard" not in "\n".join(plan.sqls)
    assert "ON CLUSTER" not in "\n".join(plan.sqls)
    assert "ENGINE = Distributed(" not in "\n".join(plan.sqls)


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
