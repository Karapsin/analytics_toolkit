"""Acknowledged stage resources survive readiness failures in cleanup scope."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.ddl.ownership import execute_owned_create_sqls
from analytics_toolkit.sql.dml.load.models import LoadOptions, LoadState
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_lifecycle import install_lifecycle
from tests.sql._support.load_table import load_df_module
from tests.sql._support.transfer_schema import stage_module


def test_load_readiness_failure_cleans_acknowledged_partial_pair_only(
    monkeypatch, write_sql_connections
):
    server = install_lifecycle(monkeypatch, write_sql_connections, driver="http")
    before = server.snapshot()
    options = LoadOptions(
        connection_key="ch",
        connection_backend="ch",
        destination_table="analytics.target",
        append=True,
        write_mode="append",
        key_columns=["id"],
    )
    state = LoadState(target_exists=True, original_target_exists=True)
    stage, physical = "analytics.stage", "analytics.stage_shard"
    monkeypatch.setattr(stage_module, "build_stage_table_name", lambda *_a, **_k: stage)
    monkeypatch.setattr(stage_module, "table_exists", lambda *_a, **_k: False)
    client = load_df_module.get_sql_connection("ch")

    def create(*_args, **kwargs):
        execute_owned_create_sqls(
            get_backend_adapter("ch"),
            client,
            [f"CREATE TABLE {physical} (id Int64) ENGINE=MergeTree ORDER BY id"],
            kwargs["on_resource_created"],
            on_scoped_resource_created=kwargs["on_scoped_resource_created"],
            connection_key="ch",
        )
        message = "created shard is not ready"
        raise SqlTableReadinessError(message)

    monkeypatch.setattr(stage_module, "_create_sql_table_with_connection", create)
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": client}),
    )
    with pytest.raises(SqlTableReadinessError):
        load_df_module._load_dataframe(options, state, pd.DataFrame({"id": [1]}))
    assert state.overlap_stage_table is None
    assert state.owned_stage_resources == [(physical, stage, None)]
    assert len(state.owned_stage_scoped_resources) == 1
    (resource,) = state.owned_stage_scoped_resources
    assert resource.name == physical
    assert resource.certificate is not None
    assert resource.scope.kind == "local"
    load_df_module._cleanup_load(options, state)
    dropped = [
        table
        for statement in server.commands
        if isinstance(
            (tree := parse_one(strip_query_label(statement), read=ClickHouseDialect)), exp.Drop
        )
        for table in tree.find_all(exp.Table)
    ]
    assert len(dropped) == 1
    assert dropped[0].db == "analytics"
    assert dropped[0].name == "stage_shard"
    assert server.snapshot() == before
    client.close()


def test_collision_and_ambiguous_candidates_never_become_owned(monkeypatch):
    names = iter(["db.collision", "db.ambiguous"])
    candidates = []
    owned = []
    monkeypatch.setattr(stage_module, "build_stage_table_name", lambda *_a, **_k: next(names))
    monkeypatch.setattr(stage_module, "table_exists", lambda *_a, **_k: False)
    attempts = []

    def create(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            error = RuntimeError("already exists")
            error.error_name = "TABLE_ALREADY_EXISTS"
            raise error
        message = "CREATE acknowledgement lost"
        raise ConnectionError(message)

    monkeypatch.setattr(stage_module, "_create_sql_table_with_connection", create)
    with pytest.raises(ConnectionError) as error:
        stage_module.create_stage_table(
            "ch",
            object(),
            "db.target",
            pd.DataFrame({"id": [1]}),
            on_stage_candidate=candidates.append,
            on_stage_owned=owned.append,
            on_resource_created=owned.append,
        )
    assert candidates == ["db.collision", "db.ambiguous"]
    assert owned == []
    assert error.value.analytics_toolkit_sql_stage_candidate == "db.ambiguous"


@pytest.mark.parametrize("fail", [False, True])
def test_policy_stage_cleanup_replaces_failed_connection_before_retry(monkeypatch, fail):
    connection = object()
    ref = {"connection": connection}
    events = []
    message = "cleanup endpoint unavailable"
    failure = ConnectionError(message)

    def cleanup(*args, **_kwargs):
        events.append(("drop", args[1]))
        if fail:
            raise failure

    monkeypatch.setattr(stage_module, "cleanup_stage_table", cleanup)
    kwargs = {
        "retry_fn": lambda **request: request["operation"](1),
        "retry_cnt": 1,
        "timeout_increment": 0,
        "rollback_fn": lambda current: events.append(("rollback", current)),
        "replace_connection_fn": lambda key, current: events.append(("replace", key, current)),
        "ch_creation_policy": SimpleNamespace(create_distributed_pair=False),
    }
    if fail:
        with pytest.raises(ConnectionError) as raised:
            stage_module.cleanup_stage_table_with_retry("ch", "ch", ref, "db.stage", **kwargs)
        assert raised.value is failure
        assert events == [("drop", connection), ("rollback", connection), ("replace", "ch", ref)]
    else:
        stage_module.cleanup_stage_table_with_retry("ch", "ch", ref, "db.stage", **kwargs)
        assert events == [("drop", connection)]


def test_successful_stage_creation_notifies_ownership_once_without_resource_observer(monkeypatch):
    owned = []
    monkeypatch.setattr(stage_module, "build_stage_table_name", lambda *_a, **_k: "db.stage")
    monkeypatch.setattr(stage_module, "table_exists", lambda *_a, **_k: False)

    def create(*_args, **kwargs):
        kwargs["on_resource_created"]("db.stage")

    monkeypatch.setattr(stage_module, "_create_sql_table_with_connection", create)
    assert (
        stage_module.create_stage_table(
            "ch",
            object(),
            "db.target",
            pd.DataFrame({"id": [1]}),
            on_stage_owned=owned.append,
        )
        == "db.stage"
    )
    assert owned == ["db.stage"]


def test_owned_load_cleanup_deduplicates_resources_and_preserves_primary_error(monkeypatch):
    options = LoadOptions("gp", "gp", "public.target")
    state = LoadState(True, True)
    state.owned_stage_resources = [("s1", "s1", None), ("s2", "s2", None), ("s1", "s1", None)]
    attempts = []
    monkeypatch.setattr(
        load_df_module,
        "_run_load_target_action",
        lambda _options, _role, operation: operation({"connection": object()}),
    )

    def drop(_adapter, _connection, resource, **_kwargs):
        attempts.append(resource)
        if resource == "s2":
            message = "drop unavailable"
            raise ConnectionError(message)

    monkeypatch.setattr(load_df_module, "drop_owned_resource", drop)
    load_df_module._cleanup_owned_load_stages(options, state)
    assert attempts == ["s1", "s2"]


def test_non_clickhouse_cleanup_keeps_created_target_operation_label(monkeypatch):
    calls = []
    monkeypatch.setattr(
        stage_module, "drop_table_with_retry", lambda *_a, **kwargs: calls.append(kwargs)
    )
    stage_module.cleanup_stage_table_with_retry(
        "gp",
        "gp",
        {"connection": object()},
        "sandbox.target",
        retry_fn=lambda **_k: None,
        retry_cnt=1,
        timeout_increment=0,
        rollback_fn=lambda _connection: None,
        replace_connection_fn=lambda *_a: None,
        operation_label="created target table",
    )
    assert calls[0]["operation_label"] == "created target table"


def test_existing_outage_target_is_not_recreated_or_reinferred(monkeypatch):
    options = LoadOptions("ch", "ch", "db.target", append=True, write_mode="append")
    state = LoadState(True, True)
    client = SimpleNamespace(_ch_outage_client=True)
    monkeypatch.setattr(
        load_df_module,
        "_create_load_target_table",
        lambda *_a, **_k: pytest.fail("unexpected CREATE"),
    )
    load_df_module._ensure_load_target_table(options, state, client, pd.DataFrame({"id": [1]}))
    upsert_options = LoadOptions("ch", "ch", "db.target", write_mode="upsert")
    load_df_module._load_target_column_metadata(
        upsert_options, state, client, pd.DataFrame({"id": [1]})
    )
    assert state.target_column_types is None
