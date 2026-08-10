from __future__ import annotations

# ruff: noqa: EM101, PLR0913, S608, TID252, TRY003
import uuid
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from ...._log_context import sql_log_context
from ....backends import get_backend_adapter
from ....backends.transfer_stage import collision_stage_suffix, execute_transfer_materialization
from ....connection.get_sql_connection import get_ch_connection_for_host
from ....connection.refs import ensure_connection_ref
from ....dml.io.read_sql import _read_backend
from ...load.load_sql_table import insert_rows_batch
from ...load.stage import build_stage_table_name, cleanup_stage_table, create_stage_table
from ...table._basic_ops import table_exists
from ..runtime.models import RowBatch
from ..runtime.retry import replace_connection, rollback_quietly, run_with_retry
from .range_scheduler import OrdinalRange
from .row_counts import best_effort_transfer_target_count
from .source_snapshot import (
    build_snapshot_range_sql,
    build_snapshot_select_sql,
    build_source_snapshot_sql,
)
from .stage_validation import validate_transfer_stage_slice
from .staged_keyed_logging import slice_tag
from .staged_target import prepare_shared_parquet_stage, write_source_staged_batch

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..runtime.connection_pool import BoundedConnectionManager
    from ..runtime.models import TransferOptions, TransferStageState
    from .lazy_keyed_runtime import (
        AttemptMetadata,
        LazyKeyedRuntime,
        QueuedKeyBatch,
        ReadyKeyTask,
    )


_MINIMUM_CONSOLIDATION_STAGE_COUNT = 2


def prepare_keyed_target_stage(
    options: TransferOptions,
    refs: Any,
    stage_state: TransferStageState,
    runtime: LazyKeyedRuntime,
    progress: Any,
) -> None:
    if not prepare_shared_parquet_stage(options, refs, stage_state):
        return
    stage_table = stage_state.stage_table
    if stage_table is None:
        raise RuntimeError("Expected a shared Parquet target stage.")
    runtime.register_target_stage_candidate(stage_table)
    runtime.register_target_stage(0, stage_table)
    progress.set_primary_writer(0)


def write_keyed_target_batch(
    options: TransferOptions,
    target_connections: BoundedConnectionManager,
    stage_state: TransferStageState,
    stage_table: str,
    batch: QueuedKeyBatch,
    metadata: AttemptMetadata,
    *,
    writer_index: int,
    insert_retry_cnt: int,
    cancellation: Any,
    insert_fn: Any,
    committed_rows_getter: Callable[[], int],
) -> int:
    if options.trino_mode == "parquet":
        return write_source_staged_batch(
            options,
            {},
            stage_state,
            stage_table,
            batch.batch,
            worker_id=writer_index,
            slice_index=batch.task.transfer_slice.index,
            file_index=batch.batch_index,
            start_ordinal=batch.start_ordinal,
            stop_ordinal=batch.stop_ordinal,
            insert_retry_cnt=insert_retry_cnt,
            safe_exception_logging=True,
            log_prefix=f"{batch.task.tag} ",
        )
    with target_connections.lease(cancellation=cancellation) as target_ref:
        return int(
            insert_fn(
                options,
                target_ref,
                stage_table,
                batch,
                metadata,
                insert_retry_cnt=insert_retry_cnt,
                committed_rows_getter=committed_rows_getter,
            )
        )


def allocate_source_stage_name(
    options: TransferOptions,
    source_ref: dict[str, Any],
    slice_index: int,
) -> str:
    preferred = f"{options.transfer_id}__s{slice_index:05d}"
    for attempt in range(10):
        suffix = (
            preferred
            if attempt == 0
            else collision_stage_suffix(options.from_db_backend, preferred, uuid.uuid4().hex)
        )
        candidate = build_stage_table_name(
            options.from_db_backend,
            options.target_table,
            transfer_staging_schema=options.source_transfer_staging_schema,
            transfer_staging_username=options.source_transfer_staging_username,
            random_suffix=suffix,
            destination_hash=options.destination_hash,
        )
        if not table_exists(
            options.from_db_backend,
            source_ref["connection"],
            candidate,
            connection_key=options.from_db_key,
        ):
            return candidate
    raise RuntimeError("Could not allocate a unique source stage table name.")


