"""Verify logical-table ownership before facade and pair-to-local cutovers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .reconfigure_ddl import comparable_create_sql, parse_create_table, retarget_create
from .reconfigure_facade_relationship import FacadeSource, verify_facade_relationships
from .reconfigure_facade_scope import (
    inspect_facade_scope,
    occupied_definitions,
)

if TYPE_CHECKING:
    from .reconfigure_facade_scope import FacadeScope


@dataclass(frozen=True)
class _FacadeOwnership:
    scope: FacadeScope | None
    definitions: dict[int, str]
    source_ddl: str | None


@dataclass(frozen=True)
class FacadeMutation:
    table: str | None
    scope: str | None
    source_ddl: str | None = None
    source_cluster: str | None = None
    physical_scope: str | None = None
    pair_to_local: bool = False
    coordinator_required: bool = True


def _certify_facades(
    connection: Any,
    mutation: FacadeMutation,
) -> _FacadeOwnership:
    table = cast("str", mutation.table)
    cluster = mutation.scope
    source_ddl = mutation.source_ddl
    if cluster is None:
        return _FacadeOwnership(None, {}, source_ddl)
    scope = inspect_facade_scope(
        connection, cluster, coordinator_required=mutation.coordinator_required
    )
    occupied = occupied_definitions(connection, table, scope)
    if scope.coordinator is not None and scope.coordinator not in occupied:
        message = "Cannot verify the coordinator logical table in the facade execution scope."
        raise InvalidSqlInputError(message)
    if source_ddl is None:
        if set(occupied) - {scope.coordinator}:
            message = (
                "Facade destination already contains an unowned logical table; "
                "refusing local-to-pair conversion."
            )
            raise InvalidSqlInputError(message)
    else:
        if set(occupied) != set(scope.identities):
            message = "Cannot verify every original facade in the execution scope."
            raise InvalidSqlInputError(message)
        verify_facade_relationships(
            connection,
            table,
            scope,
            occupied,
            FacadeSource(source_ddl, cast("str", mutation.source_cluster)),
        )
    return _FacadeOwnership(scope, occupied, source_ddl)


def _vacating_definition(
    ownership: _FacadeOwnership, target: FacadeScope, position: int
) -> str | None:
    if target.coordinator == position:
        return ownership.source_ddl
    if ownership.scope is None:
        return None
    endpoint = target.endpoints[position - 1].native
    identity = target.identities[position]
    matches = [
        old_position
        for old_position, old_endpoint in enumerate(ownership.scope.endpoints, 1)
        if old_endpoint.native == endpoint
        and ownership.scope.identities[old_position] == identity
        and old_position in ownership.definitions
    ]
    return ownership.definitions[matches[0]] if len(matches) == 1 else None


def _canonical_definition(ddl: str, table: str) -> str:
    return comparable_create_sql(retarget_create(parse_create_table(ddl, table), table, None))


def _validate_physical_destination(
    connection: Any, table: str, cluster: str | None, ownership: _FacadeOwnership
) -> None:
    if cluster is None:
        return
    scope = inspect_facade_scope(connection, cluster, coordinator_required=False)
    occupied = occupied_definitions(connection, table, scope)
    for position, ddl in occupied.items():
        original = _vacating_definition(ownership, scope, position)
        if original is None or _canonical_definition(ddl, table) != _canonical_definition(
            original, table
        ):
            message = "Physical replacement destination contains an unowned logical table."
            raise InvalidSqlInputError(message)


def validate_facade_destination(
    connection: Any,
    mutation: FacadeMutation,
) -> None:
    """Certify only existing source resources that the selected cutover will vacate."""
    if mutation.table is None:
        message = "Cannot verify the logical table identity for the facade cutover."
        raise InvalidSqlInputError(message)
    ownership = _certify_facades(connection, mutation)
    if mutation.pair_to_local:
        _validate_physical_destination(
            connection, mutation.table, mutation.physical_scope, ownership
        )


def validate_local_facade_destination(
    connection: Any, table: str | None, scope: str | None
) -> None:
    """Retain the local-conversion helper while sharing the complete ownership proof."""
    validate_facade_destination(connection, FacadeMutation(table, scope))


__all__ = ["FacadeMutation", "validate_facade_destination", "validate_local_facade_destination"]
