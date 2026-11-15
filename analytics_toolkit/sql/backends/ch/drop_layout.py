"""Certify the exact existing resources authorized for a ClickHouse drop."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.config import ChConfig, get_connection_config
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterUnavailableError,
    InvalidSqlInputError,
)
from analytics_toolkit.sql.execution.backend_options import preflight_sql_statements
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

from .ddl import build_ch_shard_table_name
from .dialect import ClickHouseDialect
from .layout import _column_identity
from .metadata import extract_clickhouse_distributed_shard_table
from .outage_context import internal_io
from .outage_session import configured_cluster
from .reconfigure_ddl import engine_name, engine_sql, parse_create_table, require_merge_tree
from .reconfigure_execution import cluster_clause
from .reconfigure_facade_relationship import (
    _resolved_names,
    _routing_endpoints,
    _scope_macros,
    facade_definition,
)
from .reconfigure_facade_scope import (
    _identity,
    cluster_table,
    inspect_facade_scope,
    occupied_definitions,
    read_rows,
)
from .reconfigure_support import (
    resolve_optional_cluster,
    resolve_required_cluster,
    unquoted_table_name,
)
from .replacement_scope import _physical_signature

if TYPE_CHECKING:
    from .reconfigure_facade_scope import FacadeScope, ServerIdentity


@dataclass(frozen=True)
class DropIntent:
    connection_key: str
    cluster: str | None
    drop_shard: bool = True
    drop_distributed: bool = True
    if_exists: bool = True
    physical_table: str | None = None


@dataclass(frozen=True)
class VerifiedDropResource:
    table: str
    role: str
    ddl: str
    cluster: str | None
    scope: FacadeScope | None
    local_identity: ServerIdentity
    table_identities: tuple[tuple[int, UUID], ...]
    if_exists: bool | None = None


@dataclass(frozen=True)
class VerifiedDropLayout:
    resources: tuple[VerifiedDropResource, ...]
    config: ChConfig


def verified_drop_resource_identity(resource: VerifiedDropResource) -> tuple[object, ...]:
    """Identify equivalent complete certificates without depending on SQL spelling."""
    identities = dict(resource.table_identities)
    scope = resource.scope
    scope_identity: tuple[object, ...]
    if scope is None:
        scope_identity = ("coordinator", resource.local_identity, identities.get(1))
    elif (
        len(scope.endpoints) == 1
        and scope.coordinator is not None
        and scope.identities[scope.coordinator] == resource.local_identity
    ):
        scope_identity = (
            "coordinator",
            resource.local_identity,
            identities.get(scope.coordinator),
        )
    else:
        # Include every certified endpoint, including an absent table there.
        # Equal present UUIDs alone cannot equate partially overlapping scopes.
        scope_identity = (
            "cluster",
            resource.local_identity,
            tuple(
                sorted(
                    (endpoint.native, scope.identities[position], identities.get(position))
                    for position, endpoint in enumerate(scope.endpoints, 1)
                )
            ),
        )
    return (
        *unquoted_table_name(resource.table),
        scope_identity,
        _physical_signature(resource.ddl, resource.table),
    )


def _config(connection_key: str) -> ChConfig:
    config = get_connection_config(connection_key)
    if not isinstance(config, ChConfig):
        message = "Verified ClickHouse drops require a ClickHouse connection."
        raise InvalidSqlInputError(message)
    return config


def require_drop_configuration(plan: VerifiedDropLayout) -> None:
    if get_connection_config(plan.config.connection_key) != plan.config:
        message = "ClickHouse configuration changed after drop ownership was verified."
        raise InvalidSqlInputError(message)


def coordinator_identity(connection: Any) -> ServerIdentity:
    rows = read_rows(connection, "SELECT serverUUID(), tcpPort()")
    message = "Cannot verify the coordinator identity before dropping ClickHouse resources."
    if len(rows) != 1:
        raise InvalidSqlInputError(message)
    try:
        return _identity(rows[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise InvalidSqlInputError(message) from exc


def require_drop_scope(connection: Any, resource: VerifiedDropResource) -> None:
    raise_if_cancelled()
    if resource.scope is None:
        matches = coordinator_identity(connection) == resource.local_identity
    else:
        matches = (
            inspect_facade_scope(connection, resource.scope.cluster, coordinator_required=False)
            == resource.scope
        )
    if not matches:
        message = (
            "ClickHouse endpoint membership or server identity changed after drop certification."
        )
        raise InvalidSqlInputError(message)


def require_drop_resource(connection: Any, resource: VerifiedDropResource) -> None:
    require_drop_scope(connection, resource)
    current = _resource(
        connection,
        resource.table,
        resource.scope.cluster if resource.scope else None,
        resource.role,
        allow_absence=True,
    )
    if (
        current is None
        or current.table_identities != resource.table_identities
        or _physical_signature(current.ddl, resource.table)
        != _physical_signature(resource.ddl, resource.table)
    ):
        message = "The selected ClickHouse table changed after drop ownership was verified."
        raise InvalidSqlInputError(message)


def drop_position_identity(resource: VerifiedDropResource, position: int) -> tuple[object, ...]:
    """Bind one submitted native table UUID independently of its cluster alias."""
    scope = resource.scope
    if scope is None:
        endpoint: tuple[object, ...] = ("coordinator", resource.local_identity)
    elif position == scope.coordinator:
        endpoint = ("coordinator", scope.identities[position])
    else:
        endpoint = (scope.endpoints[position - 1].native, scope.identities[position])
    return (
        *unquoted_table_name(resource.table),
        endpoint,
        dict(resource.table_identities)[position],
        _physical_signature(resource.ddl, resource.table),
    )


def unacknowledged_drop_positions(
    connection: Any, resource: VerifiedDropResource, acknowledged: set[tuple[object, ...]]
) -> tuple[int, ...]:
    """Recheck the full certificate, subtracting only this operation's acknowledged UUIDs."""
    require_drop_scope(connection, resource)
    current = _resource(
        connection,
        resource.table,
        resource.scope.cluster if resource.scope else None,
        resource.role,
        allow_absence=True,
    )
    expected = dict(resource.table_identities)
    present = dict(current.table_identities) if current is not None else {}
    changed = (
        current is not None
        and (
            current.scope != resource.scope
            or current.local_identity != resource.local_identity
            or _physical_signature(current.ddl, resource.table)
            != _physical_signature(resource.ddl, resource.table)
        )
    ) or any(expected.get(position) != identity for position, identity in present.items())
    unexplained_absence = any(
        position not in present and drop_position_identity(resource, position) not in acknowledged
        for position in expected
    )
    if changed or unexplained_absence:
        message = "The selected ClickHouse table changed after drop ownership was verified."
        raise InvalidSqlInputError(message)
    return tuple(
        position
        for position in expected
        if drop_position_identity(resource, position) not in acknowledged
    )


