"""Source snapshot visibility does not establish CREATE ownership."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.stage_ownership import (
    drop_owned_resource,
    single_resource_policy,
)
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import LazyKeyedRuntime
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferStageState

from tests.sql._support.fakes import FakeDbapiConnection, FakeDbapiCursor
from tests.sql._support.transfer_ordinal import (
    _staged_options,
    resolve_internal_columns,
    staged_attempt,
)


@pytest.mark.parametrize("failure_phase", ["create", "post_create"])
def test_snapshot_failure_drops_only_acknowledged_create(monkeypatch, failure_phase):
    class Cursor(FakeDbapiCursor):
        def execute(self, statement, params=None):
            super().execute(statement, params)
            failed = (
                statement.startswith("CREATE TABLE")
                if failure_phase == "create"
                else statement.startswith("CREATE INDEX")
            )
            if failed:
                message = "connection lost during source snapshot"
                raise ConnectionError(message)

    class Connection(FakeDbapiConnection):
        def cursor(self):
            return Cursor(self)

    connection = Connection()
    options = _staged_options()
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    monkeypatch.setattr(
        staged_attempt, "_allocate_snapshot_name", lambda *_a: "source_stage.snapshot"
    )
    with pytest.raises(ConnectionError, match="source snapshot"):
        staged_attempt._materialize_snapshot(options, {"connection": connection}, state)
    drops = [statement for statement in connection.executed if statement.startswith("DROP TABLE")]
    assert bool(drops) is (failure_phase == "post_create")


def test_unowned_keyed_snapshot_is_not_cleanup_eligible_and_releases_capacity():
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    assert runtime.live_stage_credits.acquire(blocking=False)
    runtime.reserve_source_stage("source.unknown")
    assert runtime.source_stage_tables == []
    assert runtime.source_stage_entries == {}
    runtime.abandon_unowned_source_stage("source.unknown")
    assert runtime.live_source_stage_count == 0
    assert runtime.live_stage_credits.acquire(blocking=False)
    assert runtime.live_stage_credits.acquire(blocking=False)
    assert not runtime.live_stage_credits.acquire(blocking=False)


def test_source_snapshot_policy_never_owns_companion():
    policy = SimpleNamespace(
        create_distributed_pair=True, shard_on_cluster="storage", distributed_on_cluster="facades"
    )
    actual = single_resource_policy(policy)
    assert actual.create_distributed_pair is False
    assert actual.shard_on_cluster == "storage"
    assert policy.create_distributed_pair is True


def test_owned_stage_cleanup_uses_individual_acknowledged_resource_scope():
    policy = ClickHouseCreationPolicy(
        create_distributed_pair=True,
        shard_engine="MergeTree",
        shard_on_cluster="storage",
        distributed_engine_template="Distributed({cluster}, {database}, {shard_table})",
        distributed_cluster="routing",
        distributed_on_cluster="facades",
        sharding_key="rand()",
        ddl_ready_timeout_seconds=1,
    )
    source_policy = single_resource_policy(policy)
    assert not source_policy.create_distributed_pair
    assert policy.create_distributed_pair
    commands = []

    def command(sql, **_kwargs):
        commands.append(sql)

    client = SimpleNamespace(command=command)
    adapter = get_backend_adapter("ch")
    for resource in ["db.stage_shard", "db.stage"]:
        drop_owned_resource(
            adapter, client, resource, logical_stage="db.stage", creation_policy=policy
        )
    assert commands == [
        "DROP TABLE IF EXISTS `db`.`stage_shard` ON CLUSTER storage",
        "DROP TABLE IF EXISTS `db`.`stage` ON CLUSTER facades",
    ]


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_owned_stage_cleanup_preserves_unquoted_case_folding_and_quoted_identity(backend):
    connection = FakeDbapiConnection()
    adapter = get_backend_adapter(backend)
    drop_owned_resource(adapter, connection, "Upper.Stage", logical_stage="Upper.Stage")
    drop_owned_resource(adapter, connection, '"Upper"."Stage"', logical_stage='"Upper"."Stage"')
    assert connection.executed == [
        'DROP TABLE IF EXISTS "upper"."stage"',
        'DROP TABLE IF EXISTS "Upper"."Stage"',
    ]
