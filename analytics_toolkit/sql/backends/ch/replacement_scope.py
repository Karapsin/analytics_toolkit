"""Authorize replacement resources from complete existing endpoint evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .managed_routing import _ClusterObject, _normalized_definition
from .outage_context import internal_io
from .reconfigure_ddl import comparable_create_sql, engine_name, engine_sql, parse_create_table
from .reconfigure_facade import FacadeMutation, validate_facade_destination
from .reconfigure_facade_relationship import FacadeSource, verify_facade_relationships
from .reconfigure_facade_scope import inspect_facade_scope, occupied_definitions
from .reconfigure_support import resolve_optional_cluster
from .scope_identity import grouped_scope_identity as _scope_identity

if TYPE_CHECKING:
    from .creation_policy import ClickHouseCreationPolicy
    from .layout import ExistingClickHouseLayout
    from .reconfigure_facade_scope import FacadeScope
    from .reconfigure_physical import PhysicalRebuildExecutionScope


def _physical_signature(ddl: str, table: str) -> str:
    create = parse_create_table(ddl, table)
    return _normalized_definition(
        _ClusterObject(
            "", engine_name(create), engine_sql(create), comparable_create_sql(create), ""
        )
    )


def _require_source_table(
    connection: Any, layout: ExistingClickHouseLayout, scope: FacadeScope
) -> dict[int, str]:
    definitions = occupied_definitions(connection, layout.physical_table, scope)
    if set(definitions) != set(scope.identities):
        message = "Cannot verify complete existing physical table coverage before replacement."
        raise InvalidSqlInputError(message)
    expected = _physical_signature(layout.physical_ddl, layout.physical_table)
    if any(
        _physical_signature(ddl, layout.physical_table) != expected for ddl in definitions.values()
    ):
        message = "Existing physical table definitions differ across the replacement scope."
        raise InvalidSqlInputError(message)
    return definitions


def inspect_completed_physical_layout(
    connection: Any, execution: PhysicalRebuildExecutionScope
) -> ExistingClickHouseLayout:
    """Inspect an acknowledged conversion's actual destination, not its removed facade."""
    from .layout import (  # noqa: PLC0415 - layout/replacement initialization cycle
        ExistingClickHouseLayout,
        inspect_existing_layout,
    )
    from .reconfigure_physical import ClusterPhysicalStorage  # noqa: PLC0415 - adapter cycle

    if get_connection_config(execution.config.connection_key) != execution.config:
        message = "ClickHouse configuration changed after the completed layout conversion."
        raise InvalidSqlInputError(message)
    target = execution.target
    if not isinstance(target, ClusterPhysicalStorage):
        actual = inspect_existing_layout(connection, target.table, require_owned=True)
        if actual.create_distributed_pair or _physical_signature(
            actual.physical_ddl, target.table
        ) != _physical_signature(target.ddl, target.table):
            message = "The completed local conversion target definition could not be verified."
            raise InvalidSqlInputError(message)
        return actual
    scope = inspect_facade_scope(connection, target.scope.cluster, coordinator_required=False)
    if scope != target.scope:
        message = "The completed physical conversion endpoint scope changed before replacement."
        raise InvalidSqlInputError(message)
    expected = ExistingClickHouseLayout(
        logical_table=target.table,
        physical_table=target.table,
        create_distributed_pair=False,
        shard_on_cluster=scope.cluster,
        distributed_on_cluster=None,
        distributed_cluster=None,
        ddl=target.ddl,
        physical_ddl=target.ddl,
    )
    definitions = _require_source_table(connection, expected, scope)
    actual_ddl = definitions[min(scope.identities)]
    return replace(expected, ddl=actual_ddl, physical_ddl=actual_ddl)


def _scope(connection: Any, cluster: str, cache: dict[str, FacadeScope]) -> FacadeScope:
    if cluster not in cache:
        cache[cluster] = inspect_facade_scope(connection, cluster, coordinator_required=False)
    return cache[cluster]


