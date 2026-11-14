"""Run one complete frozen payload through destination staging and publication."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.gp.native_assignment import native_assignment_errors
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferConnectionRefs
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error

from .client_validation import require_client_write_certificate
from .native_assignment import (
    certify_loaded_assignment,
    complete_assignment_publication,
    native_assignment_pending,
    restore_assignment_checkpoint,
)

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )

    from .adaptive_input import AdaptiveTransferContext


def run_resolved_checkpoint_transfer(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    state: TransferStageState,
    context: AdaptiveTransferContext,
) -> int:
    # Resolve historical orchestration seams at call time for compatibility.
    from . import adaptive_input as adaptive  # noqa: PLC0415 -- avoid the public transfer cycle.
    from .attempt import load_stage_batches  # noqa: PLC0415 -- orchestration import cycle.
    from .finalize import finalize_loaded_stage  # noqa: PLC0415 -- orchestration import cycle.
    from .row_counts import validate_loaded_stage_row_count  # noqa: PLC0415
    from .stage import ensure_transfer_target_table  # noqa: PLC0415

    frozen_options = replace(options, transfer_slices=None, table_schema=state.stage_column_types)
    target_runner = context.target_connection_runner or (
        lambda role, operation: adaptive.run_with_fresh_connection(
            options.to_db_key,
            role,
            operation,
            open_connection=context.open_connection or adaptive.get_sql_connection,
        )
    )

    def runner(role: str, operation: Any) -> Any:
        adaptive.require_frozen_configuration(options)

        def checked(ref: dict[str, Any]) -> Any:
            adaptive.freeze_destination_timezone(
                options.input_resolution, options.to_db_backend, ref["connection"]
            )
            return operation(ref)

        return target_runner(role, checked)

    options.input_resolution["target_connection_runner"] = runner
    try:
        if "client_write_plan" in options.input_resolution:
            runner(
                "certify_frozen_client_writer",
                lambda ref: require_client_write_certificate(options, ref["connection"], state),
            )
        pending_native = native_assignment_pending(options)
        total_rows = (
            restore_assignment_checkpoint(options, state, runner) if pending_native else None
        )
        if not pending_native:
            runner(
                "prepare_frozen_target",
                lambda target_ref: ensure_transfer_target_table(
                    frozen_options,
                    TransferConnectionRefs(source=refs.source, target=target_ref),
                    state,
                    state.source_columns,
                ),
            )
        if total_rows is None:
            with (
                native_assignment_errors(
                    "population", non_retryable_predicate=is_non_retryable_sql_error
                )
                if pending_native
                else nullcontext()
            ):
                total_rows = load_stage_batches(
                    frozen_options,
                    refs,
                    state,
                    context.read_retry_cnt,
                    context.insert_retry_cnt,
                )
            validate_loaded_stage_row_count(
                options=frozen_options,
                connection_refs=refs,
                stage_state=state,
                total_rows=total_rows,
                open_connection=context.open_connection or adaptive.get_sql_connection,
                target_connection_runner=runner,
            )
        if pending_native:
            certify_loaded_assignment(options, state, total_rows, runner)
        if pending_native and total_rows:
            frozen_options = replace(frozen_options, table_schema=state.stage_column_types)
            runner(
                "prepare_certified_native_target",
                lambda target_ref: ensure_transfer_target_table(
                    frozen_options,
                    TransferConnectionRefs(source=refs.source, target=target_ref),
                    state,
                    state.source_columns,
                ),
            )
        finalize_loaded_stage(
            frozen_options,
            refs,
            state,
            total_rows,
            target_connection_runner=runner,
            target_host_connection_runner=context.target_host_connection_runner,
        )
        if pending_native:
            complete_assignment_publication(options)
        object.__setattr__(options, "row_count_result", frozen_options.row_count_result)
        object.__setattr__(options, "final_target_rows", frozen_options.final_target_rows)
        object.__setattr__(
            options,
            "transfer_concurrency",
            replace(options.transfer_concurrency, effective_read=1, effective_write=1),
        )
        return total_rows
    finally:
        options.input_resolution.pop("target_connection_runner", None)
