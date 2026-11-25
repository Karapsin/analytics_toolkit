"""Keep original replacement targets bound to the generations inspected before loading."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import (
    AmbiguousSqlReplaceError,
    InvalidSqlInputError,
)

from .drop_layout import ResourceInspection, inspect_resource, require_drop_resource
from .recovery_inventory import attach_recovery_details, retain_structural_recovery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .drop_layout import VerifiedDropResource


def capture_replacement_identity(
    connection: Any, targets: Sequence[tuple[str, str | None]]
) -> tuple[VerifiedDropResource, ...]:
    """Inspect existing resources without preflighting a DROP or adopting later lookups."""
    resources = []
    for table, cluster in targets:
        resource = inspect_resource(
            connection,
            table,
            cluster,
            "replacement target",
            ResourceInspection(sqls=("SELECT 1",)),
        )
        if resource is None:
            message = "Cannot verify the original ClickHouse replacement target identity."
            raise InvalidSqlInputError(message)
        resources.append(resource)
    return tuple(resources)


def require_replacement_identity(
    connection: Any,
    resources: Sequence[VerifiedDropResource],
    recovery_scopes: Sequence[tuple[str, str | None]],
) -> None:
    """A concurrent same-name generation is never authorized for a later cutover."""
    try:
        for resource in resources:
            require_drop_resource(connection, resource, sqls=("SELECT 1",))
    except Exception as original:
        retain_structural_recovery()
        message = (
            "The original ClickHouse target identity or generation changed during "
            "replacement; off-side resources were retained without cutting over."
        )
        error = AmbiguousSqlReplaceError(message)
        attach_recovery_details(error, scopes=recovery_scopes)
        raise error from original
