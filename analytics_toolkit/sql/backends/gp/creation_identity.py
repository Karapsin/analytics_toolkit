"""Bind Greenplum CREATE and cleanup to the relation created in a transaction."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from analytics_toolkit.sql.backends.execution_outcome import close_cursor
from analytics_toolkit.sql.ddl.creation_resources import (
    CreatedResourceChangedError,
    CreatedTableResource,
    require_resource_configuration,
    resource_retention_message,
)
from analytics_toolkit.sql.execution.labels import apply_query_label

_IDENTITY_SQL = (
    "SELECT current_database(), inet_server_addr()::text, inet_server_port(), "
    "c.oid::bigint, c.relfilenode::bigint, c.relnamespace::bigint "
    "FROM pg_catalog.pg_class c WHERE c.oid = pg_catalog.to_regclass(%s)"
)
_IDENTITY_FIELD_COUNT = 6


@dataclass(frozen=True, repr=False)
class RelationIdentity:
    database: str
    server_address: str | None
    server_port: int | None
    relation_oid: int
    relation_file: int
    namespace_oid: int


def _same_generation(left: RelationIdentity | None, right: RelationIdentity) -> bool:
    # TRUNCATE/rewrite can replace a relation's storage file without replacing
    # the relation. The catalog object OID is the table generation identifier.
    return left is not None and (
        left.database,
        left.server_address,
        left.server_port,
        left.relation_oid,
        left.namespace_oid,
    ) == (
        right.database,
        right.server_address,
        right.server_port,
        right.relation_oid,
        right.namespace_oid,
    )


def _identity(cursor: Any, resource: CreatedTableResource) -> RelationIdentity | None:
    cursor.execute(apply_query_label(_IDENTITY_SQL, None), (resource.name,))
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != _IDENTITY_FIELD_COUNT or int(rows[0][3]) <= 0:
        message = "The created Greenplum relation has no usable native identity."
        raise CreatedResourceChangedError(message)
    database, address, port, relation_oid, relation_file, namespace_oid = rows[0]
    return RelationIdentity(
        str(database),
        None if address is None else str(address),
        None if port is None else int(port),
        int(relation_oid),
        int(relation_file),
        int(namespace_oid),
    )


def _required_identity(cursor: Any, resource: CreatedTableResource) -> RelationIdentity:
    identity = _identity(cursor, resource)
    if identity is None:
        raise CreatedResourceChangedError(resource_retention_message(resource))
    return identity


def _require_same_generation(cursor: Any, resource: CreatedTableResource) -> None:
    if not _same_generation(_identity(cursor, resource), resource.certificate):
        raise CreatedResourceChangedError(resource_retention_message(resource))


def execute_created_table(
    adapter: Any, connection: Any, sql: str, resource: CreatedTableResource
) -> CreatedTableResource:
    """Read the new OID before CREATE's transaction releases its DDL lock."""
    if getattr(connection, "autocommit", False):
        message = "Owned Greenplum creation requires a transactional connection."
        raise CreatedResourceChangedError(message)
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        identity = _required_identity(cursor, resource)
        if adapter.commit_commands:
            connection.commit()
        return replace(resource, certificate=identity)
    except BaseException:
        with suppress(Exception):
            connection.rollback()
        raise
    finally:
        close_cursor(cursor, backend="gp")


def drop_created_resource(adapter: Any, connection: Any, resource: CreatedTableResource) -> None:
    """Lock the current name before comparing generation and issuing DROP."""
    from analytics_toolkit.sql.core.identifiers import TableIdentifier  # noqa: PLC0415

    require_resource_configuration(resource)
    if not isinstance(resource.certificate, RelationIdentity) or resource.scope.kind != "local":
        raise CreatedResourceChangedError(resource_retention_message(resource))
    if getattr(connection, "autocommit", False):
        raise CreatedResourceChangedError(resource_retention_message(resource))
    identifier = TableIdentifier.parse(resource.name, "gp")
    table = identifier.render("gp")
    cursor = connection.cursor()
    try:
        if _identity(cursor, resource) is None:
            if adapter.commit_commands:
                connection.commit()
            return
        cursor.execute(apply_query_label(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE", None))
        _require_same_generation(cursor, resource)
        cursor.execute(apply_query_label(f"DROP TABLE {table}", None))
        if adapter.commit_commands:
            connection.commit()
    except BaseException:
        with suppress(Exception):
            connection.rollback()
        raise
    finally:
        close_cursor(cursor, backend="gp")


def require_created_resource(connection: Any, resource: CreatedTableResource) -> None:
    """Check a creation receipt without issuing or preflighting a DROP."""
    require_resource_configuration(resource)
    if not isinstance(resource.certificate, RelationIdentity) or resource.scope.kind != "local":
        raise CreatedResourceChangedError(resource_retention_message(resource))
    cursor = connection.cursor()
    try:
        _require_same_generation(cursor, resource)
    finally:
        close_cursor(cursor, backend="gp")