def remaining_drop_positions(connection: Any, resource: VerifiedDropResource) -> tuple[int, ...]:
    """Permit acknowledged removals while rejecting new or replaced table identities."""
    require_drop_scope(connection, resource)
    current = _resource(
        connection,
        resource.table,
        resource.scope.cluster if resource.scope else None,
        resource.role,
        allow_absence=True,
    )
    if current is None:
        return ()
    expected = dict(resource.table_identities)
    if (
        current.scope != resource.scope
        or current.local_identity != resource.local_identity
        or any(
            expected.get(position) != identity for position, identity in current.table_identities
        )
        or _physical_signature(current.ddl, resource.table)
        != _physical_signature(resource.ddl, resource.table)
    ):
        message = "The selected ClickHouse table changed after drop ownership was verified."
        raise InvalidSqlInputError(message)
    return tuple(position for position, _identity in current.table_identities)


def _qualified_table(connection: Any, table: str, database: str | None) -> str:
    parsed = parse_one(table, read=ClickHouseDialect, into=exp.Table)
    if not isinstance(parsed.this, exp.Identifier) or parsed.catalog:
        message = "ClickHouse drop requires one static database/table identifier."
        raise InvalidSqlInputError(message)
    if not parsed.db:
        if database is None:
            rows = read_rows(connection, "SELECT currentDatabase()")
            if len(rows) != 1 or len(rows[0]) != 1 or not rows[0][0]:
                message = "Cannot verify the current database for ClickHouse drop."
                raise InvalidSqlInputError(message)
            database = str(rows[0][0])
        parsed.set("db", exp.to_identifier(database, quoted=True))
    return parsed.sql(dialect=ClickHouseDialect)


def _local_definition(connection: Any, table: str) -> str | None:
    parsed = parse_one(table, read=ClickHouseDialect, into=exp.Table)
    statement = (
        exp.select("create_table_query")
        .from_("system.tables")
        .where(
            exp.column("database").eq(exp.Literal.string(parsed.db)),
            exp.column("name").eq(exp.Literal.string(parsed.name)),
        )
    )
    rows = read_rows(connection, statement.sql(dialect=ClickHouseDialect))
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        message = "Cannot verify the exact local ClickHouse table definition before drop."
        raise InvalidSqlInputError(message)
    return str(rows[0][0])


