"""Conservative recovery decisions for query-created target resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from analytics_toolkit.sql.backends import creation_resources
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from analytics_toolkit.sql.execution.backend_options import preserve_recovery_artifacts

if TYPE_CHECKING:
    from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource
    from analytics_toolkit.sql.ddl.ownership import CreationOwnership


def query_cleanup_resources(
    ownership: CreationOwnership, backend: str
) -> list[str | CreatedTableResource]:
    """Use generation-bound acknowledgements where native identity is available."""
    return creation_resources.query_cleanup_resources(ownership, backend)


def can_cleanup_created_target(ownership: CreationOwnership, error: Exception) -> bool:
    if preserve_recovery_artifacts() or not ownership.resources:
        return False
    return ownership.pending_resource is None or is_non_retryable_sql_error(error)


def record_target_recovery(ownership: CreationOwnership, error: Exception, target: str) -> None:
    error.__dict__["analytics_toolkit_sql_retry_safe"] = False
    error.__dict__["analytics_toolkit_sql_acknowledged_resources"] = tuple(ownership.resources)
    error.__dict__["analytics_toolkit_sql_acknowledged_creates"] = tuple(
        ownership.acknowledged_creates
    )
    error.__dict__["analytics_toolkit_sql_pending_create"] = ownership.pending_resource or target


def require_query_target_available(*, existed: bool, target: str, replace: bool) -> None:
    if existed and not replace:
        message = f"Target table {target} already exists."
        raise InvalidSqlInputError(message)


def require_created_target_acknowledgement(ownership: CreationOwnership, *, replace: bool) -> None:
    if not ownership.resources and not replace:
        message = "Target creation returned without an ownership acknowledgement."
        raise RuntimeError(message)
