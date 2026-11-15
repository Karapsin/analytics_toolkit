"""Real two-host pair-to-local ownership and destination scope checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql.integration._support.reconfiguration import (
    cleanup_after_acknowledgement,
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

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_CORE = "integration_cluster"
_ARCHIVE = "integration_archive_cluster"
_DATABASE = "integration"


@dataclass(frozen=True)
class _SourcePair:
    relation: str
    shard: str
    source_rows: list[tuple[int, str]]
    shadow_rows: list[tuple[int, str]]
    foreign_rows: list[tuple[int, str]]
    before: dict[str, list[tuple[str, ...]]]


def _source_pair(
    registry: ResourceRegistry, purpose: str, *, occupied_destination: bool
) -> _SourcePair:
    relation = resource_name(purpose)
    shard = relation + "_shard"
    create_storage(registry, _DATABASE, shard, _ARCHIVE)
    create_storage(registry, _DATABASE, shard, _CORE)
    table, facade_scope = quoted(_DATABASE, relation), quoted(_CORE)
    engine = f"Distributed({literal(_ARCHIVE)}, {literal(_DATABASE)}, {literal(shard)}, rand())"
    create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {facade_scope} "
        f"(id UInt64, payload String) ENGINE={engine}",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {facade_scope} SYNC",
    )
    source_rows = seed_storage(_ARCHIVE, _DATABASE, shard, rows=3)
    shadow_rows = seed_storage(_CORE, _DATABASE, shard, rows=100, offset=2000)
    foreign_rows = []
    if occupied_destination:
        create_storage(registry, _DATABASE, relation, _ARCHIVE)
        foreign_rows = seed_storage(_ARCHIVE, _DATABASE, relation, rows=100, offset=1000)
    before = {cluster: inventory(cluster, [_DATABASE], relation) for cluster in (_CORE, _ARCHIVE)}
    assert stored_rows(_CORE, _DATABASE, relation) == source_rows
    return _SourcePair(relation, shard, source_rows, shadow_rows, foreign_rows, before)


def _desired_alias(integration_connections, write_sql_connections, facade_scope: str) -> str:
    # These are only the disposable stack's generated loopback connections.
    # Keep its selected transport/authentication and add isolated DDL defaults.
    connections = deepcopy(integration_connections)
    alias = resource_name("pair_scope_alias")
    connection = deepcopy(connections["ch"])
    connection["ddl_defaults"]["regular"] = {
        "create_distributed_pair": False,
        "shard": {"engine": "MergeTree", "on_cluster": _ARCHIVE},
        "distributed": {
            "engine_template": "Distributed({cluster}, {database}, {shard_table}, {sharding_key})",
            "cluster": _ARCHIVE,
            "on_cluster": facade_scope,
            "sharding_key": "rand()",
        },
    }
    connections[alias] = connection
    general_module.set_connections_path(write_sql_connections(connections))
    return alias


def _convert_to_defaults(alias: str, relation: str):
    return sql.ch_reconfigure_table(
        alias,
        quoted(_DATABASE, relation),
        to_defaults=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
        query_label=label("pair-to-local-defaults-scope"),
    )


def _assert_source_unchanged(source: _SourcePair) -> None:
    assert {
        cluster: inventory(cluster, [_DATABASE], source.relation) for cluster in (_CORE, _ARCHIVE)
    } == source.before
    assert stored_rows(_CORE, _DATABASE, source.relation) == source.source_rows
    assert stored_rows(_ARCHIVE, _DATABASE, source.shard) == source.source_rows
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.shadow_rows
    assert stored_rows(_ARCHIVE, _DATABASE, source.relation) == source.foreign_rows


@pytest.mark.sql_scenario("audit.clickhouse.pair_to_local_wrong_facade_scope")
def test_pair_to_local_defaults_reject_a_scope_that_excludes_the_source_facade(
    resource_registry: ResourceRegistry, integration_connections, write_sql_connections
) -> None:
    source = _source_pair(resource_registry, "pair_wrong_scope", occupied_destination=True)
    alias = _desired_alias(integration_connections, write_sql_connections, _ARCHIVE)

    with pytest.raises(InvalidSqlInputError, match="does not contain the verified coordinator"):
        _convert_to_defaults(alias, source.relation)

    _assert_source_unchanged(source)


@pytest.mark.sql_scenario("audit.clickhouse.pair_to_local_occupied_destination")
def test_pair_to_local_defaults_reject_foreign_physical_destination_before_source_rename(
    resource_registry: ResourceRegistry, integration_connections, write_sql_connections
) -> None:
    source = _source_pair(resource_registry, "pair_occupied_destination", occupied_destination=True)
    alias = _desired_alias(integration_connections, write_sql_connections, _CORE)

    with pytest.raises(InvalidSqlInputError, match="Physical replacement destination contains"):
        _convert_to_defaults(alias, source.relation)

    _assert_source_unchanged(source)


@pytest.mark.sql_scenario("audit.clickhouse.pair_to_local_preserves_unrelated_storage")
def test_pair_to_local_defaults_preserve_the_unrelated_coordinator_suffix_table(
    resource_registry: ResourceRegistry, integration_connections, write_sql_connections
) -> None:
    source = _source_pair(resource_registry, "pair_keep_shadow", occupied_destination=False)
    alias = _desired_alias(integration_connections, write_sql_connections, _CORE)
    assert {row[1] for row in source.before[_ARCHIVE]} == {source.shard}
    acknowledge_destination = cleanup_after_acknowledgement(
        resource_registry,
        f"DROP TABLE IF EXISTS {quoted(_DATABASE, source.relation)} "
        f"ON CLUSTER {quoted(_ARCHIVE)} SYNC",
    )

    result = _convert_to_defaults(alias, source.relation)

    assert isinstance(result, sql.SqlOperationResult)
    assert result.data["strategy"] == "pair_to_local"
    acknowledge_destination()
    assert result.data["row_count_validated"] is True
    assert result.data["cleanup_complete"] is True
    assert stored_rows(_ARCHIVE, _DATABASE, source.relation) == source.source_rows
    assert stored_rows(_CORE, _DATABASE, source.shard) == source.shadow_rows
    after_core = inventory(_CORE, [_DATABASE], source.relation)
    assert after_core == [row for row in source.before[_CORE] if row[1] == source.shard]
    after_archive = inventory(_ARCHIVE, [_DATABASE], source.relation)
    assert {row[1] for row in after_archive} == {source.relation}
    assert "MergeTree" in after_archive[0][3]