def _table_identities(
    connection: Any, table: str, scope: FacadeScope | None
) -> tuple[tuple[int, UUID], ...]:
    parsed = parse_one(table, read=ClickHouseDialect, into=exp.Table)
    source = (
        cluster_table(scope.cluster, "tables")
        if scope is not None
        else exp.to_table("system.tables")
    )
    query = exp.select("_shard_num", "uuid") if scope is not None else exp.select("uuid")
    query = query.from_(source).where(
        exp.column("database").eq(exp.Literal.string(parsed.db)),
        exp.column("name").eq(exp.Literal.string(parsed.name)),
    )
    rows = read_rows(connection, query.sql(dialect=ClickHouseDialect))
    message = (
        "Cannot certify stable ClickHouse table UUID ownership before drop; "
        "use tables with nonzero native UUIDs (for example an Atomic database)."
    )
    fields = 2 if scope is not None else 1
    if any(len(row) != fields for row in rows):
        raise InvalidSqlInputError(message)
    try:
        identities = {int(row[0]) if scope is not None else 1: UUID(str(row[-1])) for row in rows}
    except (TypeError, ValueError, IndexError) as exc:
        raise InvalidSqlInputError(message) from exc
    if len(identities) != len(rows) or any(not identity.int for identity in identities.values()):
        raise InvalidSqlInputError(message)
    expected = set(scope.identities) if scope is not None else {1}
    if not set(identities) <= expected:
        message = "Cannot verify complete selected ClickHouse table UUID coverage before drop."
        raise InvalidSqlInputError(message)
    return tuple(sorted(identities.items()))


def _resource(
    connection: Any, table: str, cluster: str | None, role: str, *, allow_absence: bool = False
) -> VerifiedDropResource | None:
    raise_if_cancelled()
    preflight_sql_statements(connection, [f"DROP TABLE IF EXISTS {table}{cluster_clause(cluster)}"])
    if cluster is None:
        table_ids = _table_identities(connection, table, None)
        if not table_ids:
            return None
        ddl = _local_definition(connection, table)
        if ddl is None:
            message = "The selected local ClickHouse table changed during drop certification."
            raise InvalidSqlInputError(message)
        return VerifiedDropResource(
            table, role, ddl, None, None, coordinator_identity(connection), table_ids
        )
    scope = inspect_facade_scope(connection, cluster, coordinator_required=False)
    table_ids = _table_identities(connection, table, scope)
    if not table_ids:
        return None
    definitions = occupied_definitions(connection, table, scope)
    if not definitions:
        message = "The selected ClickHouse table changed during drop certification."
        raise InvalidSqlInputError(message)
    expected = {position for position, _identity in table_ids}
    if set(definitions) != expected:
        message = "The selected ClickHouse table changed during drop certification."
        raise InvalidSqlInputError(message)
    if not allow_absence and expected != set(scope.identities):
        preflight_sql_statements(connection, [f"DROP TABLE {table}{cluster_clause(cluster)}"])
        message = "Dropping without IF EXISTS requires selected table readiness on every endpoint."
        raise ClickHouseClusterUnavailableError(message)
    ddl = definitions[min(definitions)]
    signature = _physical_signature(ddl, table)
    if any(_physical_signature(value, table) != signature for value in definitions.values()):
        message = "Selected ClickHouse table definitions differ across the drop scope."
        raise InvalidSqlInputError(message)
    return VerifiedDropResource(
        table, role, ddl, scope.cluster, scope, scope.local_identity, table_ids
    )


def _require_local_intent(
    resource: VerifiedDropResource, intent: DropIntent
) -> VerifiedDropResource:
    if intent.cluster is not None or resource.scope is None:
        return resource
    if len(resource.scope.endpoints) != 1 or resource.scope.coordinator is None:
        message = (
            "ch_cluster=None permits coordinator-local drops only; the selected verified "
            "resource is remote. Use a non-null cluster hint to authorize its actual scope."
        )
        raise InvalidSqlInputError(message)
    return replace(resource, cluster=None)


def _require_managed_relationship(
    connection: Any, facade: VerifiedDropResource, physical: VerifiedDropResource
) -> None:
    facade_create = parse_create_table(facade.ddl, facade.table)
    physical_create = parse_create_table(physical.ddl, physical.table)
    require_merge_tree(physical_create, physical.table)
    if _column_identity(facade_create) != _column_identity(physical_create):
        message = "The referenced physical table is not a verified owned companion."
        raise InvalidSqlInputError(message)
    if facade.scope is None:
        return
    definitions = occupied_definitions(connection, facade.table, facade.scope)
    parsed = {
        position: facade_definition(ddl, facade.table) for position, ddl in definitions.items()
    }
    names = _resolved_names(parsed, _scope_macros(connection, facade.scope, parsed))
    routing = _routing_endpoints(connection, facade.scope, set(names.values()))
    expected = (
        {endpoint.native for endpoint in physical.scope.endpoints}
        if physical.scope is not None
        else set()
    )
    if not expected or any(
        routing.get(position, {}).get(name) != expected for position, name in names.items()
    ):
        message = "The selected facades do not route to the verified physical drop scope."
        raise InvalidSqlInputError(message)


