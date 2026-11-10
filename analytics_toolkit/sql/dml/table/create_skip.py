"""Whole-operation query-create existence checks and skip return shapes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult

if TYPE_CHECKING:
    from collections.abc import Callable

CONTINUE_CREATE = object()


def skip_existing_query_target(
    options: Any,
    open_connection: Callable[..., Any],
    table_exists: Callable[..., bool],
    close_connection: Callable[..., None],
    *,
    attempt: int = 1,
) -> object:
    connection = open_connection(options.target_key)
    try:
        exists = table_exists(
            options.target_backend,
            connection,
            options.target_table,
            options.target_key,
        )
    finally:
        close_connection(
            connection, connection_key=options.target_key, backend=options.target_backend
        )
    if not exists:
        return CONTINUE_CREATE
    if options.return_metadata:
        return SqlOperationResult(
            rows=None,
            metadata=SqlOperationMetadata(
                statement_count=0, query_label=options.query_label, retry_attempts=attempt
            ),
        )
    return None


__all__ = ["CONTINUE_CREATE", "skip_existing_query_target"]