def materialize_source_key(
    options: TransferOptions,
    source_ref: dict[str, Any],
    metadata: AttemptMetadata,
    transfer_slice: Any,
    source_stage: str,
) -> int:
    with sql_log_context(f"{slice_tag(options, transfer_slice)} ", suppress_sql=True):
        select_sql = build_snapshot_select_sql(
            backend=options.from_db_backend,
            source_sql=transfer_slice.source_sql,
            source_columns=metadata.source_columns,
            transfer_id=options.transfer_id or "",
            canonical_destination=options.canonical_destination_identity or "",
            slice_id=transfer_slice.index,
            internal_columns=metadata.internal_columns,
        )
        snapshot_sql = build_source_snapshot_sql(
            backend=options.from_db_backend,
            snapshot_table=source_stage,
            snapshot_select_sql=select_sql,
            internal_columns=metadata.internal_columns,
        )
        adapter = get_backend_adapter(options.from_db_backend)
        execute_transfer_materialization(
            adapter,
            options.from_db_backend,
            source_ref["connection"],
            snapshot_sql.create_sql,
        )
        for sql in snapshot_sql.post_create_sqls:
            adapter.execute_command(source_ref["connection"], sql)
        return count_source_slice(
            options,
            source_ref["connection"],
            source_stage,
            transfer_slice.index,
            metadata,
        )


def count_source_slice(
    options: TransferOptions,
    connection: Any,
    stage_table: str,
    slice_id: int,
    metadata: AttemptMetadata,
) -> int:
    adapter = get_backend_adapter(options.from_db_backend)
    slice_column = adapter.quote_identifier(metadata.internal_columns.slice_id)
    result = _read_backend(
        options.from_db_backend,
        connection,
        f"SELECT COUNT(*) FROM {stage_table} WHERE {slice_column} = {slice_id}",
        print_queries=False,
        output_type="dict",
        action_name="snapshot counting",
        phase="count_snapshot",
    )
    return int(result.columns[0][0])


def read_key_batch(
    options: TransferOptions,
    source_ref: dict[str, Any],
    task: ReadyKeyTask,
    metadata: AttemptMetadata,
    start_ordinal: int,
    stop_ordinal: int,
    *,
    batch_index: int | None = None,
    committed_rows_getter: Callable[[], int] | None = None,
) -> RowBatch:
    row_limit = stop_ordinal - start_ordinal
    sql = build_snapshot_range_sql(
        backend=options.from_db_backend,
        snapshot_table=task.source_stage,
        source_columns=metadata.source_columns,
        internal_columns=metadata.internal_columns,
        transfer_id=options.transfer_id or "",
        canonical_destination=options.canonical_destination_identity or "",
        ordinal_range=OrdinalRange(
            task.transfer_slice.index,
            start_ordinal,
            stop_ordinal,
        ),
    )

    def read_once(_attempt: int) -> RowBatch:
        connection = ensure_connection_ref(options.from_db_key, source_ref)
        try:
            with sql_log_context(f"{task.tag} ", suppress_sql=True):
                result = _read_backend(
                    options.from_db_backend,
                    connection,
                    sql,
                    print_queries=False,
                    output_type="dict",
                    action_name="source-batch reading",
                    phase="read_source_batch",
                )
        except Exception:
            rollback_quietly(connection)
            _replace_managed_connection(options.from_db_key, source_ref)
            raise
        result_row_count = len(result.columns[0]) if result.columns else 0
        if any(len(column) != result_row_count for column in result.columns):
            message = f"{task.tag} Source batch columns have unequal lengths."
            raise RuntimeError(message)
        if result_row_count > row_limit:
            message = (
                f"{task.tag} Source batch returned {result_row_count} row(s); "
                f"scheduled limit is {row_limit}."
            )
            raise RuntimeError(message)
        batch = RowBatch(
            columns=list(result.column_names),
            rows=list(zip(*result.columns)),
        )
        normalized = cast(
            "RowBatch",
            get_backend_adapter(
                options.from_db_backend,
            ).normalize_transfer_source_batch(
                batch,
                dict(metadata.source_column_types),
            ),
        )
        if normalized.row_count > row_limit:
            message = (
                f"{task.tag} Normalized source batch returned "
                f"{normalized.row_count} row(s); scheduled limit is {row_limit}."
            )
            raise RuntimeError(message)
        return normalized

    return cast(
        "RowBatch",
        run_with_retry(
            operation_name=(
                f"reading staged key {task.transfer_slice.index} rows "
                f"{start_ordinal}:{stop_ordinal}"
            ),
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=read_once,
            retryable_exceptions=(Exception,),
            log_prefix=f"{task.tag} ",
            safe_exception_logging=True,
            retry_status=lambda attempt, total: (
                f"Retrying source batch {batch_index or 1} read: attempt "
                f"{attempt}/{total}; committed total remains "
                f"{committed_rows_getter() if committed_rows_getter else 0:,} rows; "
                "ETA unchanged"
            ),
        ),
    )


