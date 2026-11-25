"""Replay data population without repeating acknowledged structural creation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends import creation_resources, get_backend_adapter
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.transfer.runtime.retry import close_connection_ref, run_with_retry
from analytics_toolkit.sql.execution.backend_options import clear_data_replay_retention

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.execution.plans import SqlOperationMetadata


def populate_acknowledged_target(
    connection_ref: dict[str, Any],
    options: dict[str, Any],
    metadata: SqlOperationMetadata,
    operation: Callable[[], None],
    open_connection: Callable[[str], Any],
) -> None:
    """Retry only the data statement, checking the same generations on reuse."""
    retry = options.get("input_resolution", {}).get("native_population_retry")
    if retry is None:
        operation()
        return
    ownership, retry_cnt, timeout_increment = retry
    previous_error: Exception | None = None

    def populate(attempt: int) -> None:
        nonlocal previous_error
        if attempt > 1:
            close_connection_ref(connection_ref, options["target_key"], "population_retry")
            connection_ref["connection"] = open_connection(options["target_key"])
            if not ownership.scoped_resources:
                message = (
                    f"Retaining created target {options['target_table']}: "
                    "no native creation identity is available for population replay."
                )
                error = InvalidSqlInputError(message)
                error.__dict__["analytics_toolkit_sql_retry_safe"] = False
                raise error from previous_error
            for resource in ownership.scoped_resources:
                creation_resources.require_created_resource(
                    get_backend_adapter("ch"), connection_ref["connection"], resource
                )
            metadata.data_write_replay_possible = True
        try:
            operation()
        except Exception as error:
            previous_error = error
            raise

    run_with_retry(
        "ClickHouse query population",
        retry_cnt,
        timeout_increment,
        populate,
        safe_exception_logging=True,
    )
    clear_data_replay_retention()
