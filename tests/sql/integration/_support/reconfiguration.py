"""Run-owned ClickHouse resources and observations for reconfiguration cases."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

from analytics_toolkit import sql
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp

from .identity import query_label
from .identity import resource_name as integration_resource_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from .resources import ResourceRegistry


def label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def resource_name(purpose: str) -> str:
    return integration_resource_name(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
        limit=50,
    )


def quoted(database: str, relation: str | None = None) -> str:
    parts = (database,) if relation is None else (database, relation)
    return TableIdentifier(parts, (True,) * len(parts)).render_quoted("ch")


def literal(value: str) -> str:
    return exp.Literal.string(value).sql(dialect="clickhouse")


def cleanup_after_acknowledgement(registry: ResourceRegistry, drop_sql: str) -> Callable[[], None]:
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute("ch", drop_sql, retry_cnt=1, query_label=label("reconfigure-owned-cleanup"))

    registry.finalizer(cleanup)

    def acknowledge() -> None:
        nonlocal acknowledged
        acknowledged = True

    return acknowledge


def create_owned(registry: ResourceRegistry, create_sql: str, drop_sql: str) -> None:
    # Publish the candidate before CREATE; only its acknowledgement grants
    # cleanup ownership. Plain CREATE never adopts an existing collision.
    acknowledge = cleanup_after_acknowledgement(registry, drop_sql)
    sql.execute("ch", create_sql, retry_cnt=1, query_label=label("reconfigure-owned-create"))
    acknowledge()


def create_storage(registry: ResourceRegistry, database: str, relation: str, cluster: str) -> None:
    table, scope = quoted(database, relation), quoted(cluster)
    create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {scope} "
        "(id UInt64, payload String) ENGINE=MergeTree ORDER BY tuple()",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {scope} SYNC",
    )


def create_facade(
    registry: ResourceRegistry,
    database: str,
    relation: str,
    storage_database: str,
    cluster: str,
) -> None:
    table, scope = quoted(database, relation), quoted(cluster)
    engine = (
        f"Distributed({literal(cluster)}, {literal(storage_database)}, "
        f"{literal(relation + '_shard')}, rand())"
    )
    create_owned(
        registry,
        f"CREATE TABLE {table} ON CLUSTER {scope} (id UInt64, payload String) ENGINE={engine}",
        f"DROP TABLE IF EXISTS {table} ON CLUSTER {scope} SYNC",
    )


def seed_storage(
    cluster: str, database: str, relation: str, *, rows: int, offset: int = 0
) -> list[tuple[int, str]]:
    target = f"cluster({literal(cluster)}, {literal(database)}, {literal(relation)}, rand())"
    sql.execute(
        "ch",
        f"INSERT INTO FUNCTION {target} "
        f"SELECT number + {offset}, concat('value-', toString(number + {offset})) "
        f"FROM numbers({rows})",
        retry_cnt=1,
        query_label=label("reconfigure-owned-seed"),
    )
    return [(value, f"value-{value}") for value in range(offset, offset + rows)]


def stored_rows(cluster: str, database: str, relation: str) -> list[tuple[int, str]]:
    source = f"cluster({literal(cluster)}, {literal(database)}, {literal(relation)})"
    frame = sql.read(
        "ch",
        f"SELECT id, payload FROM {source} ORDER BY id",
        retry_cnt=1,
        query_label=label("reconfigure-owned-values"),
    )
    return list(frame.itertuples(index=False, name=None))


def inventory(cluster: str, databases: list[str], relation: str) -> list[tuple[str, ...]]:
    source = f"clusterAllReplicas({literal(cluster)}, system, tables)"
    database_values = ", ".join(literal(database) for database in databases)
    frame = sql.read(
        "ch",
        "SELECT database, name, toString(uuid) AS identity, create_table_query "
        f"FROM {source} WHERE database IN ({database_values}) "
        f"AND startsWith(name, {literal(relation)}) ORDER BY database, name, identity",
        retry_cnt=1,
        query_label=label("reconfigure-owned-inventory"),
    )
    return list(frame.itertuples(index=False, name=None))
