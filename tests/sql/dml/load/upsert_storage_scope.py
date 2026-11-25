"""Existing ClickHouse storage scopes override desired future creation settings."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, ClusterRoutingClient
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint
from analytics_toolkit.sql.dml.table.write_modes import upsert_stage_table
from analytics_toolkit.sql.dml.transfer.flow.finalize import _finalize_target_once
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions, TransferStageState
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.load_table import load_df_module
from tests.sql._support.outage import Client, Cluster
from tests.sql._support.upsert_metadata import UpsertMetadata
from tests.sql._support.upsert_scope import (
    EXPECTED_ROWS,
    INCOMING_ROWS,
    UNRELATED_ROWS,
    StatefulUpsertClient,
)


@pytest.fixture(autouse=True)
def configured_creation_scopes(write_sql_connections):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch"]["database"] = "db"
    defaults = connections["ch"]["ddl_defaults"]
    defaults["regular"]["shard"]["on_cluster"] = "caller_a"
    defaults["regular"]["distributed"]["cluster"] = "caller_a"
    defaults["regular"]["distributed"]["on_cluster"] = "facade_c"
    defaults["staging"]["shard"]["on_cluster"] = "caller_a"
    write_sql_connections(connections)


class ExistingStorageClient:
    def __init__(self, *, local=False):
        self.local = local
        self.commands = []
        self.queries = []
        self.mutations = []
        self.metadata = UpsertMetadata(local=local, physical_table="custom_storage")
        self.partitions = {
            ("caller_a", "custom_storage"): {202401: "unrelated caller data"},
            ("storage_b", "custom_storage"): {202401: "selected target data"},
            (None, "events"): {202401: "selected local data"},
            ("caller_a", "events"): {202401: "unrelated routed data"},
        }

    def query(self, statement, settings=None, **_kwargs):
        self.queries.append(statement)
        metadata = self.metadata.query(statement, settings=settings)
        if metadata is not None:
            return metadata
        if "SELECT DISTINCT" in statement:
            rows = [(202401,)]
        elif "HAVING" in statement:
            rows = []
        else:
            rows = []
        return SimpleNamespace(result_rows=rows)

    def command(self, statement, **_kwargs):
        self.commands.append(statement)
        if not statement.startswith("ALTER TABLE"):
            return
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Alter):
            cluster = tree.args.get("cluster")
            scope = cluster.this.name if cluster is not None else None
            identity = (scope, tree.this.name)
            self.mutations.append(identity)
            self.partitions[identity].pop(202401, None)

    def insert_df(self, *_args, **_kwargs):
        pass

    def insert(self, *_args, **_kwargs):
        pass

    def close(self):
        pass


def assert_verified_mutation_only(client):
    expected = (None, "events") if client.local else ("storage_b", "custom_storage")
    assert client.mutations == [expected]
    assert client.partitions[("caller_a", "custom_storage")] == {202401: "unrelated caller data"}
    assert client.partitions[expected] == {}
    assert client.partitions[("caller_a", "events")] == {202401: "unrelated routed data"}


@pytest.mark.parametrize("local", [False, True])
def test_load_df_upsert_mutates_only_verified_existing_storage_scope(monkeypatch, local):
    client = ExistingStorageClient(local=local)
    stages = iter(["db.incoming", "db.payload"])
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: client)
    stage_policies = []
    target_policies = []

    def create_stage(**kwargs):
        stage_policies.append(kwargs["ch_creation_policy"])
        return next(stages)

    def ensure_target(options, *_args):
        target_policies.append(options.regular_ch_policy)

    monkeypatch.setattr(load_df_module, "create_stage_table", create_stage)
    monkeypatch.setattr(load_df_module, "_create_load_target_table", ensure_target)
    monkeypatch.setattr(load_df_module, "cleanup_stage_table_with_retry", lambda *_a, **_k: None)
    assert (
        sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [1], "month": [202401], "value": [10]}),
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="month",
            retry_cnt=1,
        )
        == 1
    )
    assert_verified_mutation_only(client)
    assert stage_policies
    assert all(policy.shard_on_cluster == "caller_a" for policy in stage_policies)
    assert target_policies[0].distributed_on_cluster == "facade_c"
    assert target_policies[0].distributed_cluster == "caller_a"


@pytest.mark.parametrize("local", [False, True])
def test_transfer_finalization_upsert_mutates_only_verified_existing_storage_scope(local):
    client = ExistingStorageClient(local=local)
    options = TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="SELECT id, month, value FROM source_table",
        target_table="db.events",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="month",
        ch_cluster="caller_a",
    )
    state = TransferStageState(
        target_exists=True,
        target_existed_at_start=True,
        stage_table="db.incoming",
        final_upsert_stage_table="db.payload",
        source_columns=["id", "month", "value"],
        insert_column_types=dict.fromkeys(("id", "month", "value"), "Int64"),
    )
    _finalize_target_once(
        options,
        state,
        state.stage_table,
        state.insert_column_types,
        target_precleared=False,
        fresh_clickhouse_target=False,
        target_connection_runner=lambda _role, operation: operation({"connection": client}),
    )
    assert_verified_mutation_only(client)


@pytest.mark.parametrize("local", [False, True])
def test_existing_layout_rewrites_prebuilt_partition_scope_without_changing_other_sql(local):
    client = ExistingStorageClient(local=local)
    source = ["SELECT 1", "ALTER TABLE db.events_shard ON CLUSTER caller_a DROP PARTITION 202401"]
    statements = get_backend_adapter("ch").resolve_upsert_execution_sqls(
        client, "db.events", source
    )
    assert statements[0] == "SELECT 1"
    client.command(statements[1])
    assert_verified_mutation_only(client)


def test_upsert_dry_plan_keeps_desired_scope_without_querying_existing_storage(monkeypatch):
    monkeypatch.setattr(
        load_df_module,
        "get_sql_connection",
        lambda *_a: pytest.fail("dry plan opened a connection"),
    )
    plan = sql.load_df(
        "ch",
        "db.events",
        pd.DataFrame({"id": [1], "month": [202401], "value": [10]}),
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column="month",
        dry_run=True,
    )
    partitions = [
        statement.sql for statement in plan.statements if "DROP PARTITION" in statement.sql
    ]
    assert plan.options["pending_existing_layout_verification"] is True
    assert partitions
    assert all("caller_a" in statement for statement in partitions)
    assert all("storage_b" not in statement for statement in partitions)


def test_transfer_upsert_dry_plan_defers_existing_storage_verification(monkeypatch):
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.api.get_sql_connection",
        lambda *_a: pytest.fail("dry plan opened a connection"),
    )
    plan = sql.transfer(
        from_db="gp",
        to_db="ch",
        from_sql="SELECT id, month, value FROM source_table",
        to_table="db.events",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="month",
        table_schema=dict.fromkeys(("id", "month", "value"), "Int64"),
        dry_run=True,
    )
    assert plan.options["pending_existing_layout_verification"] is True
    partitions = [
        statement.sql for statement in plan.statements if "DROP PARTITION" in statement.sql
    ]
    assert partitions
    assert all("caller_a" in statement for statement in partitions)
    assert all("storage_b" not in statement for statement in partitions)


@pytest.mark.parametrize("local", [False, True])
def test_routed_upsert_preserves_target_rows_and_unrelated_scope(local):
    raw = StatefulUpsertClient(local=local)
    client = ClusterRoutingClient(raw, routing=ChClusterRouting("caller_a"), database="db")
    options = TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="SELECT id, month, value FROM source_table",
        target_table="db.events",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="month",
        ch_cluster="caller_a",
    )
    state = TransferStageState(
        target_exists=True,
        stage_table="db.incoming",
        final_upsert_stage_table="db.payload",
        source_columns=["id", "month", "value"],
        insert_column_types=dict.fromkeys(("id", "month", "value"), "Int64"),
    )
    _finalize_target_once(
        options,
        state,
        state.stage_table,
        state.insert_column_types,
        target_precleared=False,
        fresh_clickhouse_target=False,
        target_connection_runner=lambda _role, operation: operation({"connection": client}),
    )
    assert sorted(raw.rows[raw.target]) == sorted(EXPECTED_ROWS)
    assert raw.rows[raw.unrelated] == UNRELATED_ROWS
    assert sorted(raw.rows[("caller_a", "payload")]) == sorted(EXPECTED_ROWS[:-1])


@pytest.mark.parametrize("routed", [False, True])
def test_outage_checkpoint_replay_respects_verified_local_or_selected_cluster_scope(
    monkeypatch,
    routed,
):
    cluster = Cluster()
    cluster.members_by_cluster["caller_a"] = cluster.members
    for host in cluster.hosts:
        cluster.create(
            host, "CREATE TABLE db.events (id UInt64) ENGINE=MergeTree PARTITION BY id ORDER BY id"
        )
    cluster.tables[("h2", "db.events")].rows = [(99,)]
    config = replace(
        get_connection_config("ch"),
        host="h1",
        database="db",
        cluster_routing=ChClusterRouting("caller_a") if routed else None,
    )
    adapter = get_backend_adapter("ch")
    monkeypatch.setattr(adapter, "open_connection", cluster.open)
    client = OutageClient(Client(cluster, "h1"), config)
    options = TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="SELECT id FROM source_table",
        target_table="db.events",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        ch_cluster="caller_a",
    )
    with backend_operation():
        replay = adapter.resolve_upsert_execution_sqls(
            client,
            "db.events",
            ["ALTER TABLE db.events ON CLUSTER caller_a DROP PARTITION 1"],
        )
        tree = parse_one(replay[0], read=ClickHouseDialect)
        assert (tree.args["cluster"].this.name if tree.args.get("cluster") else None) == (
            "caller_a" if routed else None
        )
        state = TransferStageState(
            target_exists=True,
            stage_table="db.incoming",
            final_upsert_stage_table="db.payload",
            source_columns=["id"],
            upsert_checkpoint=UpsertCheckpoint(
                final_stage_table="db.payload",
                target_table="db.events",
                backend="ch",
                replay_sqls=replay,
                prepared=True,
            ),
        )
        cluster.commands.clear()
        cluster.queries.clear()
        _finalize_target_once(
            options,
            state,
            state.stage_table,
            None,
            target_precleared=False,
            fresh_clickhouse_target=False,
            target_connection_runner=lambda _role, operation: operation({"connection": client}),
        )
    assert len(cluster.commands) == 1
    host, statement, _settings = cluster.commands[0]
    assert host == "h1"
    tree = parse_one(statement, read=ClickHouseDialect)
    assert (tree.args["cluster"].this.name if tree.args.get("cluster") else None) == (
        "caller_a" if routed else None
    )
    if not routed:
        assert not any("system.clusters" in query.lower() for query in cluster.queries)
    assert cluster.tables[("h2", "db.events")].rows == [(99,)]


@pytest.mark.parametrize("local", [False, True])
def test_upsert_stage_default_checkpoint_preserves_payload_before_verified_partition_mutation(
    local,
):
    raw = ExistingStorageClient(local=local)
    client = (
        ClusterRoutingClient(raw, routing=ChClusterRouting("caller_a"), database="db")
        if local
        else raw
    )
    result = upsert_stage_table(
        "ch",
        client,
        "db.events",
        "db.incoming",
        columns=["id", "month", "value"],
        key_columns=["id"],
        upsert_partition_column="month",
        final_stage_table="db.payload",
        ch_cluster="caller_a",
    )
    assert result is None
    assert_verified_mutation_only(raw)
    assert len(raw.commands) == 4
    preservation, incoming, mutation, publication = raw.commands
    assert preservation.startswith("INSERT INTO")
    assert "payload" in preservation
    assert "LEFT ANTI JOIN" in preservation
    assert "target_dst" in preservation
    assert "events" in preservation
    assert incoming.startswith("INSERT INTO")
    assert "payload" in incoming
    assert "incoming" in incoming
    assert "LEFT ANTI JOIN" not in incoming
    tree = parse_one(mutation, read=ClickHouseDialect)
    assert isinstance(tree, exp.Alter)
    assert tree.this.name == ("events" if local else "custom_storage")
    assert (tree.args["cluster"].this.name if tree.args.get("cluster") else None) == (
        None if local else "storage_b"
    )
    assert publication.startswith("INSERT INTO")
    assert "events" in publication
    assert "payload" in publication
    if local:
        target, source_query = StatefulUpsertClient._insert_parts(publication)
        assert target.db == "db"
        assert target.name == "events"
        assert isinstance(target.this, exp.Identifier)
        stage_source = source_query.args["from_"].this.this
        assert isinstance(stage_source, exp.Anonymous)
        assert stage_source.expressions[0].this == "caller_a"
        assert stage_source.expressions[2].this == "payload"


class StringPartitionClient(ExistingStorageClient):
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.partition_literals = []

    def query(self, statement, settings=None, **kwargs):
        result = super().query(statement, settings=settings, **kwargs)
        if "SELECT DISTINCT" in statement:
            result.result_rows = [(self.value,)]
        elif statement.startswith("DESCRIBE TABLE"):
            result.result_rows = [
                ("id", "Int64", ""),
                ("month", "String", ""),
                ("value", "Int64", ""),
            ]
        else:
            result.result_rows = [
                tuple(
                    value.replace("month Int64", "month String")
                    if isinstance(value, str)
                    else value
                    for value in row
                )
                for row in result.result_rows
            ]
        return result

    def command(self, statement, **kwargs):
        if statement.startswith("ALTER TABLE"):
            tree = parse_one(statement, read=ClickHouseDialect)
            literal = tree.find(exp.DropPartition).expressions[0].expressions[0]
            assert isinstance(literal, exp.Literal)
            assert literal.is_string
            self.partition_literals.append(literal.this)
        return super().command(statement, **kwargs)


@pytest.mark.parametrize(
    "value",
    ["<part>", "<affected partition value>", r"prefix\n\path'quoted"],
)
def test_load_df_upsert_quotes_real_string_partitions_that_resemble_plan_markers(
    monkeypatch, value
):
    client = StringPartitionClient(value)
    stages = iter(["db.incoming", "db.payload"])
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: client)
    monkeypatch.setattr(load_df_module, "create_stage_table", lambda **_kwargs: next(stages))
    monkeypatch.setattr(load_df_module, "_create_load_target_table", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "cleanup_stage_table_with_retry", lambda *_a, **_k: None)
    assert (
        sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [1], "month": [value], "value": [10]}),
            write_mode="upsert",
            key_columns="id",
            upsert_partition_column="month",
            retry_cnt=1,
        )
        == 1
    )
    assert client.partition_literals == [value]
    assert_verified_mutation_only(client)


def test_unknown_partition_values_keep_only_the_synthetic_dry_plan_marker():
    statements = get_backend_adapter("ch").build_drop_upsert_partition_sqls(
        "db.events",
        partition_column="month",
        partition_values=None,
        ch_cluster="caller_a",
    )
    assert len(statements) == 1
    assert statements[0].endswith("DROP PARTITION <affected partition value>")
    assert "'<affected partition value>'" not in statements[0]


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("table_name", ["db.events", "events"])
def test_load_df_upsert_keeps_all_target_operations_on_verified_storage(
    monkeypatch, local, table_name
):
    raw = StatefulUpsertClient(local=local)
    client = ClusterRoutingClient(raw, routing=ChClusterRouting("caller_a"), database="db")
    stages = iter(["db.incoming", "db.payload"])
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: client)
    monkeypatch.setattr(load_df_module, "create_stage_table", lambda **_kwargs: next(stages))
    monkeypatch.setattr(load_df_module, "_create_load_target_table", lambda *_a, **_k: None)
    monkeypatch.setattr(load_df_module, "cleanup_stage_table_with_retry", lambda *_a, **_k: None)

    def insert_stage(_backend, _connection, table, batch, **_kwargs):
        raw.rows[("caller_a", table.split(".")[-1])] = list(
            batch.itertuples(index=False, name=None)
        )
        return len(batch)

    monkeypatch.setattr(load_df_module, "insert_table_batch", insert_stage)
    assert sql.load_df(
        "ch",
        table_name,
        pd.DataFrame(INCOMING_ROWS, columns=["id", "month", "value"]),
        write_mode="upsert",
        key_columns="id",
        upsert_partition_column="month",
        retry_cnt=1,
    ) == len(INCOMING_ROWS)
    assert sorted(raw.rows[raw.target]) == sorted(EXPECTED_ROWS)
    assert raw.rows[raw.unrelated] == UNRELATED_ROWS
    assert sorted(raw.rows[("caller_a", "payload")]) == sorted(EXPECTED_ROWS[:-1])
