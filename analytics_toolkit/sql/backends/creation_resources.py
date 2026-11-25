"""Backend creation identity capabilities and acknowledgement dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.ddl.creation_resources import (
    CreatedResourceChangedError,
    CreatedTableResource,
    record_created_table,
    resource_retention_message,
    scoped_creation_count,
    skipped_creation_count,
    submitted_creation_resource,
)

if TYPE_CHECKING:
    from analytics_toolkit.sql.core.identifiers import TableIdentifier
    from analytics_toolkit.sql.ddl.ownership import CreationOwnership


def creation_identity_key(adapter: Any, identity: TableIdentifier) -> tuple[str, ...]:
    """Match each backend's native identifier casing for repeated CREATEs."""
    return tuple(
        part if quoted or adapter.backend == "ch" else part.lower()
        for part, quoted in zip(identity.parts, identity.quoted)
    )


def query_cleanup_resources(
    ownership: CreationOwnership, backend: str
) -> list[str | CreatedTableResource]:
    """Use generation-bound acknowledgements for native identity backends."""
    if backend in {"gp", "ch"}:
        return list(ownership.scoped_resources)
    return list(ownership.resources)


def supports_created_resource_identity(adapter: Any) -> bool:
    return adapter.backend in {"gp", "ch"}


def prepare_coordinator_mirror(adapter: Any, connection: Any, sql: str) -> tuple[str, bool]:
    if adapter.backend != "ch":
        return sql, False
    from .ch.created_resources import prepare_coordinator_mirror as prepare_mirror  # noqa: PLC0415

    return prepare_mirror(connection, sql)


def require_created_resource(adapter: Any, connection: Any, resource: CreatedTableResource) -> None:
    if resource.backend != adapter.backend:
        raise CreatedResourceChangedError(resource_retention_message(resource))
    if resource.backend == "gp":
        from .gp.creation_identity import (  # noqa: PLC0415 -- registry-safe backend dispatch.
            require_created_resource as require_native,
        )
    elif resource.backend == "ch":
        from .ch.created_resources import (  # noqa: PLC0415 -- registry-safe backend dispatch.
            require_created_resource as require_native,
        )
    else:
        raise CreatedResourceChangedError(resource_retention_message(resource))
    require_native(connection, resource)


def execute_scoped_create(
    adapter: Any, connection: Any, sql: str, resource: CreatedTableResource
) -> None:
    if adapter.backend == "gp":
        from .gp.creation_identity import execute_created_table  # noqa: PLC0415

        record_created_table(execute_created_table(adapter, connection, sql, resource))
        return
    if adapter.backend == "ch":
        from .ch.created_resources import (  # noqa: PLC0415
            capture_created_resource,
            prepare_created_table,
        )

        sql, resource = prepare_created_table(connection, sql, resource)
        before = scoped_creation_count()
        skipped_before = skipped_creation_count()
        with submitted_creation_resource(resource):
            adapter.execute_command(connection, sql)
        if scoped_creation_count() == before and skipped_creation_count() == skipped_before:
            record_created_table(capture_created_resource(connection, resource))
        return
    adapter.execute_command(connection, sql)
    record_created_table(resource)
