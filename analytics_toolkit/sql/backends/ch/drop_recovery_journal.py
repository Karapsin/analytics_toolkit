"""Retain exact recovery certificates when a local DROP acknowledgement is lost."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.execution.backend_options import current_backend_operation

from .drop_layout import drop_position_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from .drop_layout import VerifiedDropLayout, VerifiedDropResource
    from .recovery_endpoints import RecoveryEndpoint


def execute_recovery_drop(
    plan: VerifiedDropLayout,
    resource: VerifiedDropResource,
    endpoint: RecoveryEndpoint | None,
    sql: str,
    execute: Callable[[], None],
) -> None:
    """Guard raw recovery clients independently of transport observation wrappers."""
    journal: dict[str, Any] = {
        "backend": "ch",
        "connection_key": plan.config.connection_key,
        "table": resource.table,
        "role": resource.role,
        "endpoint": endpoint.identity if endpoint is not None else None,
        "server_identity": resource.local_identity,
        "certificate": drop_position_identity(resource, 1),
        "sql": sql,
        "submitted": False,
        "acknowledged": False,
        "recovery_retained": False,
    }
    state = current_backend_operation()
    if state is not None:
        with state.lock:
            state.resources.setdefault("clickhouse_drop_recovery_journal", []).append(journal)
    journal["submitted"] = True
    try:
        execute()
    except BaseException as original:
        journal["recovery_retained"] = True
        if state is not None:
            state.preserve_recovery = True
            state.structural_replay_blocked = True
        if not isinstance(original, Exception):
            raise
        endpoint_description = journal["endpoint"] or (
            f"verified server {resource.local_identity[0]}:{resource.local_identity[1]}"
        )
        message = (
            "ClickHouse recovery DROP acknowledgement is uncertain; structural replay "
            "stopped and recovery resources were retained. "
            f"Resource={resource.table}, endpoint={endpoint_description}, "
            f"connection={plan.config.connection_key}."
        )
        if isinstance(original, AmbiguousSqlReplaceError):
            original.args = (message,)
            cast("Any", original).analytics_toolkit_sql_drop_journal = journal
            raise
        error = AmbiguousSqlReplaceError(message)
        cast("Any", error).analytics_toolkit_sql_drop_journal = journal
        raise error from original
    journal["acknowledged"] = True


__all__ = ["execute_recovery_drop"]
