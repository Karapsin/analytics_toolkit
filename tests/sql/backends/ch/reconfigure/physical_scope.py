"""Physical source certification preserves metadata-only planning fast paths."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    InvalidSqlInputError,
)
from analytics_toolkit.sql.execution.observation import observe_sql
from analytics_toolkit.sql.execution.plans import SqlPlan
from sqlglot import exp, parse_one

from tests.sql._support.reconfigure_independent_replicas import ReconfigureSubmissionLog
from tests.sql._support.reconfigure_physical_scope import (
    add_migration_scopes,
    change_scope_before_execution,
    install_physical_scope,
    reconfigure_physical,
)


def _assert_metadata_only(raw, submissions, original):
    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.wrappers[0]._client._sql_observed is True
    assert submissions.sql
    assert raw.counts == []
    assert raw.native_reads == []
    assert raw.native_insert_fanout == []
    assert raw.commands == []
    assert raw.snapshot() == original
    assert raw.closed


def test_physical_rebuild_dry_run_collects_metadata_without_counts_or_copies(
    monkeypatch, write_sql_connections
):
    raw = install_physical_scope(monkeypatch, write_sql_connections)
    assert get_connection_config("ch").cluster_routing is None
    original = raw.snapshot()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions):
        plan = reconfigure_physical(dry_run=True)

    assert isinstance(plan, SqlPlan)
    assert plan.options["strategy"] == "managed_pair_rebuild"
    _assert_metadata_only(raw, submissions, original)


def test_physical_no_op_keeps_missing_replica_metadata_fast_path(
    monkeypatch, write_sql_connections
):
    raw = install_physical_scope(monkeypatch, write_sql_connections, complete=False)
    original = raw.snapshot()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions):
        plan = reconfigure_physical(order_by="id", dry_run=True)

    assert isinstance(plan, SqlPlan)
    assert plan.options["strategy"] == "no_op"
    assert raw.source_physical not in raw.host_tables[raw.remote]
    _assert_metadata_only(raw, submissions, original)


def test_physical_source_scope_change_after_planning_rejects_before_ddl_or_worker_open(
    monkeypatch, write_sql_connections
):
    raw = install_physical_scope(monkeypatch, write_sql_connections)
    original = raw.snapshot()
    mutations = change_scope_before_execution(monkeypatch, raw)
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions), pytest.raises(
        (InvalidSqlInputError, ClickHouseClusterTopologyError),
        match=r"(?i)(source|scope|membership|endpoint|changed)",
    ):
        reconfigure_physical()

    assert mutations == ["source_scope_changed"]
    assert raw.worker_connections == raw.worker_commands == []
    _assert_metadata_only(raw, submissions, original)


def test_physical_copy_without_row_validation_preserves_every_host_without_counts(
    monkeypatch, write_sql_connections
):
    raw = install_physical_scope(monkeypatch, write_sql_connections)
    original = raw.physical_counters()
    assert sum(sum(rows.values()) for rows in original.values()) == 103
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions):
        result = reconfigure_physical(validate_row_count=False)

    assert raw.physical_counters() == original
    assert raw.counts == []
    assert all(
        parse_one(statement, read=ClickHouseDialect).find(exp.Count) is None
        for worker in raw.worker_connections
        for statement in worker.queries
    )
    assert result.metadata.source_rows is None
    assert result.metadata.staged_rows is None
    assert result.metadata.final_target_rows is None
    assert result.metadata.row_count_validated is False
    assert result.data["cleanup_complete"] is True
    assert raw.native_reads == raw.native_insert_fanout == []
    assert raw.worker_connections
    assert all(worker.closed for worker in raw.worker_connections)
    assert raw.closed


@pytest.mark.parametrize(
    "options",
    [
        {
            "ch_shard_on_cluster": "archive",
            "ch_distributed_cluster": "archive",
            "ch_distributed_on_cluster": "coordinator_only",
        },
        {"ch_distributed_table": False, "ch_distributed_on_cluster": "coordinator_only"},
        {"ch_engine": "ReplicatedMergeTree('/clickhouse/{table}', '{replica}')"},
    ],
    ids=["relocation", "pair-to-local", "independent-to-replicated"],
)
def test_independent_physical_migration_requires_explicit_host_plan_before_ddl(
    monkeypatch, write_sql_connections, options
):
    raw = install_physical_scope(monkeypatch, write_sql_connections)
    add_migration_scopes(raw)
    original = raw.snapshot()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions), pytest.raises(
        InvalidSqlInputError, match="explicit per-host migration"
    ):
        reconfigure_physical(**options)

    assert raw.worker_connections == raw.worker_commands == []
    _assert_metadata_only(raw, submissions, original)


def test_independent_physical_nonreplicated_engine_change_preserves_each_host(
    monkeypatch, write_sql_connections
):
    raw = install_physical_scope(monkeypatch, write_sql_connections)
    original = raw.physical_counters()
    submissions = ReconfigureSubmissionLog()

    with observe_sql(submissions):
        result = reconfigure_physical(ch_engine="ReplacingMergeTree")

    assert raw.physical_counters() == original
    assert result.metadata.source_rows == result.metadata.staged_rows == 103
    assert result.metadata.final_target_rows == 103
    assert result.metadata.row_count_validated is True
    assert result.data["cleanup_complete"] is True
    assert all(
        raw.host_tables[host][raw.source_physical].engine == "ReplacingMergeTree"
        for host in raw.clusters["storage"]
    )
    assert raw.native_reads == raw.native_insert_fanout == []
    assert raw.worker_connections
    assert all(worker.closed for worker in raw.worker_connections)
    assert raw.closed
