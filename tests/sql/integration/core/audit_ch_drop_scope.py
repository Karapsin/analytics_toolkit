"""Drop existing ClickHouse resources only in their verified execution scopes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.reconfiguration import (
    create_owned,
    create_storage,
    inventory,
    label,
    literal,
    quoted,
    resource_name,
    seed_storage,
    stored_rows,
)
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_CORE = "integration_cluster"
_ARCHIVE = "integration_archive_cluster"
_FULL = "integration_two_shards"
_DATABASE = "integration"


@dataclass(frozen=True)
class _DropPair:
    relation: str
    shard: str
    physical_rows: list[tuple[int, str]]
    foreign_rows: list[tuple[int, str]]
    remote_foreign_rows: list[tuple[int, str]]
    before: dict[str, list[tuple[str, ...]]]


def _owned_drop_pair(registry: ResourceRegistry) -> _DropPair:
    relation = resource_name("drop_existing_scope")
    shard = relation + "_shard"
    create_storage(registry, _DATABASE, shard, _ARCHIVE)
    create_storage(registry, _DATABASE, shard, _CORE)
    create_storage(registry, _DATABASE, relation, _ARCHIVE)
    table = quoted(_DATABASE, relation)
    create_owned(
        registry,
        f"CREATE TABLE {table} (id UInt64, payload String) "
        f"ENGINE=Distributed({literal(_ARCHIVE)}, {literal(_DATABASE)}, {literal(shard)}, rand())",
        f"DROP TABLE IF EXISTS {table} SYNC",
    )
    physical_rows = seed_storage(_ARCHIVE, _DATABASE, shard, rows=3)
    foreign_rows = seed_storage(_CORE, _DATABASE, shard, rows=100, offset=1000)
    remote_foreign_rows = seed_storage(_ARCHIVE, _DATABASE, relation, rows=100, offset=2000)
    before = {cluster: inventory(cluster, [_DATABASE], relation) for cluster in (_CORE, _ARCHIVE)}
    assert stored_rows(_CORE, _DATABASE, relation) == physical_rows
    return _DropPair(relation, shard, physical_rows, foreign_rows, remote_foreign_rows, before)


@pytest.mark.parametrize(
    "role",
    [
        scenario_param("audit.drop.clickhouse.managed_pair_scopes", "both"),
        scenario_param("audit.drop.clickhouse.physical_role_only", "physical"),
        scenario_param("audit.drop.clickhouse.facade_role_only", "facade"),
    ],
)
def test_drop_tables_keeps_owned_roles_in_their_verified_execution_scopes(
    resource_registry: ResourceRegistry, role: str
) -> None:
    source = _owned_drop_pair(resource_registry)
    drop_shard, drop_facade = role != "facade", role != "physical"

    result = sql.drop_tables(
        "ch",
        quoted(_DATABASE, source.relation),
        ch_cluster=_ARCHIVE,
        ch_drop_shard=drop_shard,
        ch_drop_distributed=drop_facade,
        if_exists=True,
        ch_wait_for_absence=True,
        ch_wait_timeout_seconds=15,
        ch_wait_poll_interval_seconds=0.1,
        ch_retry_per_host_drops=True,
        return_metadata=True,
        query_label=label("drop-verified-existing-scopes"),
    )

    assert isinstance(result, sql.SqlOperationResult)
    assert result.rows is None
    assert result.data is None
    assert result.plan is not None
    assert result.plan.options["ch_drop_shard"] == drop_shard
    assert result.plan.options["ch_drop_distributed"] == drop_facade
    removed = {
        _CORE: {source.relation} if drop_facade else set(),
        _ARCHIVE: {source.shard} if drop_shard else set(),
    }
    expected = {
        cluster: [row for row in rows if row[1] not in removed[cluster]]
        for cluster, rows in source.before.items()
    }
    assert {
        cluster: inventory(cluster, [_DATABASE], source.relation) for cluster in (_CORE, _ARCHIVE)
    } == expected
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.foreign_rows
    assert stored_rows(_ARCHIVE, _DATABASE, source.relation) == source.remote_foreign_rows
    if not drop_shard:
        assert stored_rows(_ARCHIVE, _DATABASE, source.shard) == source.physical_rows


@pytest.mark.sql_scenario("audit.create.clickhouse.execute_query_drop_scope")
def test_execute_create_replaces_only_verified_existing_pair_resources(
    resource_registry: ResourceRegistry,
) -> None:
    source = _owned_drop_pair(resource_registry)
    foreign_before = {
        _CORE: [row for row in source.before[_CORE] if row[1] == source.shard],
        _ARCHIVE: [row for row in source.before[_ARCHIVE] if row[1] == source.relation],
    }
    result = sql.execute_create(
        "ch",
        quoted(_DATABASE, source.relation),
        "SELECT toUInt64(201) AS id, 'replacement' AS payload",
        drop_if_exists=True,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_sharding_key="rand()",
        ch_shard_on_cluster=_ARCHIVE,
        ch_distributed_cluster=_ARCHIVE,
        ch_distributed_on_cluster=None,
        order_by="id",
        retry_cnt=1,
        query_label=label("execute-create-verified-existing-scopes"),
    )

    assert isinstance(result, int)
    assert result in (0, 1)  # Historical zero is allowed when the driver supplies no count.
    assert stored_rows(_CORE, _DATABASE, source.relation) == [(201, "replacement")]
    assert stored_rows(_ARCHIVE, _DATABASE, source.shard) == [(201, "replacement")]
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.foreign_rows
    assert stored_rows(_ARCHIVE, _DATABASE, source.relation) == source.remote_foreign_rows
    assert {
        _CORE: [
            row for row in inventory(_CORE, [_DATABASE], source.relation) if row[1] == source.shard
        ],
        _ARCHIVE: [
            row
            for row in inventory(_ARCHIVE, [_DATABASE], source.relation)
            if row[1] == source.relation
        ],
    } == foreign_before


@pytest.mark.parametrize(
    "physical_first",
    [
        scenario_param("audit.drop.clickhouse.overlapping_facade_first", False),
        scenario_param("audit.drop.clickhouse.overlapping_physical_first", True),
    ],
)
def test_drop_tables_executes_overlapping_requested_scopes_once(
    resource_registry: ResourceRegistry,
    integration_connections,
    write_sql_connections,
    physical_first: bool,
) -> None:
    relation = resource_name("drop_scope_overlap")
    shard, sentinel = relation + "_shard", relation + "_keep"
    for scope in (_CORE, _ARCHIVE):
        create_storage(resource_registry, _DATABASE, shard, scope)
        create_storage(resource_registry, _DATABASE, sentinel, scope)
    table, full_scope = quoted(_DATABASE, relation), quoted(_FULL)
    create_owned(
        resource_registry,
        f"CREATE TABLE {table} ON CLUSTER {full_scope} (id UInt64, payload String) "
        f"ENGINE=Distributed({literal(_ARCHIVE)}, {literal(_DATABASE)}, {literal(shard)}, rand())",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {full_scope} SYNC",
    )
    seed_storage(_CORE, _DATABASE, shard, rows=100)
    seed_storage(_ARCHIVE, _DATABASE, shard, rows=3)
    sentinel_rows = {
        scope: seed_storage(scope, _DATABASE, sentinel, rows=5, offset=offset)
        for scope, offset in ((_CORE, 1000), (_ARCHIVE, 2000))
    }
    sentinel_before = {
        scope: [row for row in inventory(scope, [_DATABASE], relation) if row[1] == sentinel]
        for scope in (_CORE, _ARCHIVE)
    }
    # Only the disposable stack's generated connections are copied here.
    connections = deepcopy(integration_connections)
    alias = resource_name("drop_overlap_alias")
    connections[alias] = deepcopy(connections["ch"])
    connections[alias]["cluster_routing"] = {"cluster": _FULL}
    general_module.set_connections_path(write_sql_connections(connections))
    targets = [table, quoted(_DATABASE, shard)]
    if physical_first:
        targets.reverse()

    assert (
        sql.drop_tables(
            alias,
            targets,
            ch_cluster=_ARCHIVE,
            if_exists=True,
            ch_wait_timeout_seconds=15,
            ch_wait_poll_interval_seconds=0.1,
            query_label=label("drop-overlapping-verified-scopes"),
        )
        is None
    )
    assert {
        scope: inventory(scope, [_DATABASE], relation) for scope in (_CORE, _ARCHIVE)
    } == sentinel_before
    assert {
        scope: stored_rows(scope, _DATABASE, sentinel) for scope in (_CORE, _ARCHIVE)
    } == sentinel_rows


@pytest.mark.sql_scenario("audit.create.clickhouse.remote_plain_skip")
def test_execute_create_skips_existing_remote_plain_target_before_setup(
    resource_registry: ResourceRegistry,
) -> None:
    relation = resource_name("remote_plain_skip")
    create_storage(resource_registry, _DATABASE, relation, _ARCHIVE)
    rows = seed_storage(_ARCHIVE, _DATABASE, relation, rows=100)
    table = quoted(_DATABASE, relation)
    before = {scope: inventory(scope, [_DATABASE], relation) for scope in (_CORE, _ARCHIVE)}
    assert before[_CORE] == []
    # The operation may create a local fallback if skip regresses. The name is
    # generated for this disposable run and was verified absent above.
    resource_registry.table("ch", table, ch_only_shard=True)
    result = sql.execute_create(
        "ch",
        table,
        "CREATE TEMPORARY TABLE skipped_setup (id UInt64) ENGINE=Memory; "
        "SELECT toUInt64(201) AS id, 'new' AS payload",
        if_not_exists=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster=_ARCHIVE,
        retry_cnt=1,
        return_metadata=True,
        query_label=label("remote-plain-full-skip"),
    )

    assert isinstance(result, sql.SqlOperationResult)
    assert result.rows == 0
    assert result.data == 0
    assert result.metadata.statement_count == 0
    assert {scope: inventory(scope, [_DATABASE], relation) for scope in (_CORE, _ARCHIVE)} == before
    assert stored_rows(_ARCHIVE, _DATABASE, relation) == rows