def create_target_writer_stage(
    options: TransferOptions,
    target_ref: dict[str, Any],
    metadata: AttemptMetadata,
    writer_index: int,
    *,
    on_stage_candidate: Callable[[str], None] | None = None,
    log_prefix: str = "",
) -> str:
    columns = [*metadata.source_columns, *metadata.internal_columns.names()]
    with sql_log_context(log_prefix, suppress_sql=True):
        return create_stage_table(
            options.to_db_backend,
            target_ref["connection"],
            options.target_table,
            pd.DataFrame(columns=columns),
            column_types=metadata.stage_column_types,
            gp_distributed_by_key=options.gp_distributed_by_key,
            connection_key=options.to_db_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
            random_suffix=f"{options.transfer_id}__w{writer_index:05d}",
            destination_hash=options.destination_hash,
            on_stage_candidate=on_stage_candidate,
            log_prefix=log_prefix,
            ddl_properties=options.staging_ddl_properties,
            ch_creation_policy=options.staging_ch_policy,
        )


def insert_target_batch(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_table: str,
    batch: QueuedKeyBatch,
    metadata: AttemptMetadata,
    *,
    insert_retry_cnt: int,
    committed_rows_getter: Callable[[], int] | None = None,
) -> int:
    with sql_log_context(f"{batch.task.tag} ", suppress_sql=True):
        return int(
            insert_rows_batch(
                options.to_db_backend,
                target_ref,
                stage_table,
                batch.batch.columns,
                batch.batch.rows,
                retry_fn=lambda **kwargs: run_with_retry(
                    **kwargs,
                    log_prefix=f"{batch.task.tag} ",
                    safe_exception_logging=True,
                    retry_status=lambda attempt, total: (
                        f"Retrying target-stage batch {batch.batch_index} insert: attempt "
                        f"{attempt}/{total}; committed total remains "
                        f"{committed_rows_getter() if committed_rows_getter else 0:,} rows; "
                        "ETA unchanged"
                    ),
                ),
                retry_cnt=insert_retry_cnt,
                timeout_increment=options.timeout_increment,
                target_column_types=dict(metadata.stage_column_types or {}),
                gp_insert_chunk_size=options.gp_insert_chunk_size,
                trino_insert_chunk_size=options.trino_insert_chunk_size,
                query_label=options.query_label,
                connection_key=options.to_db_key,
                rollback_fn=rollback_quietly,
                replace_connection_fn=_replace_managed_connection,
                safe_exception_logging=True,
                log_prefix=f"{batch.task.tag} ",
            )
        )


def validate_target_key(
    options: TransferOptions,
    target_ref: dict[str, Any],
    metadata: AttemptMetadata,
    task: ReadyKeyTask,
    target_stage: str | list[str] | None,
    streamed_rows: int,
) -> None:
    validate_transfer_stage_slice(
        options=options,
        connection=target_ref["connection"],
        stage_table=target_stage,
        internal_columns=metadata.internal_columns,
        slice_id=task.transfer_slice.index,
        expected_count=task.expected_rows,
        streamed_count=streamed_rows,
        log_prefix=f"{task.tag} ",
    )


