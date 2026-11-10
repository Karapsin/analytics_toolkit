from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.layout import ExistingClickHouseLayout
from analytics_toolkit.sql.backends.ch.query_source_scope import query_overlaps_layout
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.query_source_binding import query_may_read_target
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from analytics_toolkit.sql.ddl import api
from analytics_toolkit.sql.dml import query_input
from analytics_toolkit.sql.dml.transfer import schema as transfer_schema
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql.backends.ch.policy_ctas import options as policy_options
from tests.sql.backends.ch.policy_ctas import policy as policy_fixture
from tests.sql.dml.table.create_from_sql.adaptive import install, legacy, policy, run


@pytest.mark.parametrize(
    "columns", [[], [SourceColumn("id", "Int64"), SourceColumn("id", "Int64")]]
)
def test_query_creation_rejects_unusable_positional_schema_before_ddl(monkeypatch, columns):
    client = install(monkeypatch)
    monkeypatch.setattr(query_input, "inspect_source_query_schema", lambda *_a: columns)
    with pytest.raises(InvalidSqlInputError, match="uniquely named"):
        run()
    assert not client.commands


def test_query_recovery_manifest_failure_preserves_primary_error_checkpoint_and_guard_cleanup(
    monkeypatch,
):
    client = install(monkeypatch, rows=((1,),))
    original = ReplaySpool.retain
    retained = []
    primary = ConnectionError("uncertain structural submission")

    def retain(spool, metadata):
        retained.append((spool, Path(original(spool, metadata))))
        message = "manifest acknowledgement lost"
        raise OSError(message)

    def command(*_a, **_k):
        operation.preserve_recovery = True
        raise primary

    monkeypatch.setattr(ReplaySpool, "retain", retain)
    monkeypatch.setattr(client, "command", command)
    try:
        with backend_operation() as operation:
            with pytest.raises(ConnectionError, match="uncertain structural") as caught:
                run(schema={"id": "Int8"})
            assert caught.value is primary
            assert not operation.resources.get("connection_configuration_guards")
        assert len(retained) == 1
        assert retained[0][1].exists()
        assert json.loads(retained[0][1].read_text())["row_count"] == 1
    finally:
        for spool, manifest in retained:
            spool.close()
            shutil.rmtree(manifest.parent)
    query_input.close_query_input({})


def test_query_client_open_failure_releases_its_guard_without_closing_unacquired_client(
    monkeypatch,
):
    client = install(monkeypatch)

    def fail(_key):
        message = "target client unavailable"
        raise ConnectionError(message)

    monkeypatch.setattr(legacy, "get_sql_connection", fail)
    with backend_operation() as operation:
        with pytest.raises(ConnectionError, match="target client unavailable"):
            policy.create_table_as_with_policy(policy=policy_fixture(), **policy_options())
        assert not operation.resources.get("connection_configuration_guards")
    assert client.close_calls == 0


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_replacement_source_binding_keeps_cte_shadowing_and_freezes_unknown_table_functions(
    backend,
):
    assert not query_may_read_target(
        "WITH events AS (SELECT 1 AS id) SELECT id FROM events", "events", backend
    )
    assert query_may_read_target("SELECT * FROM generate_series(1, 2)", "events", backend)
    with pytest.raises(InvalidSqlInputError, match="Cannot verify replacement"):
        query_may_read_target("SELECT (", "events", backend)


@pytest.mark.parametrize(
    ("query", "overlap"),
    [
        ("WITH events AS (SELECT 1 AS id) SELECT id FROM events", False),
        ("SELECT * FROM cluster('storage', 'analytics.events_shard')", True),
        ("SELECT * FROM clusterAllReplicas('storage', 'analytics', 'events_shard')", True),
        ("SELECT * FROM numbers(2)", False),
    ],
)
def test_ch_replacement_binding_uses_scope_static_cluster_components_and_generators(query, overlap):
    layout = ExistingClickHouseLayout(
        "analytics.events", "analytics.events_shard", True, None, None, None, "", ""
    )
    connection = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=[("analytics",)]))
    assert query_overlaps_layout(query, layout, connection, None) is overlap


@pytest.mark.parametrize(
    "query",
    [
        "SELECT (",
        "SELECT * FROM remote('host', 'db', 'events')",
        "SELECT * FROM s3('url')",
        "SELECT * FROM read_parquet('path')",
    ],
)
def test_unverifiable_ch_replacement_binding_rejects_before_mutation(query):
    layout = SimpleNamespace(
        logical_table="analytics.events", physical_table="analytics.events_shard"
    )
    with pytest.raises(InvalidSqlInputError, match=r"(Cannot verify|verifiable static)"):
        query_overlaps_layout(query, layout, None, "analytics")


def test_sql_only_generation_rejects_uncertified_wide_query_without_scanning(monkeypatch):
    closed = []
    monkeypatch.setattr(
        api, "get_sql_connection", lambda _key: SimpleNamespace(close=lambda: closed.append(True))
    )
    monkeypatch.setattr(
        transfer_schema, "inspect_source_query_schema", lambda *_a: [SourceColumn("id", "Int256")]
    )
    monkeypatch.setattr(
        ReplaySpool, "__init__", lambda *_a, **_k: pytest.fail("SQL generation spooled data")
    )
    with pytest.raises(ValueError, match="adaptive full-input profiling"):
        sql.create_table(
            "trino", "sandbox.events", source_db="ch", sql="SELECT id", only_generate_sql=True
        )
    assert closed == [True]
