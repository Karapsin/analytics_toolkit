from __future__ import annotations

from dataclasses import replace
from typing import Any

from ....backend_adapters import get_backend_adapter
from ....dml.table._basic_ops import count_table_rows
from ....connection.errors import sql_preview
from analytics_toolkit.general import time_print
from ..runtime.models import (
    TransferConnectionRefs,
    TransferOptions,
    TransferRowCountResult,
    TransferSliceRowCount,
    TransferStageState,
)
from ..runtime.retry import (
    replace_connection,
    rollback_quietly,
    run_with_fresh_connection,
    run_with_retry,
)
from .logging import format_transfer_key_log_fragment


class TransferRowCountMismatchError(ValueError):
    pass


def count_source_rows(
    backend: str,
    connection: Any,
    source_sql: str,
    *,
    query_label: str | None = None,
) -> int:
    return get_backend_adapter(backend).count_source_rows(
        connection,
        source_sql,
        query_label=query_label,
    )


def source_sql_for_count_limited_read(
    *,
    backend: str,
    source_sql: str,
    expected_rows: int | None,
    enabled: bool,
) -> str:
    return get_backend_adapter(backend).source_sql_for_count_limited_read(
        source_sql=source_sql,
        expected_rows=expected_rows,
        enabled=enabled,
    )


def disable_query_limit_for_transfer_reads(backend: str) -> bool:
    return get_backend_adapter(backend).disable_query_limit_for_transfer_reads()


def prepare_row_count_validated_options(
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    slice_index: int | None = None,
    transfer_key_label: str | None = None,
) -> TransferOptions:
    if not options.validate_row_count:
        return options

    expected_rows = _count_source_rows_with_retry(
        options,
        connection_refs.source,
        options.source_sql,
    )
    stage_state.expected_source_rows = (
        expected_rows
        if stage_state.expected_source_rows is None
        else stage_state.expected_source_rows + expected_rows
    )
    stage_state.current_expected_source_rows = expected_rows
    _log_expected_rows(options, expected_rows, slice_index, transfer_key_label)

    source_sql = source_sql_for_count_limited_read(
        backend=options.from_db_backend,
        source_sql=options.source_sql,
        expected_rows=expected_rows,
        enabled=options.ch_count_limit_read,
    )
    return replace(options, source_sql=source_sql)


def validate_slice_row_count(
    *,
    options: TransferOptions,
    stage_state: TransferStageState,
    slice_index: int,
    transfer_key_label: str | None,
    streamed_rows: int,
) -> None:
    if not options.validate_row_count:
        return
    expected_rows = stage_state.current_expected_source_rows
    if expected_rows is None:
        raise RuntimeError("Expected slice source row count to be initialized.")

    row_count = TransferSliceRowCount(
        index=slice_index,
        label=transfer_key_label,
        expected_rows=expected_rows,
        streamed_rows=streamed_rows,
    )
    stage_state.slice_counts.append(row_count)
    if expected_rows != streamed_rows:
        raise TransferRowCountMismatchError(
            _format_row_count_mismatch(
                options=options,
                expected_rows=expected_rows,
                streamed_rows=streamed_rows,
                stage_rows=None,
                stage_table=stage_state.stage_table,
                slice_index=slice_index,
                transfer_key_label=transfer_key_label,
            )
        )


def validate_streamed_row_count(
    *,
    options: TransferOptions,
    stage_state: TransferStageState,
    total_rows: int,
) -> None:
    if not options.validate_row_count:
        return
    expected_rows = _sum_worker_expected_rows(stage_state)
    stage_state.expected_source_rows = expected_rows
    stage_state.streamed_rows = total_rows
    stage_state.slice_counts = _collect_worker_slice_counts(stage_state)
    if expected_rows != total_rows:
        raise TransferRowCountMismatchError(
            _format_row_count_mismatch(
                options=options,
                expected_rows=expected_rows,
                streamed_rows=total_rows,
                stage_rows=None,
                stage_table=stage_state.stage_table,
            )
        )


