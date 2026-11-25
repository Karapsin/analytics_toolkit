"""Complete native endpoint evidence for logical-table ownership checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple, cast
from uuid import UUID

from sqlglot import exp

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .dialect import ClickHouseDialect
from .metadata_values import decode_metadata_rows
from .outage_context import internal_io
from .reconfigure_ddl import distributed_table_parts
from .reconfigure_support import resolve_optional_cluster
from .routing import query_local

_MEMBERSHIP_FIELDS = 6
_IDENTITY_FIELDS = 2
_PROBE_FIELDS = 3
_DEFINITION_FIELDS = 2
_MAX_PORT = 65535

ServerIdentity = Tuple[UUID, int]


@dataclass(frozen=True)
class FacadeEndpoint:
    native: tuple[str, int]
    local_candidate: bool


@dataclass(frozen=True)
class FacadeScope:
    cluster: str
    endpoints: tuple[FacadeEndpoint, ...]
    identities: dict[int, ServerIdentity]
    local_identity: ServerIdentity
    coordinator: int | None
    membership: tuple[tuple[int, int, str], ...] = ()


def read_rows(connection: Any, statement: str, *, text_columns: tuple[int, ...] = ()) -> list[Any]:
    with internal_io():
        result = query_local(connection, statement, settings={"skip_unavailable_shards": 0})
    return decode_metadata_rows(list(getattr(result, "result_rows", None) or []), text_columns)


def cluster_table(cluster: str, table: str) -> exp.Table:
    return exp.Table(
        this=exp.Anonymous(
            this="clusterAllReplicas",
            expressions=[
                exp.Literal.string(cluster),
                exp.to_identifier("system"),
                exp.to_identifier(table),
            ],
        )
    )


def _configured_endpoints(
    connection: Any, cluster: str
) -> tuple[tuple[FacadeEndpoint, ...], tuple[tuple[int, int, str], ...]]:
    query = (
        exp.select("shard_num", "replica_num", "host_name", "host_address", "port", "is_local")
        .from_("system.clusters")
        .where(exp.column("cluster").eq(exp.Literal.string(cluster)))
        .order_by("shard_num", "replica_num")
    )
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect), text_columns=(2, 3))
    message = "Cannot verify complete facade endpoint coverage."
    if not rows or any(len(row) != _MEMBERSHIP_FIELDS for row in rows):
        raise InvalidSqlInputError(message)
    try:
        members = [(int(row[0]), int(row[1])) for row in rows]
        endpoints = tuple(
            FacadeEndpoint((str(row[3] or row[2] or ""), int(row[4])), bool(row[5])) for row in rows
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise InvalidSqlInputError(message) from exc
    valid = (
        len(set(members)) == len(rows)
        and all(shard > 0 and replica > 0 for shard, replica in members)
        and len({endpoint.native for endpoint in endpoints}) == len(rows)
        and all(
            address and 0 < port <= _MAX_PORT
            for address, port in (endpoint.native for endpoint in endpoints)
        )
        and all(row[5] in (0, 1) for row in rows)
    )
    if not valid:
        raise InvalidSqlInputError(message)
    membership = tuple(
        (shard, replica, str(row[2])) for (shard, replica), row in zip(members, rows)
    )
    return endpoints, membership


def _identity(values: Any) -> ServerIdentity:
    if len(values) != _IDENTITY_FIELDS:
        message = "Incomplete facade server identity."
        raise ValueError(message)
    identity, port = UUID(str(values[0])), int(values[1])
    if not identity.int or not 0 <= port <= _MAX_PORT:
        message = "Invalid facade server identity."
        raise ValueError(message)
    return identity, port


def inspect_facade_scope(
    connection: Any, cluster: str, *, coordinator_required: bool = True
) -> FacadeScope:
    with internal_io():
        resolved = cast(
            "str", resolve_optional_cluster(connection, cluster, "ch_distributed_on_cluster")
        )
    endpoints, membership = _configured_endpoints(connection, resolved)
    message = "Facade execution scope does not contain the verified coordinator endpoint."
    local = read_rows(connection, "SELECT serverUUID(), tcpPort()", text_columns=(0,))
    query = exp.select(
        exp.column("_shard_num"),
        exp.Anonymous(this="serverUUID"),
        exp.Anonymous(this="tcpPort"),
    ).from_(cluster_table(resolved, "one"))
    probe = read_rows(connection, query.sql(dialect=ClickHouseDialect), text_columns=(1,))
    if len(local) != 1 or any(len(row) != _PROBE_FIELDS for row in probe):
        raise InvalidSqlInputError(message)
    try:
        coordinator_identity = _identity(local[0])
        reached = {int(row[0]): _identity(row[1:]) for row in probe}
    except (IndexError, TypeError, ValueError) as exc:
        raise InvalidSqlInputError(message) from exc
    if (
        len(reached) != len(probe)
        or set(reached) != set(range(1, len(endpoints) + 1))
        or len(set(reached.values())) != len(probe)
    ):
        raise InvalidSqlInputError(message)
    candidates = [
        position for position, endpoint in enumerate(endpoints, 1) if endpoint.local_candidate
    ]
    coordinator = candidates[0] if len(candidates) == 1 else None
    if (
        len(candidates) > 1
        or (coordinator is not None and reached[coordinator] != coordinator_identity)
        or (coordinator is None and coordinator_identity in reached.values())
        or (coordinator_required and coordinator is None)
    ):
        raise InvalidSqlInputError(message)
    return FacadeScope(resolved, endpoints, reached, coordinator_identity, coordinator, membership)


def occupied_definitions(connection: Any, table: str, scope: FacadeScope) -> dict[int, str]:
    database, relation = distributed_table_parts(table)
    query = (
        exp.select("_shard_num", "create_table_query")
        .from_(cluster_table(scope.cluster, "tables"))
        .where(
            exp.column("database").eq(exp.Literal.string(database)),
            exp.column("name").eq(exp.Literal.string(relation)),
        )
    )
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect), text_columns=(1,))
    message = "Cannot verify facade destination table coverage."
    if any(len(row) != _DEFINITION_FIELDS or not isinstance(row[1], str) for row in rows):
        raise InvalidSqlInputError(message)
    try:
        definitions = {int(row[0]): row[1] for row in rows}
    except (IndexError, TypeError, ValueError) as exc:
        raise InvalidSqlInputError(message) from exc
    if (
        len(definitions) != len(rows)
        or not set(definitions) <= set(scope.identities)
        or any(not ddl for ddl in definitions.values())
    ):
        raise InvalidSqlInputError(message)
    return definitions


__all__ = [
    "FacadeScope",
    "cluster_table",
    "inspect_facade_scope",
    "occupied_definitions",
    "read_rows",
]
