"""Disposable checks for physical and facade properties during reconfiguration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.reconfiguration import (
    cleanup_after_acknowledgement as _cleanup_after_acknowledgement,
)
from tests.sql.integration._support.reconfiguration import create_facade as _create_facade
from tests.sql.integration._support.reconfiguration import create_owned as _create_owned
from tests.sql.integration._support.reconfiguration import create_storage as _create_storage
from tests.sql.integration._support.reconfiguration import inventory as _inventory
from tests.sql.integration._support.reconfiguration import label as _label
from tests.sql.integration._support.reconfiguration import quoted as _quoted
from tests.sql.integration._support.reconfiguration import resource_name as _resource_name
from tests.sql.integration._support.reconfiguration import seed_storage as _seed_storage
from tests.sql.integration._support.reconfiguration import stored_rows as _stored_rows

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _columns(ddl: str) -> dict[str, exp.ColumnDef]:
    create = parse_one(ddl, read="clickhouse")
    assert isinstance(create, exp.Create)
    schema = create.this
    assert isinstance(schema, exp.Schema)
    return {
        column.name: column for column in schema.expressions if isinstance(column, exp.ColumnDef)
    }


def _has_column_ttl(column: exp.ColumnDef) -> bool:
    return any(
        isinstance(constraint.args["kind"], exp.MergeTreeTTL)
        for constraint in column.args.get("constraints", [])
    )


def _common_constraints(column: exp.ColumnDef) -> list[str]:
    return [
        constraint.sql(dialect="clickhouse")
        for constraint in column.args.get("constraints", [])
        if not isinstance(constraint.args["kind"], exp.MergeTreeTTL)
    ]


@pytest.mark.sql_scenario("audit.clickhouse.column_ttl_reconfigure")
def test_local_to_pair_keeps_column_ttl_only_on_physical_storage(
    resource_registry: ResourceRegistry,
) -> None:
    alias, cluster = "ch", "integration_cluster"
    table = resource_registry.table(
        alias, integration_table("ch", "column_ttl"), ch_cluster=cluster
    )
    shard = resource_registry.table(alias, f"{table}_shard", ch_cluster=cluster)
    sql.execute(
        alias,
        f"CREATE TABLE {table} ("
        "id UInt64 DEFAULT 7 COMMENT 'id TTL stays text', "
        "ts DateTime('UTC') DEFAULT toDateTime('2099-01-01 00:00:00', 'UTC') "
        "TTL ts + INTERVAL 1 DAY COMMENT 'ts comment', "
        "payload String) ENGINE=MergeTree ORDER BY id",
        retry_cnt=1,
        query_label=_label("column-ttl-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {table} (id, payload) VALUES (1, 'first'), (2, 'second')",
        retry_cnt=1,
        query_label=_label("column-ttl-seed"),
    )
    original_columns = _columns(sql.extract_ddl(alias, table))
    assert _has_column_ttl(original_columns["ts"])
    result = sql.ch_reconfigure_table(
        alias,
        table,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster=cluster,
        ch_distributed_on_cluster=cluster,
        ch_distributed_cluster=cluster,
        retry_cnt=1,
        return_metadata=True,
        query_label=_label("column-ttl-reconfigure"),
    )
    assert isinstance(result, sql.SqlOperationResult)
    assert result.data["strategy"] == "local_to_pair"
    assert result.data["row_count_validated"] is True
    assert result.data["cleanup_complete"] is True
    facade_ddl, shard_ddl = sql.extract_ddl(alias, table), sql.extract_ddl(alias, shard)
    assert "Distributed(" in facade_ddl
    facade_columns, shard_columns = _columns(facade_ddl), _columns(shard_ddl)
    assert _has_column_ttl(shard_columns["ts"])
    assert shard_columns["ts"] == original_columns["ts"]
    assert all(not _has_column_ttl(column) for column in facade_columns.values())
    for columns in (facade_columns, shard_columns):
        assert set(columns) == set(original_columns)
        for name, original in original_columns.items():
            assert columns[name].args["kind"] == original.args["kind"]
            assert _common_constraints(columns[name]) == _common_constraints(original)
    for actual_table in (table, shard):
        actual = sql.read(
            alias,
            f"SELECT id, ts, payload FROM {actual_table} ORDER BY id",
            retry_cnt=1,
            query_label=_label("column-ttl-check"),
        )
        assert list(actual.itertuples(index=False, name=None)) == [
            (1, pd.Timestamp("2099-01-01", tz="UTC"), "first"),
            (2, pd.Timestamp("2099-01-01", tz="UTC"), "second"),
        ]
    inspected_plan = sql.ch_reconfigure_table(
        alias,
        table,
        ch_distributed_table=False,
        ch_shard_on_cluster=cluster,
        ch_distributed_on_cluster=cluster,
        dry_run=True,
        retry_cnt=1,
        query_label=_label("column-ttl-inspect-pair"),
    )
    assert isinstance(inspected_plan, sql.SqlPlan)
    assert inspected_plan.options["strategy"] == "pair_to_local"


@pytest.mark.sql_scenario("audit.clickhouse.cross_database_reconfigure_ownership")
def test_cross_database_facade_never_adopts_a_populated_local_suffix(
    resource_registry: ResourceRegistry,
) -> None:
    core, database = "integration_cluster", "integration"
    physical_database, relation = _resource_name("physical_db"), _resource_name("crossdb_owner")
    scope, quoted_database = _quoted(core), _quoted(physical_database)
    _create_owned(
        resource_registry,
        f"CREATE DATABASE {quoted_database} ON CLUSTER {scope}",
        f"DROP DATABASE IF EXISTS {quoted_database} ON CLUSTER {scope} SYNC",
    )
    shard = relation + "_shard"
    _create_storage(resource_registry, physical_database, shard, core)
    _create_storage(resource_registry, database, shard, core)
    _create_facade(resource_registry, database, relation, physical_database, core)
    physical_rows = _seed_storage(core, physical_database, shard, rows=3)
    unrelated_rows = _seed_storage(core, database, shard, rows=100, offset=1000)
    before = _inventory(core, [database, physical_database], relation)
    assert _stored_rows(core, database, relation) == physical_rows

    with pytest.raises(InvalidSqlInputError, match="not a managed Distributed/_shard pair"):
        sql.ch_reconfigure_table(
            "ch",
            _quoted(database, relation),
            order_by="payload",
            ch_shard_on_cluster=core,
            ch_distributed_on_cluster=core,
            retry_cnt=1,
            query_label=_label("crossdb-reconfigure-rejected"),
        )

    assert _inventory(core, [database, physical_database], relation) == before
    assert _stored_rows(core, database, relation) == physical_rows
    assert _stored_rows(core, physical_database, shard) == physical_rows
    assert _stored_rows(core, database, shard) == unrelated_rows


@pytest.mark.sql_scenario("audit.clickhouse.same_physical_routing_reconfigure")
def test_same_physical_rebuild_rejects_a_disjoint_facade_route(
    resource_registry: ResourceRegistry,
) -> None:
    core, archive, database = "integration_cluster", "integration_archive_cluster", "integration"
    relation = _resource_name("route_owner")
    shard = relation + "_shard"
    for cluster in (core, archive):
        _create_storage(resource_registry, database, shard, cluster)
    _create_facade(resource_registry, database, relation, database, core)
    physical_rows = _seed_storage(core, database, shard, rows=3)
    archive_rows = _seed_storage(archive, database, shard, rows=100, offset=1000)
    before = {cluster: _inventory(cluster, [database], relation) for cluster in (core, archive)}
    assert _stored_rows(core, database, relation) == physical_rows

    # The order change requires a physical copy; placement remains on core.
    with pytest.raises(InvalidSqlInputError, match="replacement storage endpoints"):
        sql.ch_reconfigure_table(
            "ch",
            _quoted(database, relation),
            order_by="id",
            ch_distributed_table=True,
            ch_shard_on_cluster=core,
            ch_distributed_on_cluster=core,
            ch_distributed_cluster=archive,
            retry_cnt=1,
            query_label=_label("disjoint-facade-route-rejected"),
        )

    assert {
        cluster: _inventory(cluster, [database], relation) for cluster in (core, archive)
    } == before
    assert _stored_rows(core, database, relation) == physical_rows
    assert _stored_rows(core, database, shard) == physical_rows
    assert _stored_rows(archive, database, shard) == archive_rows


@pytest.mark.sql_scenario("audit.clickhouse.local_to_pair_facade_collision")
def test_local_to_pair_rejects_a_facade_scope_that_contains_independent_originals(
    resource_registry: ResourceRegistry,
) -> None:
    local, peer, physical = (
        "integration_cluster",
        "integration_archive_cluster",
        "integration_two_shards",
    )
    database, relation = "integration", _resource_name("local_pair_reject")
    for cluster in (local, peer):
        _create_storage(resource_registry, database, relation, cluster)
    local_rows = _seed_storage(local, database, relation, rows=3)
    peer_rows = _seed_storage(peer, database, relation, rows=100, offset=1000)
    before = {cluster: _inventory(cluster, [database], relation) for cluster in (local, peer)}

    with pytest.raises(InvalidSqlInputError, match="unowned logical table"):
        sql.ch_reconfigure_table(
            "ch",
            _quoted(database, relation),
            ch_distributed_table=True,
            ch_engine="MergeTree",
            ch_shard_on_cluster=physical,
            ch_distributed_on_cluster=physical,
            ch_distributed_cluster=physical,
            ch_sharding_key="cityHash64(id)",
            retry_cnt=1,
            query_label=_label("local-pair-peer-collision-rejected"),
        )

    assert {
        cluster: _inventory(cluster, [database], relation) for cluster in (local, peer)
    } == before
    assert _stored_rows(local, database, relation) == local_rows
    assert _stored_rows(peer, database, relation) == peer_rows


@pytest.mark.sql_scenario("audit.clickhouse.local_to_pair_scope_preservation")
def test_local_to_pair_preserves_independent_remote_originals_during_cutover_and_cleanup(
    resource_registry: ResourceRegistry,
) -> None:
    local, peer, physical = (
        "integration_cluster",
        "integration_archive_cluster",
        "integration_two_shards",
    )
    database, relation = "integration", _resource_name("local_pair_keep")
    for cluster in (local, peer):
        _create_storage(resource_registry, database, relation, cluster)
    local_rows = _seed_storage(local, database, relation, rows=3)
    peer_rows = _seed_storage(peer, database, relation, rows=100, offset=1000)
    before_peer = _inventory(peer, [database], relation)
    shard = relation + "_shard"
    acknowledge_shard = _cleanup_after_acknowledgement(
        resource_registry,
        f"DROP TABLE IF EXISTS {_quoted(database, shard)} ON CLUSTER {_quoted(physical)} SYNC",
    )

    result = sql.ch_reconfigure_table(
        "ch",
        _quoted(database, relation),
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster=physical,
        ch_distributed_on_cluster=local,
        ch_distributed_cluster=physical,
        ch_sharding_key="cityHash64(id)",
        retry_cnt=1,
        return_metadata=True,
        query_label=_label("local-pair-preserve-peer-original"),
    )
    assert isinstance(result, sql.SqlOperationResult)
    assert result.data["strategy"] == "local_to_pair"
    acknowledge_shard()
    assert result.data["row_count_validated"] is True
    assert result.data["cleanup_complete"] is True
    assert _stored_rows(local, database, relation) == local_rows
    assert _stored_rows(physical, database, shard) == local_rows
    assert _stored_rows(peer, database, relation) == peer_rows
    after_peer = _inventory(peer, [database], relation)
    assert [row for row in after_peer if row[1] == relation] == before_peer
    assert {row[1] for row in after_peer} == {relation, shard}
    assert {row[1] for row in _inventory(local, [database], relation)} == {relation, shard}
