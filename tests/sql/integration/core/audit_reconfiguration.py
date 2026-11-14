"""Disposable checks for physical and facade properties during reconfiguration."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.identity import query_label, resource_name

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


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


def _resource_name(purpose: str) -> str:
    return resource_name(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
        limit=50,
    )


def _quoted(database: str, relation: str | None = None) -> str:
    parts = (database,) if relation is None else (database, relation)
    return TableIdentifier(parts, (True,) * len(parts)).render_quoted("ch")


def _literal(value: str) -> str:
    return exp.Literal.string(value).sql(dialect="clickhouse")


def _create_owned(registry: ResourceRegistry, create_sql: str, drop_sql: str) -> None:
    # Make the candidate visible before CREATE; only its acknowledgement grants
    # cleanup ownership. Plain CREATE never adopts an existing collision.
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute(
                "ch", drop_sql, retry_cnt=1, query_label=_label("reconfigure-owned-cleanup")
            )

    registry.finalizer(cleanup)
    sql.execute("ch", create_sql, retry_cnt=1, query_label=_label("reconfigure-owned-create"))
    acknowledged = True


def _create_storage(registry: ResourceRegistry, database: str, relation: str, cluster: str) -> None:
    table, scope = _quoted(database, relation), _quoted(cluster)
    _create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {scope} "
        "(id UInt64, payload String) ENGINE=MergeTree ORDER BY tuple()",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {scope} SYNC",
    )


def _create_facade(
    registry: ResourceRegistry,
    database: str,
    relation: str,
    storage_database: str,
    cluster: str,
) -> None:
    table, scope = _quoted(database, relation), _quoted(cluster)
    engine = (
        f"Distributed({_literal(cluster)}, {_literal(storage_database)}, "
        f"{_literal(relation + '_shard')}, rand())"
    )
    _create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {scope} (id UInt64, payload String) ENGINE={engine}",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {scope} SYNC",
    )


def _seed_storage(
    cluster: str, database: str, relation: str, *, rows: int, offset: int = 0
) -> list[tuple[int, str]]:
    target = f"cluster({_literal(cluster)}, {_literal(database)}, {_literal(relation)}, rand())"
    sql.execute(
        "ch",
        f"INSERT INTO FUNCTION {target} "
        f"SELECT number + {offset}, concat('value-', toString(number + {offset})) "
        f"FROM numbers({rows})",
        retry_cnt=1,
        query_label=_label("reconfigure-owned-seed"),
    )
    return [(value, f"value-{value}") for value in range(offset, offset + rows)]


def _stored_rows(cluster: str, database: str, relation: str) -> list[tuple[int, str]]:
    source = f"cluster({_literal(cluster)}, {_literal(database)}, {_literal(relation)})"
    frame = sql.read(
        "ch",
        f"SELECT id, payload FROM {source} ORDER BY id",
        retry_cnt=1,
        query_label=_label("reconfigure-owned-values"),
    )
    return list(frame.itertuples(index=False, name=None))


def _inventory(cluster: str, databases: list[str], relation: str) -> list[tuple[str, ...]]:
    source = f"clusterAllReplicas({_literal(cluster)}, system, tables)"
    database_values = ", ".join(_literal(database) for database in databases)
    frame = sql.read(
        "ch",
        "SELECT database, name, toString(uuid) AS identity, create_table_query "
        f"FROM {source} WHERE database IN ({database_values}) "
        f"AND startsWith(name, {_literal(relation)}) ORDER BY database, name, identity",
        retry_cnt=1,
        query_label=_label("reconfigure-owned-inventory"),
    )
    return list(frame.itertuples(index=False, name=None))


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
