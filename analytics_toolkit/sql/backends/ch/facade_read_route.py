"""Verify read equivalence without claiming ownership of a facade destination."""

from __future__ import annotations

from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .dialect import ClickHouseDialect
from .managed_routing import ManagedTableRoute


def _projection(create_sql: str) -> tuple[Any, ...]:
    try:
        create = parse_one(create_sql, read=ClickHouseDialect)
    except SqlglotError as exc:
        message = "Cannot verify Distributed facade projection metadata."
        raise ClickHouseClusterTopologyError(message) from exc
    if not isinstance(create, exp.Create) or not isinstance(create.this, exp.Schema):
        message = "Distributed facade routing requires explicit column schema metadata."
        raise ClickHouseClusterTopologyError(message)
    columns = []
    for column in create.this.expressions:
        if not isinstance(column, exp.ColumnDef):
            continue
        kind = column.args.get("kind")
        if not isinstance(kind, exp.DataType):
            message = "Cannot verify the Distributed facade's declared column types."
            raise ClickHouseClusterTopologyError(message)
        columns.append(
            (
                column.name,
                kind.sql(dialect=ClickHouseDialect),
                tuple(
                    constraint.sql(dialect=ClickHouseDialect)
                    for constraint in column.args.get("constraints") or []
                ),
            )
        )
    if not columns:
        message = "Distributed facade projection metadata is empty."
        raise ClickHouseClusterTopologyError(message)
    return tuple(columns)


def verify_facade_projection(logical_objects: list[Any], physical: list[Any]) -> None:
    """Prove that replacing the facade by its storage preserves column semantics."""
    reference = _projection(logical_objects[0].create_sql)
    if any(_projection(item.create_sql) != reference for item in physical):
        message = "Distributed facade and physical target column projections differ."
        raise ClickHouseClusterTopologyError(message)


def independent_facade_read_route(  # noqa: PLR0913 - one verified cross-scope routing decision.
    session: Any,
    logical_objects: list[Any],
    physical: list[Any],
    destination: Any,
    shard: Any,
    *,
    writing: bool,
) -> ManagedTableRoute | None:
    if writing or session.config.cluster_routing is None:
        return None
    replicated = physical[0].engine.lower().startswith(("replicated", "shared"))
    multi_replica = len({host.shard for host in destination.hosts}) < len(destination.hosts)
    if replicated or not multi_replica:
        return None
    verify_facade_projection(logical_objects, physical)
    return ManagedTableRoute(
        shard.database,
        shard.table,
        "all_replicas",
        physical[0].engine,
        cluster=destination.cluster,
    )
