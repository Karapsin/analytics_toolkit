"""Publish certified Python query values with the configured writer policy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.dml.destination_context import freeze_destination_timezone
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError
from analytics_toolkit.sql.dml.query_client_validation import guarded_query_retry
from analytics_toolkit.sql.dml.transfer.flow.adaptive_input import (
    require_frozen_configuration,
    resolved_source_batches,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    is_non_retryable_sql_error,
    replace_connection,
    rollback_quietly,
    run_with_retry,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.dml.query_input import PreparedQueryInput
    from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions


def _retain_deterministic_cause(error: AmbiguousTableLoadError) -> None:
    """Ambiguous acknowledgement does not make a deterministic failure transient."""
    cause = error.__cause__
    while isinstance(cause, AmbiguousTableLoadError):
        cause = cause.__cause__
    if isinstance(cause, Exception) and is_non_retryable_sql_error(cause):
        error.__dict__["analytics_toolkit_sql_retry_safe"] = False


def _query_replay_retry(options: TransferOptions) -> Callable[..., Any]:
    guarded_retry = guarded_query_retry(options, run_with_retry)

    def retry(**kwargs: Any) -> Any:
        operation = kwargs["operation"]

        def classified_operation(attempt: int) -> Any:
            try:
                return operation(attempt)
            except AmbiguousTableLoadError as exc:
                _retain_deterministic_cause(exc)
                raise

        return guarded_retry(
            **{
                **kwargs,
                "operation": classified_operation,
                "safe_exception_logging": True,
            }
        )

    return retry


def replay_query_batches(
    prepared: PreparedQueryInput,
    connection_ref: dict[str, Any],
    *,
    insert_fn: Callable[..., int],
) -> int:
    """Replay the same certified rows; retry callbacks follow backend policy."""
    options = prepared.options
    retry = _query_replay_retry(options)
    total = 0
    for batch in resolved_source_batches(options, iter):
        require_frozen_configuration(options)
        freeze_destination_timezone(
            options.input_resolution,
            options.to_db_backend,
            connection_ref["connection"],
            prepared.schema,
        )
        total += insert_fn(
            options.to_db_backend,
            connection_ref,
            options.target_table,
            batch.columns,
            batch.rows,
            retry_fn=retry,
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            target_column_types=prepared.schema,
            trino_insert_chunk_size=options.trino_insert_chunk_size,
            gp_insert_chunk_size=options.gp_insert_chunk_size,
            connection_key=options.to_db_key,
            query_label=options.query_label,
            rollback_fn=rollback_quietly,
            replace_connection_fn=replace_connection,
            safe_exception_logging=True,
        )
    return total
