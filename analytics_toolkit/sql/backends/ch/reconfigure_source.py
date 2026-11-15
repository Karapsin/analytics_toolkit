"""Verified existing source identity, independent of desired replacement placement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .layout import inspect_existing_layout
from .reconfigure_ddl import parse_create_table, require_merge_tree
from .reconfigure_support import database_engine, resolve_optional_cluster, table_database

if TYPE_CHECKING:
    from sqlglot import exp

    from .layout import ExistingClickHouseLayout


@dataclass(frozen=True)
class ReconfigureSource:
    table: str
    layout: ExistingClickHouseLayout
    table_create: exp.Create
    database: str
    database_engine: str
    shard_table: str
    shard_create: exp.Create
    cluster: str | None
    resolved_cluster: str | None


def inspect_reconfigure_source(connection: Any, table: str) -> ReconfigureSource:
    # Strict ownership deliberately inspects coordinator metadata, even when a
    # client hook can inspect a broader configured read scope.
    layout = inspect_existing_layout(connection, table, require_owned=True)
    create = parse_create_table(layout.ddl, table)
    database = table_database(connection, create)
    pair = layout.create_distributed_pair
    shard_table = layout.physical_table if pair else table
    shard_create = parse_create_table(layout.physical_ddl, shard_table) if pair else create
    require_merge_tree(shard_create, shard_table)
    cluster = layout.distributed_cluster if pair else None
    resolved_cluster = (
        resolve_optional_cluster(connection, cluster, "ch_distributed_cluster") if pair else None
    )
    return ReconfigureSource(
        table=table,
        layout=layout,
        table_create=create,
        database=database,
        database_engine=database_engine(connection, database),
        shard_table=shard_table,
        shard_create=shard_create,
        cluster=cluster,
        resolved_cluster=resolved_cluster,
    )
