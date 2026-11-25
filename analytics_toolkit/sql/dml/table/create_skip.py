"""Whole-operation query-create existence checks and skip return shapes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult

from .query_namespace import needs_setup_namespace

if TYPE_CHECKING:
    from collections.abc import Callable

CONTINUE_CREATE = object()


def _target_exists(options: Any, connection: Any, table_exists: Callable[..., bool]) -> bool:
    policy = getattr(options, "ch_creation_policy", None)
    if options.target_backend == "ch" and policy is not None:
        from analytics_toolkit.sql.backends import get_backend_adapter  # noqa: PLC0415

        adapter = get_backend_adapter(options.target_backend)
        return bool(
            adapter.execute_create_target_exists(
                connection,
                SimpleNamespace(
                    table_name=options.target_table,
                    ch_creation_policy=policy,
                    ch_only_shard=options.ch_only_shard,
                    connection_key=options.target_key,
                ),
            )
        )
    return table_exists(
        options.target_backend, connection, options.target_table, options.target_key
    )


def skip_existing_query_target(  # noqa: PLR0913 -- preserve callbacks and optional borrowed session.
    options: Any,
    open_connection: Callable[..., Any],
    table_exists: Callable[..., bool],
    close_connection: Callable[..., None],
    *,
    attempt: int = 1,
    connection: Any = None,
) -> object:
    if connection is None and needs_setup_namespace(options):
        return CONTINUE_CREATE
    owns_connection = connection is None
    connection = open_connection(options.target_key) if owns_connection else connection
    try:
        exists = _target_exists(options, connection, table_exists)
    finally:
        if owns_connection:
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
