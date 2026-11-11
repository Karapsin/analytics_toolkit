"""Inspect cleanup ownership without requesting data-read completeness."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.ddl.identifiers import quote_identifier

from .dialect import ClickHouseDialect
from .metadata import extract_clickhouse_distributed_shard_table
from .outage_session import configured_cluster

if TYPE_CHECKING:
    from .layout import ExistingClickHouseLayout


def inspect_outage_layout(session: Any, table_name: str) -> ExistingClickHouseLayout | None:
    from .layout import ExistingClickHouseLayout  # noqa: PLC0415 - layout imports adapter plumbing

    table = parse_one(table_name, read=ClickHouseDialect, into=exp.Table)
    database = table.db or session.config.database
    if not database:
        message = "A database is required for existing-layout inspection."
        raise ClickHouseClusterTopologyError(message)
    cluster = configured_cluster(session.config) if session.config.cluster_routing else None
    objects = (
        session.scope(cluster).objects(database, table.name)
        if cluster is not None
        else session.local_objects(database, table.name)
    )
    physical_scope = cluster if objects else None
    objects = objects or session.local_facade(database, table.name)
    if not objects:
        return None
    original = objects[0]
    if original.engine.lower() != "distributed":
        return ExistingClickHouseLayout(
            logical_table=table_name,
            physical_table=table_name,
            create_distributed_pair=False,
            shard_on_cluster=physical_scope,
            distributed_on_cluster=None,
            distributed_cluster=None,
            ddl=original.create_sql,
            physical_ddl=original.create_sql,
        )
    reference = extract_clickhouse_distributed_shard_table(original.engine_full, database)
    if reference is None:
        message = f"Cannot verify managed ClickHouse storage for {table_name}."
        raise ClickHouseClusterTopologyError(message)
    storage = session.scope(reference.cluster).objects(reference.database, reference.table)
    if not storage or not storage[0].engine.lower().endswith("mergetree"):
        message = f"Cannot verify managed physical storage for {table_name}."
        raise ClickHouseClusterTopologyError(message)
    physical = (
        f"{quote_identifier(reference.database, 'ch')}.{quote_identifier(reference.table, 'ch')}"
    )
    return ExistingClickHouseLayout(
        logical_table=table_name,
        physical_table=physical,
        create_distributed_pair=True,
        shard_on_cluster=reference.cluster,
        distributed_on_cluster=cluster,
        distributed_cluster=reference.cluster,
        ddl=original.create_sql,
        physical_ddl=storage[0].create_sql,
        owns_physical=(reference.database, reference.table) == (database, f"{table.name}_shard"),
    )
