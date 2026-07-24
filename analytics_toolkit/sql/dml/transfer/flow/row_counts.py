from __future__ import annotations

# ruff: noqa: BLE001, S110, SIM105

import uuid
from dataclasses import replace
from typing import Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backend_adapters import get_backend_adapter
from analytics_toolkit.sql.connection.errors import sql_preview
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.dml.load.stage import build_stage_table_name
from analytics_toolkit.sql.dml.table._basic_ops import count_table_rows
from analytics_toolkit.sql.dml.transfer.flow.logging import (
    format_transfer_key_log_fragment,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferOptions,
    TransferRowCountResult,
    TransferSliceRowCount,
    TransferStageState,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    close_connection_ref,
    replace_connection,
    rollback_quietly,
    run_with_fresh_connection,
    run_with_retry,
)


class TransferRowCountMismatchError(ValueError):
    pass


def best_effort_transfer_target_count(
    options: TransferOptions,
    *,
    open_connection: Any = get_sql_connection,
    count_rows: Any = count_table_rows,
) -> int | None:
    connection = None
    try:
        connection = open_connection(options.to_db_key)
        return int(
            count_rows(
                options.to_db_backend,
                connection,
                options.target_table,
                query_label=options.query_label,
            )
        )
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
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

    source_sql = options.source_sql
    if options.source_transfer_staging_schema is not None:
        source_sql = _materialize_source_with_retry(
            options,
            connection_refs.source,
            stage_state,
        )
        expected_rows = _count_materialized_source_rows_with_retry(
            options,
            connection_refs.source,
            stage_state.source_stage_tables[-1],
        )
    else:
        expected_rows = _count_source_rows_with_retry(
            options,
            connection_refs.source,
            source_sql,
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
        source_sql=source_sql,
        expected_rows=expected_rows,
        enabled=options.ch_count_limit_read,
    )
    return replace(options, source_sql=source_sql)


def cleanup_materialized_sources(
    options: TransferOptions,
    connection_ref: dict[str, Any],
    stage_state: TransferStageState,
) -> None:
    adapter = get_backend_adapter(options.from_db_backend)
    while stage_state.source_stage_tables:
        stage_table = stage_state.source_stage_tables[-1]

        def operation(attempt: int, stage_table: str = stage_table) -> None:
            del attempt
            try:
                adapter.drop_table(
                    connection_ref["connection"],
                    stage_table,
                    if_exists=True,
                    query_label=options.query_label,
                )
            except Exception:
                rollback_quietly(connection_ref["connection"])
                replace_connection(options.from_db_key, connection_ref)
                raise

        run_with_retry(
            operation_name=(
                f"dropping materialized transfer source on {options.from_db_key} "
                f"({options.from_db_backend})"
            ),
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=operation,
        )
        stage_state.source_stage_tables.pop()


def cleanup_sources_and_close(
    options: TransferOptions,
    connection_ref: dict[str, Any],
    stage_state: TransferStageState,
) -> None:
    try:
        cleanup_materialized_sources(
            options=options,
            connection_ref=connection_ref,
            stage_state=stage_state,
        )
    finally:
        close_connection_ref(connection_ref, options.from_db_key, "source")


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
            f"counting transfer source rows on {options.from_db_key} ({options.from_db_backend})"
        ),
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        operation=operation,
    )


def _materialize_source_with_retry(
    options: TransferOptions,
    source_ref: dict[str, Any],
    stage_state: TransferStageState,
) -> str:
    staging_schema = options.source_transfer_staging_schema
    if staging_schema is None:
        message = "Expected source transfer staging schema to be configured."
        raise RuntimeError(message)
    adapter = get_backend_adapter(options.from_db_backend)
    stage_table = build_stage_table_name(
        options.from_db_backend,
        "source_result",
        transfer_staging_schema=staging_schema,
        transfer_staging_username=options.source_transfer_staging_username,
        random_suffix=(
            f"{options.transfer_id}__source"
            if options.transfer_id is not None
            else uuid.uuid4().hex[:8]
        ),
        destination_hash=options.destination_hash,
    )
    stage_state.source_stage_tables.append(stage_table)

    def operation(attempt: int) -> None:
        try:
            if attempt > 1:
                adapter.drop_table(
                    source_ref["connection"],
                    stage_table,
                    if_exists=True,
                    query_label=options.query_label,
                )
            adapter.execute_command(
                source_ref["connection"],
                adapter.build_materialize_transfer_source_sql(
                    stage_table,
                    options.source_sql,
                    query_label=options.query_label,
                ),
            )
        except Exception:
            rollback_quietly(source_ref["connection"])
            replace_connection(options.from_db_key, source_ref)
            raise

    run_with_retry(
        operation_name=(
            f"materializing transfer source on {options.from_db_key} ({options.from_db_backend})"
        ),
        retry_cnt=options.retry_cnt,
        timeout_increment=options.timeout_increment,
        operation=operation,
    )
    return f"SELECT * FROM {stage_table}"  # noqa: S608


def _count_materialized_source_rows_with_retry(
    options: TransferOptions,
    source_ref: dict[str, Any],
    stage_table: str,
) -> int:
    adapter = get_backend_adapter(options.from_db_backend)

    def operation(attempt: int) -> int:
        del attempt
        try:
            return int(
                adapter.count_table_rows(
                    source_ref["connection"],
                    stage_table,
                    query_label=options.query_label,
                )
            )
        except Exception:
            rollback_quietly(source_ref["connection"])
            replace_connection(options.from_db_key, source_ref)
            raise

    return int(
        run_with_retry(
            operation_name=(
                f"counting materialized transfer source rows on {options.from_db_key} "
                f"({options.from_db_backend})"
            ),
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=operation,
        )
    )


def _sum_worker_expected_rows(stage_state: TransferStageState) -> int:
    worker_states = getattr(stage_state, "worker_stage_states", None)
    if worker_states is None:
        raise RuntimeError("Expected worker stage states to be initialized.")
    return sum(worker_state.stage_state.expected_source_rows or 0 for worker_state in worker_states)


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
        message = f"Expecting {_format_row_count(expected_rows)} source row(s){key_suffix}"
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
