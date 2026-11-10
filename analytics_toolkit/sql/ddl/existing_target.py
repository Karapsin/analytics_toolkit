"""Metadata-only schema creation skips using the standard connection retry scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import SqlOperationContext
from analytics_toolkit.sql.execution.operation_runner import run_connection_operation
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult

if TYPE_CHECKING:
    from collections.abc import Callable

CONTINUE_CREATE = object()


@dataclass(frozen=True)
class ExistingCreateTarget:
    config: Any
    table_name: str
    return_metadata: bool
    query_label: str | None
    retry_cnt: int
    timeout_increment: float


def skip_existing_schema_target(
    options: ExistingCreateTarget,
    open_connection: Callable[[str], Any],
) -> object:
    # The table facade imports the DDL facade during package initialization.
    from analytics_toolkit.sql.dml.table._basic_ops import table_exists  # noqa: PLC0415

    config = options.config
    exists = run_connection_operation(
        operation_name=f"checking existing target {options.table_name}",
        connection_key=config.connection_key,
        backend=config.backend,
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        open_connection=open_connection,
        operation=lambda ref, _attempt: table_exists(
            config.backend,
            ref["connection"],
            options.table_name,
            config.connection_key,
        ),
        context_factory=lambda attempt: SqlOperationContext(
            operation="create_table",
            alias=config.connection_key,
            backend=config.backend,
            phase="check_target",
            target_table=options.table_name,
            retry_attempt=attempt,
        ),
    )
    if not exists:
        return CONTINUE_CREATE
    if options.return_metadata:
        return SqlOperationResult(
            rows=None,
            metadata=SqlOperationMetadata(statement_count=0, query_label=options.query_label),
        )
    return None


__all__ = ["CONTINUE_CREATE", "ExistingCreateTarget", "skip_existing_schema_target"]
