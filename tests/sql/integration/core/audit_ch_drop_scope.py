"""Drop existing ClickHouse resources only in their verified execution scopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