def drop_source_stage(
    options: TransferOptions,
    source_ref: dict[str, Any],
    task: ReadyKeyTask,
) -> None:
    def drop_once(_attempt: int) -> None:
        connection = ensure_connection_ref(options.from_db_key, source_ref)
        try:
            cleanup_stage_table(
                options.from_db_backend,
                connection,
                task.source_stage,
                query_label=options.query_label,
            )
        except Exception:
            rollback_quietly(connection)
            _replace_managed_connection(options.from_db_key, source_ref)
            raise

    with sql_log_context(f"{task.tag} ", suppress_sql=True):
        run_with_retry(
            operation_name=(
                f"dropping acknowledged source stage for slice {task.transfer_slice.index}"
            ),
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=drop_once,
            retryable_exceptions=(Exception,),
            log_prefix=f"{task.tag} ",
            safe_exception_logging=True,
            retry_status=lambda attempt, total: (
                "Retrying acknowledged source-stage drop: attempt "
                f"{attempt}/{total}; target checkpoint remains verified; "
                "rows will not be retransmitted"
            ),
        )


def cleanup_source_stages(
    options: TransferOptions,
    source_ref: dict[str, Any],
    stage_tables: list[str],
) -> None:
    first_error: Exception | None = None
    for stage_table in stage_tables:
        try:
            cleanup_stage_table(
                options.from_db_backend,
                source_ref["connection"],
                stage_table,
                query_label=options.query_label,
            )
        except Exception as exc:  # noqa: BLE001, PERF203
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def cleanup_failed_empty_source_stages(
    options: TransferOptions,
    runtime: LazyKeyedRuntime,
    source_connections: BoundedConnectionManager,
) -> None:
    stage_tables = [
        stage_table
        for stage_table, task in runtime.source_stage_entries.items()
        if task is None or task.expected_rows == 0
    ]
    if not stage_tables:
        return
    source_connections.resume_for_cleanup()
    first_error: Exception | None = None
    for stage_table in stage_tables:
        error = _cleanup_failed_empty_source_stage(
            options,
            runtime,
            source_connections,
            stage_table,
        )
        first_error = first_error or error
    if first_error is not None:
        raise first_error.with_traceback(first_error.__traceback__)


def _cleanup_failed_empty_source_stage(
    options: TransferOptions,
    runtime: LazyKeyedRuntime,
    source_connections: BoundedConnectionManager,
    stage_table: str,
) -> Exception | None:
    try:
        with source_connections.lease() as source_ref:
            cleanup_source_stages(options, source_ref, [stage_table])
        runtime.mark_source_stage_dropped(stage_table)
    except Exception as exc:  # noqa: BLE001 -- preserve the first driver cleanup error
        return exc
    return None


def consolidate_created_stages(
    options: TransferOptions,
    target_connections: BoundedConnectionManager,
    stage_state: TransferStageState,
    stage_tables: list[str],
    runtime: LazyKeyedRuntime,
) -> int:
    if options.write_mode == "upsert" or len(stage_tables) < _MINIMUM_CONSOLIDATION_STAGE_COUNT:
        return 0
    # Kept local to avoid staged_attempt -> staged_keyed_pipeline -> this module cycle.
    from .staged_attempt import _consolidate_worker_stages  # noqa: PLC0415

    with target_connections.lease() as target_ref:
        _consolidate_worker_stages(options, target_ref, stage_state, stage_tables)
    primary = stage_tables[0]
    return sum(
        checkpoint.expected_rows
        for checkpoint in runtime.verified.values()
        if checkpoint.target_stage is not None and checkpoint.target_stage != primary
    )


def capture_final_target_count(
    options: TransferOptions,
    target_connections: BoundedConnectionManager,
) -> None:
    if not options.collect_final_target_count:
        return
    object.__setattr__(
        options,
        "final_target_rows",
        best_effort_transfer_target_count(
            options,
            target_connection_runner=target_connections.run,
        ),
    )


def make_target_host_connection_runner(
    options: TransferOptions,
    target_connections: BoundedConnectionManager,
) -> Callable[[str, Callable[[Any], Any]], Any]:
    def run(host: str, operation: Callable[[Any], Any]) -> Any:
        return target_connections.run_with_connection(
            "clickhouse per-host target cleanup",
            lambda: get_ch_connection_for_host(options.to_db_key, host),
            operation,
        )

    return run


def _replace_managed_connection(
    connection_key: str,
    connection_ref: dict[str, Any],
) -> None:
    bounded_replace = connection_ref.get("bounded_replace_connection")
    if callable(bounded_replace):
        bounded_replace(connection_key, connection_ref)
        return
    replace_connection(connection_key, connection_ref)