def _physical_resource(
    connection: Any, facade: VerifiedDropResource, intent: DropIntent
) -> VerifiedDropResource | None:
    if not intent.drop_shard:
        return None
    create = parse_create_table(facade.ddl, facade.table)
    reference = extract_clickhouse_distributed_shard_table(
        engine_sql(create), unquoted_table_name(facade.table)[0]
    )
    if reference is None:
        message = "Cannot verify the existing Distributed storage relationship before drop."
        raise InvalidSqlInputError(message)
    physical_table = exp.Table(
        this=exp.to_identifier(reference.table, quoted=True),
        db=exp.to_identifier(reference.database, quoted=True),
    ).sql(dialect=ClickHouseDialect)
    managed = unquoted_table_name(physical_table) == unquoted_table_name(
        intent.physical_table or build_ch_shard_table_name(facade.table)
    )
    if not managed:
        if not intent.drop_distributed:
            message = "External physical storage must be named explicitly to authorize its removal."
            raise InvalidSqlInputError(message)
        return None
    storage = resolve_required_cluster(connection, reference.cluster)
    physical = _resource(
        connection, physical_table, storage, "physical", allow_absence=intent.if_exists
    )
    if physical is None:
        if intent.if_exists:
            return None
        message = "Cannot verify complete selected physical table coverage before drop."
        raise InvalidSqlInputError(message)
    physical = _require_local_intent(physical, intent)
    _require_managed_relationship(connection, facade, physical)
    return physical


def certify_existing_drop(connection: Any, table: str, intent: DropIntent) -> VerifiedDropLayout:
    """Resolve existing roles without treating a suffix or a cluster hint as ownership."""
    if not intent.drop_shard and not intent.drop_distributed:
        message = "At least one of ch_drop_shard or ch_drop_distributed must be True."
        raise ValueError(message)
    config = _config(intent.connection_key)
    with internal_io():
        table = _qualified_table(connection, table, config.database)
        cluster = configured_cluster(config) if config.cluster_routing else None
        named = _resource(connection, table, cluster, "named", allow_absence=intent.if_exists)
        if named is None and cluster is None and intent.cluster is not None:
            hint = resolve_required_cluster(connection, intent.cluster)
            named = _resource(connection, table, hint, "named", allow_absence=intent.if_exists)
        if named is None:
            if intent.if_exists:
                return VerifiedDropLayout((), config)
            message = f"ClickHouse table {table} does not exist in the selected scope."
            raise InvalidSqlInputError(message)
        named = _require_local_intent(named, intent)
        create = parse_create_table(named.ddl, named.table)
        if engine_name(create).casefold() != "distributed":
            return VerifiedDropLayout((named,), config)
        facade = replace(named, role="facade")
        selected = [facade] if intent.drop_distributed else []
        physical = _physical_resource(connection, facade, intent)
        if physical is not None:
            selected.append(physical)
        return VerifiedDropLayout(tuple(selected), config)


def certify_explicit_drop(
    connection: Any,
    table: str,
    cluster: str | None,
    *,
    connection_key: str = "ch",
    if_exists: bool = True,
) -> VerifiedDropLayout:
    """Certify a named resource at one explicit role scope, without adding a local mirror."""
    config = _config(connection_key)
    with internal_io():
        table = _qualified_table(connection, table, config.database)
        cluster = resolve_optional_cluster(connection, cluster)
        resource = _resource(connection, table, cluster, "explicit", allow_absence=if_exists)
    if resource is None and not if_exists:
        message = f"ClickHouse table {table} does not exist in the selected scope."
        raise InvalidSqlInputError(message)
    return VerifiedDropLayout((resource,) if resource is not None else (), config)


__all__ = [
    "DropIntent",
    "VerifiedDropLayout",
    "VerifiedDropResource",
    "certify_existing_drop",
    "certify_explicit_drop",
    "coordinator_identity",
    "drop_position_identity",
    "remaining_drop_positions",
    "require_drop_configuration",
    "require_drop_resource",
    "require_drop_scope",
    "unacknowledged_drop_positions",
    "verified_drop_resource_identity",
]
