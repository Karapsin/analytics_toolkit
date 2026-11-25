from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.cutover import (
    StructuralCutoverError,
    create_replacements,
    execute_cutover,
    rollback_cutover,
)
from analytics_toolkit.sql.backends.ch.safe_replace import _rollback_completed_steps
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError, InvalidSqlInputError
from analytics_toolkit.sql.execution.backend_options import backend_operation
from analytics_toolkit.sql.execution.plans import SqlPlan

from tests.sql._support.reconfigure import TABLE_DDL, ReconfigureClient

module = importlib.import_module("analytics_toolkit.sql.backends.ch.cutover")
topology = importlib.import_module("analytics_toolkit.sql.backends.ch.reconfigure_topology")


def change():
    return SimpleNamespace(
        plan=SqlPlan(operation="test"),
        cutover_sqls=["RENAME TABLE db.a TO db.b"],
        rollback_sqls=[],
        cutover_journal=[],
        temporary_table_scopes=[],
        owned_table_scopes=[],
        backup_tables=["db.backup"],
        cleanup_error=None,
    )


def test_incomplete_inverse_journal_never_submits_structural_ddl():
    statements = []
    adapter = SimpleNamespace(execute_command=lambda _c, sql: statements.append(sql))
    with pytest.raises(StructuralCutoverError, match="complete inverse journal"):
        execute_cutover(adapter, object(), change())
    assert statements == []


def test_rollback_skips_unacknowledged_and_already_restored_steps():
    current = change()
    current.cutover_journal = [
        {"acknowledged": False, "inverse": "must not execute"},
        {"acknowledged": True, "rollback_acknowledged": True, "inverse": "already restored"},
        {"acknowledged": True, "inverse": "RENAME TABLE db.b TO db.a"},
    ]
    statements = []
    adapter = SimpleNamespace(execute_command=lambda _c, sql: statements.append(sql))
    rollback_cutover(adapter, object(), current)
    rollback_cutover(adapter, object(), current)
    assert statements == ["RENAME TABLE db.b TO db.a"]
    assert current.cutover_journal[-1]["rollback_acknowledged"] is True


def test_failed_rollback_blocks_replay_and_retains_original_recovery_names():
    current = change()
    current.cutover_journal = [{"acknowledged": True, "inverse": "RENAME TABLE db.b TO db.a"}]
    failure = OSError("rollback acknowledgement lost")

    def execute(*_args):
        raise failure

    with backend_operation() as state:
        with pytest.raises(StructuralCutoverError, match=r"db\.backup") as caught:
            rollback_cutover(SimpleNamespace(execute_command=execute), object(), current)
        assert state.preserve_recovery
        assert state.structural_replay_blocked
    assert caught.value.__cause__ is failure
    assert current.cutover_journal[0]["rollback_submitted"] is True
    assert "rollback_acknowledged" not in current.cutover_journal[0]


def test_replacement_acknowledgement_only_owns_matching_plain_create_candidate():
    current = change()
    current.plan.add(
        "CREATE TABLE IF NOT EXISTS db.preexisting (id Int64)", phase="create_replacement"
    )
    current.plan.add("CREATE TABLE db.unknown (id Int64)", phase="create_replacement")
    current.plan.add("CREATE TABLE db.owned (id Int64)", phase="create_replacement")
    current.temporary_table_scopes = [("db.unmatched", None), ("db.owned", None)]
    statements = []
    create_replacements(
        SimpleNamespace(execute_command=lambda _c, sql: statements.append(sql)), object(), current
    )
    assert len(statements) == 3
    assert current.owned_table_scopes == [("db.owned", None)]


@pytest.mark.parametrize(("resolved", "hosts"), [(None, []), ("core", [])])
def test_unresolved_or_empty_destination_endpoints_fail_before_copy(monkeypatch, resolved, hosts):
    monkeypatch.setattr(topology, "resolve_optional_cluster", lambda *_a: resolved)
    monkeypatch.setattr(topology, "cluster_hosts", lambda *_a: hosts)
    with pytest.raises(InvalidSqlInputError, match=r"(resolve|verify)"):
        topology.endpoints(object(), "core")


@pytest.mark.parametrize(("source_pair", "cross"), [(False, False), (True, True)])
def test_copy_rejects_incomplete_storage_and_routing_scope(source_pair, cross):
    request = topology.DestinationTopology(source_pair, True, "old", None, None, "db.shard", cross)
    with pytest.raises(InvalidSqlInputError, match=r"(requires verified|Cannot verify)"):
        topology.validate_destination(object(), request)


def test_cluster_alias_cannot_route_replacement_back_to_original_storage():
    client = ReconfigureClient(target_hosts=(("source-alias", "10.0.0.1", 9000),))
    request = topology.DestinationTopology(
        source_pair=True,
        target_pair=True,
        source_cluster="core",
        target_cluster="archive",
        distributed_cluster="{copy_route}",
        desired_shard_table="analytics.events_shard",
        cross_cluster=True,
        logical_table="analytics.events",
        facade_cluster="core",
        source_facade_ddl=TABLE_DDL,
    )
    with pytest.raises(InvalidSqlInputError, match="original source storage"):
        topology.validate_destination(client, request)
    assert client.commands == []


def test_guarded_rollback_preserves_ambiguous_primary_error_after_acknowledged_inverse():
    failure = AmbiguousSqlReplaceError("original uncertain state")
    executed = []
    adapter = SimpleNamespace(execute_command=lambda _c, sql: executed.append(sql))
    with pytest.raises(AmbiguousSqlReplaceError) as caught:
        _rollback_completed_steps(adapter, object(), ["RENAME TABLE db.b TO db.a"], failure)
    assert caught.value is failure
    assert executed == ["RENAME TABLE db.b TO db.a"]