def validate_loaded_stage_row_count(
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    total_rows: int,
    open_connection: Any,
) -> None:
    del connection_refs
    if not options.validate_row_count:
        return

    expected_rows = stage_state.expected_source_rows
    if expected_rows is None:
        raise RuntimeError("Expected source row count to be initialized.")

    stage_state.streamed_rows = total_rows
    if expected_rows != total_rows:
        raise TransferRowCountMismatchError(
            _format_row_count_mismatch(
                options=options,
                expected_rows=expected_rows,
                streamed_rows=total_rows,
                stage_rows=None,
                stage_table=stage_state.stage_table,
            )
        )

    stage_rows = _count_loaded_stage_rows(
        options,
        stage_state,
        total_rows,
        open_connection=open_connection,
    )
    stage_state.stage_rows = stage_rows
    if stage_rows != expected_rows:
        raise TransferRowCountMismatchError(
            _format_row_count_mismatch(
                options=options,
                expected_rows=expected_rows,
                streamed_rows=total_rows,
                stage_rows=stage_rows,
                stage_table=stage_state.stage_table,
            )
        )

    stage_state.row_count_validated = True
    object.__setattr__(
        options,
        "row_count_result",
        TransferRowCountResult(
            expected_source_rows=expected_rows,
            streamed_rows=total_rows,
            stage_rows=stage_rows,
            row_count_validated=True,
            slice_counts=list(stage_state.slice_counts),
        ),
    )


def _count_loaded_stage_rows(
    options: TransferOptions,
    stage_state: TransferStageState,
    total_rows: int,
    *,
    open_connection: Any,
) -> int:
    if total_rows == 0:
        return 0

    stage_table = stage_state.stage_table
    if stage_table is None:
        raise RuntimeError("Expected stage table to be initialized.")
    stage_tables = (
        stage_state.stage_tables
        if options.write_mode == "upsert" and stage_state.stage_tables is not None
        else [stage_table]
    )
    return run_with_fresh_connection(
        options.to_db_key,
        "validate_stage_row_count",
        lambda target_ref: sum(
            count_table_rows(
                options.to_db_backend,
                target_ref["connection"],
                current_stage_table,
                query_label=options.query_label,
            )
            for current_stage_table in stage_tables
        ),
        open_connection=open_connection,
    )


def _count_source_rows_with_retry(
    options: TransferOptions,
    source_ref: dict[str, Any],
    source_sql: str,
) -> int:
    def operation(attempt: int) -> int:
        del attempt
        try:
            return count_source_rows(
                options.from_db_backend,
                source_ref["connection"],
                source_sql,
                query_label=options.query_label,
            )
        except Exception:
            rollback_quietly(source_ref["connection"])
            replace_connection(options.from_db_key, source_ref)
            raise

    return run_with_retry(
        operation_name=(
            f"counting transfer source rows on {options.from_db_key} "
            f"({options.from_db_backend})"
        ),
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        operation=operation,
    )


def _sum_worker_expected_rows(stage_state: TransferStageState) -> int:
    worker_states = getattr(stage_state, "worker_stage_states", None)
    if worker_states is None:
        raise RuntimeError("Expected worker stage states to be initialized.")
    return sum(
        worker_state.stage_state.expected_source_rows or 0
        for worker_state in worker_states
    )


def _collect_worker_slice_counts(
    stage_state: TransferStageState,
) -> list[TransferSliceRowCount]:
    worker_states = getattr(stage_state, "worker_stage_states", None)
    if worker_states is None:
        return []
    slice_counts: list[TransferSliceRowCount] = []
    for worker_state in worker_states:
        slice_counts.extend(worker_state.stage_state.slice_counts)
    return slice_counts


def _log_expected_rows(
    options: TransferOptions,
    expected_rows: int,
    slice_index: int | None,
    transfer_key_label: str | None,
) -> None:
    del slice_index
    if transfer_key_label is None:
        message = f"Expecting {_format_row_count(expected_rows)} source row(s)"
    else:
        key_fragment = format_transfer_key_log_fragment(transfer_key_label).strip()
        key_suffix = f" for {key_fragment}" if key_fragment else ""
        message = (
            f"Expecting {_format_row_count(expected_rows)} source row(s){key_suffix}"
        )
    time_print(
        message,
        connection=options.from_db_key,
        backend=options.from_db_backend,
    )


def _format_row_count_mismatch(
    *,
    options: TransferOptions,
    expected_rows: int,
    streamed_rows: int,
    stage_rows: int | None,
    stage_table: str | None,
    slice_index: int | None = None,
    transfer_key_label: str | None = None,
) -> str:
    parts = [
        "Transfer row count validation failed",
        f"expected_source_rows={expected_rows}",
        f"streamed_rows={streamed_rows}",
    ]
    if stage_rows is not None:
        parts.append(f"stage_rows={stage_rows}")
    if stage_table is not None:
        parts.append(f"stage_table={stage_table}")
    if slice_index is not None:
        parts.append(f"slice_index={slice_index}")
    if transfer_key_label:
        parts.append(f"slice={transfer_key_label}")
    parts.append(f"source_sql={sql_preview(options.source_sql, max_chars=500)}")
    return "; ".join(parts)


def _format_row_count(value: Any) -> str:
    try:
        return f"{int(value):_}"
    except (TypeError, ValueError):
        return str(value)
