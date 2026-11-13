"""Key-validated append plans preserve incoming column names and order."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.execution.plans import SqlPlan
from sqlglot import exp, parse_one

from tests.sql._support.load_column_state import ColumnConnection, existing_column_target
from tests.sql._support.load_table import _write_trino_connections, load_df_module


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("planning_flag", ["dry_run", "return_sql"])
@pytest.mark.parametrize("subset", [False, True], ids=["reordered", "subset"])
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["inferred", "explicit"])
def test_key_validated_append_plan_names_and_projects_incoming_columns(
    monkeypatch, backend, planning_flag, subset, explicit_schema
):
    opened = []

    def unexpected_connection(key):
        opened.append(key)
        message = "Offline append planning opened a SQL connection."
        raise AssertionError(message)

    monkeypatch.setattr(load_df_module, "get_sql_connection", unexpected_connection)
    frame = pd.DataFrame({"id": [7]}) if subset else pd.DataFrame({"value": [9], "id": [7]})
    original_frame = frame.copy(deep=True)
    native = "Int64" if backend == "ch" else "BIGINT"
    schema = {name: native for name in ("id", "value") if name in frame.columns}
    options = {"table_schema": schema} if explicit_schema else {}
    if backend == "ch":
        options.update(
            ch_shard_on_cluster="audit_cluster",
            ch_distributed_on_cluster="audit_cluster",
            ch_distributed_cluster="audit_cluster",
        )

    plan = sql.load_df(
        backend,
        "sandbox.events",
        frame,
        write_mode="append",
        key_columns=["id"],
        **options,
        **{planning_flag: True},
    )

    assert isinstance(plan, SqlPlan)
    assert opened == []
    pd.testing.assert_frame_equal(frame, original_frame)
    assert plan.options["write_mode"] == "append"
    assert plan.options["key_columns"] == ["id"]
    assert plan.metadata.source_rows == plan.metadata.staged_rows == 1
    stage = plan.metadata.stage_table
    assert stage is not None
    assert stage != "sandbox.events"
    phases = [statement.phase for statement in plan.statements]
    lifecycle = ["create_stage", "load_stage", "insert_from_stage", "drop_stage"]
    assert [phase for phase in phases if phase in lifecycle] == lifecycle
    assert "clear_target" not in phases
    assert "drop_target" not in phases
    assert phases[-1] == "count_target"
    statements = {statement.phase: statement for statement in plan.statements}
    for phase in ("create_stage", "load_stage", "drop_stage"):
        assert statements[phase].target_table == stage
    final = statements["insert_from_stage"]
    assert final.target_table == "sandbox.events"
    assert final.source_table == stage
    dialect = ClickHouseDialect if backend == "ch" else "postgres" if backend == "gp" else "trino"
    insert = parse_one(final.sql, read=dialect)
    assert isinstance(insert, exp.Insert)
    assert [column.name for column in insert.this.expressions] == list(frame.columns)
    assert isinstance(insert.expression, exp.Select)
    assert not list(insert.expression.find_all(exp.Star))
    assert [
        expression.find(exp.Column).name for expression in insert.expression.expressions
    ] == list(frame.columns)
    expected_source = parse_one(f"SELECT * FROM {stage}", read=dialect).args["from_"].this
    actual_source = insert.expression.args["from_"].this
    assert (actual_source.db, actual_source.name) == (expected_source.db, expected_source.name)


def test_empty_existing_greenplum_keep_policy_preserves_seed_and_closes_connection(monkeypatch):
    store = existing_column_target(monkeypatch, "gp", real_metadata=True)
    original_rows = deepcopy(store.rows)
    connections = []

    class RecordingConnection(ColumnConnection):
        def __init__(self):
            super().__init__(store)
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            super().close()

    def connect(_key):
        connection = RecordingConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    frame = pd.DataFrame({"id": pd.Series([], dtype="int64")})
    original_frame = frame.copy(deep=True)

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            table_schema={"id": "INTEGER"},
            write_mode="truncate_insert",
            empty_source_policy="keep",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 0
    )

    assert store.rows == original_rows
    assert store.submissions == []
    assert len(connections) == 1
    assert connections[0].close_calls == 1
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("planning_flag", ["dry_run", "return_sql"])
@pytest.mark.parametrize("subset", [False, True], ids=["reordered", "subset"])
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["inferred", "explicit"])
def test_parquet_append_plan_names_incoming_columns_and_cleans_owned_resources(
    monkeypatch, write_sql_connections, planning_flag, subset, explicit_schema
):
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
    )
    opened = []

    def unexpected_connection(key):
        opened.append(key)
        message = "Parquet append planning opened a SQL connection."
        raise AssertionError(message)

    monkeypatch.setattr(load_df_module, "get_sql_connection", unexpected_connection)
    frame = pd.DataFrame({"id": [7]}) if subset else pd.DataFrame({"value": [9], "id": [7]})
    options = (
        {"table_schema": {name: "BIGINT" for name in ("id", "value") if name in frame.columns}}
        if explicit_schema
        else {}
    )

    plan = sql.load_df(
        "trino_stage",
        "iceberg.sandbox.events",
        frame,
        write_mode="append",
        key_columns=["id"],
        **options,
        **{planning_flag: True},
    )

    assert isinstance(plan, SqlPlan)
    assert opened == []
    assert plan.options["use_parquet_staging"] is True
    assert [statement.phase for statement in plan.statements] == [
        "create_stage",
        "load_stage",
        "insert_from_stage",
        "drop_stage",
        "cleanup_stage_location",
        "analyze",
        "count_target",
    ]
    stage = plan.metadata.stage_table
    location = plan.metadata.stage_external_location
    assert stage is not None
    assert location is not None
    assert location.startswith("s3://bucket/tmp/analytics_toolkit_transfer/")
    statements = {statement.phase: statement for statement in plan.statements}
    final = statements["insert_from_stage"]
    assert final.target_table == "iceberg.sandbox.events"
    assert final.source_table == stage
    insert = parse_one(final.sql, read="trino")
    assert [column.name for column in insert.this.expressions] == list(frame.columns)
    assert not list(insert.expression.find_all(exp.Star))
    assert [
        expression.find(exp.Column).name for expression in insert.expression.expressions
    ] == list(frame.columns)
    assert statements["drop_stage"].target_table == stage
    assert location in statements["load_stage"].sql
    assert location in statements["cleanup_stage_location"].sql