def _same_scope(
    connection: Any,
    original: str | None,
    desired: str | None,
    cache: dict[str, FacadeScope],
) -> bool:
    if original is None:
        if desired is None:
            return True
        candidate = _scope(connection, desired, cache)
        return len(candidate.endpoints) == 1 and candidate.coordinator is not None
    if desired is None:
        source = _scope(connection, original, cache)
        return len(source.endpoints) == 1 and source.coordinator is not None
    source = _scope(connection, original, cache)
    destination = _scope(connection, desired, cache)
    return _scope_identity(source) == _scope_identity(destination)


def _direct_facade_scope(
    connection: Any,
    layout: ExistingClickHouseLayout,
    source_cluster: str,
    desired: str | None,
    cache: dict[str, FacadeScope],
) -> None:
    if desired is None:
        return
    scope = _scope(connection, desired, cache)
    definitions = occupied_definitions(connection, layout.logical_table, scope)
    if scope.coordinator is None or scope.coordinator not in definitions:
        message = "Cannot verify existing facade ownership in the desired replacement scope."
        raise InvalidSqlInputError(message)
    verify_facade_relationships(
        connection,
        layout.logical_table,
        scope,
        definitions,
        FacadeSource(layout.ddl, source_cluster),
    )


def _require_staged_facade_scope(
    connection: Any,
    layout: ExistingClickHouseLayout,
    desired: str | None,
    cache: dict[str, FacadeScope],
) -> None:
    original = resolve_optional_cluster(connection, layout.distributed_on_cluster)
    if original is not None and not _same_scope(connection, original, desired, cache):
        message = (
            "Facade placement differs from its verified existing scope. "
            "Use ch_reconfigure_table to migrate that layout before replacing it."
        )
        raise InvalidSqlInputError(message)


def same_storage_scope(connection: Any, original: str | None, desired: str | None) -> bool:
    """Compare complete grouped instance identities, including named local aliases."""
    with internal_io():
        original = resolve_optional_cluster(connection, original)
        desired = resolve_optional_cluster(connection, desired)
        return _same_scope(connection, original, desired, {})


def certify_replacement_scope(
    connection: Any,
    layout: ExistingClickHouseLayout,
    policy: ClickHouseCreationPolicy,
    *,
    staged_exchange: bool,
) -> None:
    """Certify same-topology replacement without inferring a physical migration."""
    if not layout.owns_physical:
        message = "Cannot structurally replace externally owned physical storage."
        raise InvalidSqlInputError(message)
    with internal_io():
        original = resolve_optional_cluster(
            connection,
            layout.distributed_cluster
            if layout.create_distributed_pair
            else layout.shard_on_cluster,
        )
        desired = resolve_optional_cluster(connection, policy.shard_on_cluster)
        cache: dict[str, FacadeScope] = {}
        if not _same_scope(connection, original, desired, cache):
            message = (
                "Physical placement differs from the verified existing storage. "
                "Use ch_reconfigure_table to migrate the verified layout before replacing it."
            )
            raise InvalidSqlInputError(message)
        if original is not None:
            _require_source_table(connection, layout, _scope(connection, original, cache))
        elif desired is not None:
            _require_source_table(connection, layout, _scope(connection, desired, cache))
        if not layout.create_distributed_pair:
            return
        if original is None:
            message = "Cannot verify the existing Distributed storage scope before replacement."
            raise InvalidSqlInputError(message)
        routing = resolve_optional_cluster(connection, policy.distributed_cluster)
        if routing is None or not _same_scope(connection, original, routing, cache):
            message = (
                "Distributed replacement routing differs from the verified existing storage. "
                "Reconfigure that layout before replacing it."
            )
            raise InvalidSqlInputError(message)
        facade = resolve_optional_cluster(connection, policy.distributed_on_cluster)
        if staged_exchange:
            _require_staged_facade_scope(connection, layout, facade, cache)
            validate_facade_destination(
                connection,
                FacadeMutation(
                    table=layout.logical_table,
                    scope=facade,
                    source_ddl=layout.ddl,
                    source_cluster=original,
                    coordinator_required=False,
                ),
            )
        else:
            _direct_facade_scope(connection, layout, original, facade, cache)


__all__ = [
    "certify_replacement_scope",
    "inspect_completed_physical_layout",
    "same_storage_scope",
]
