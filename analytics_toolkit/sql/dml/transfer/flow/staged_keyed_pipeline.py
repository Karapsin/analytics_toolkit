from __future__ import annotations

# ruff: noqa: BLE001, EM101, I001, PLR0913, PLR0915, TID252, TRY003
import contextvars
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
import pandas as pd
from tqdm import tqdm
from analytics_toolkit.general import time_print

from ...._log_context import sql_log_context
from ....connection.get_sql_connection import get_sql_connection
from ....ddl.schema import validate_table_schema_columns
from ....execution.cancellation import raise_if_cancelled
from ..runtime.connection_pool import BoundedConnectionManager
from ..runtime.models import (
    AdaptiveBatchSizer,
    TransferConnectionRefs,
    TransferOptions,
    TransferSliceRowCount,
    TransferStageState,
)
from ..schema import (
    get_existing_target_insert_types,
    inspect_source_query_schema,
    map_source_schema_to_target,
)
from .finalize import cleanup_stage, finalize_loaded_stage
from .lazy_keyed_runtime import (
    AcknowledgedSourceStageDropError,
    AttemptMetadata,
    DropReady,
    FinalizedTargetCleanupError,
    KeyReadComplete,
    LazyKeyedRuntime,
    ReadyKeyTask,
    VerifiedKey,
    freeze_attempt_metadata,
    get_with_cancellation as _get_with_cancellation,
    make_batch_sizer as _make_batch_sizer,
    put_batch_with_cancellation as _put_batch_with_cancellation,
    put_with_cancellation as _put_with_cancellation,
    release_queued_batch_slot,
    wait_for_assignment as _wait_for_assignment,
)
from .progress import make_transfer_progress_bar
from .row_counts import validate_loaded_stage_row_count
from .stage import _with_internal_column_types, create_stage_state, ensure_transfer_target_table
from .stage_identity import resolve_internal_columns
from .stage_validation import validate_transfer_stage_identity
from .staged_keyed_io import (
    allocate_source_stage_name,
    capture_final_target_count,
    cleanup_failed_empty_source_stages,
    cleanup_source_stages,
    consolidate_created_stages as _consolidate_created_stages,
    create_target_writer_stage,
    drop_source_stage,
    insert_target_batch,
    make_target_host_connection_runner,
    materialize_source_key,
    read_key_batch,
    validate_target_key,
)
from .staged_keyed_logging import (
    attach_attempt_summary,
    log_batch_progress,
    log_key_verification,
    log_loading_complete,
    log_pipeline_start,
    log_transfer_complete,
    slice_tag as _slice_tag,
)
from .staged_keyed_stream import KeyStreamCallbacks, KeyStreamContext, stream_ready_key
from .superseded import cleanup_superseded_transfer_stages
from .transfer_progress import (
    BatchTiming,
    TransferProgressTracker,
    format_duration,
    format_eta,
)


def run_keyed_staged_source_transfer_attempt(  # noqa: C901, PLR0912 -- attempt lifecycle
    options: TransferOptions,
    *,
    insert_retry_cnt: int,
) -> int:
    transfer_slices = options.transfer_slices
    if not transfer_slices:
        raise ValueError("Keyed source staging requires transfer slices.")
    if options.transfer_id is None or options.canonical_destination_identity is None:
        raise RuntimeError("Transfer runtime identity must be initialized.")

    read_workers = options.transfer_concurrency.effective_read
    write_workers = options.transfer_concurrency.effective_write
    source_connections = BoundedConnectionManager(
        options.from_db_key,
        read_workers,
        role="source transfer pool",
        open_connection=get_sql_connection,
    )
    target_connections = BoundedConnectionManager(
        options.to_db_key,
        write_workers,
        role="target transfer pool",
        open_connection=get_sql_connection,
    )
    runtime = LazyKeyedRuntime(
        transfer_slices,
        read_workers=read_workers,
        write_workers=write_workers,
    )
    runtime.add_failure_callback(source_connections.interrupt_active)
    runtime.add_failure_callback(target_connections.interrupt_active)
    progress_bar = make_transfer_progress_bar(options, total=None, base_tqdm=tqdm)
    progress = TransferProgressTracker(
        total_key_count=len(transfer_slices),
        active_writers=write_workers,
        consolidation_enabled=options.write_mode != "upsert",
        attempt_number=options.attempt_number,
        progress_bar=progress_bar,
    )
    stage_state: TransferStageState | None = None
    transfer_error: BaseException | None = None
    attempt_started = time.monotonic()
    attempt_phase = "metadata inspection"
    total_rows = 0

    try:
        with source_connections.lease() as source_ref, target_connections.lease() as target_ref:
            refs = TransferConnectionRefs(source=source_ref, target=target_ref)
            stage_state = create_stage_state(options, refs)
            with sql_log_context(suppress_sql=True):
                metadata = _prepare_attempt(options, refs, stage_state)
        log_pipeline_start(options, runtime)
        time_print(
            f"Inspected transfer metadata for attempt {options.attempt_number}: "
            f"{len(metadata.source_columns)} source columns; "
            f"target {'exists' if stage_state.target_exists else 'does not exist'}; "
            "cached schema will be reused for all lazy stage DDL"
        )

        attempt_phase = "source-stage loading"
        _run_lazy_workers(
            options,
            metadata,
            stage_state,
            runtime,
            source_connections,
            target_connections,
            progress,
            insert_retry_cnt=insert_retry_cnt,
        )
        _sync_stage_state(options, stage_state, runtime, require_complete=True)
        total_rows = sum(item.expected_rows for item in runtime.verified.values())
        loading_snapshot = progress.mark_loading_complete()
        log_loading_complete(loading_snapshot)

        attempt_phase = "aggregate stage validation"
        stage_tables = stage_state.stage_tables or []
        with target_connections.lease() as target_ref:
            _validate_target_stages(
                options,
                target_ref,
                stage_state,
                stage_tables,
                {item.index: item.expected_rows for item in stage_state.slice_counts},
                {item.index: item.streamed_rows for item in stage_state.slice_counts},
            )

        attempt_phase = "target-stage consolidation"
        consolidation_started = time.monotonic()
        consolidated_rows = _consolidate_created_stages(
            options,
            target_connections,
            stage_state,
            stage_tables,
            runtime,
        )
        progress.record_consolidated_rows(
            logical_operation_id="writer-stage-consolidation",
            rows=consolidated_rows,
        )
        consolidation_snapshot = progress.mark_consolidation_complete()
        time_print(
            "Completed target-stage consolidation: "
            f"{len(stage_tables)} writer stages, {consolidated_rows:,} copied rows in "
            f"{format_duration(time.monotonic() - consolidation_started)}; "
            "remaining total transfer ETA "
            f"{format_eta(consolidation_snapshot.total_transfer_eta_seconds, approximate=True)}"
        )

        attempt_phase = "destination validation"
        source_connections.close()
        validate_loaded_stage_row_count(
            options=options,
            connection_refs=TransferConnectionRefs(),
            stage_state=stage_state,
            total_rows=total_rows,
            open_connection=get_sql_connection,
            target_connection_runner=target_connections.run,
        )
        attempt_phase = "destination finalization"
        finalization_snapshot = progress.mark_finalization_started()
        time_print(
            f"Starting destination finalization: mode {options.write_mode}; "
            f"{total_rows:,} rows; total transfer ETA "
            f"{format_eta(finalization_snapshot.total_transfer_eta_seconds, approximate=True)}"
        )
        finalization_started = time.monotonic()
        finalize_loaded_stage(
            options,
            TransferConnectionRefs(),
            stage_state,
            total_rows,
            target_connection_runner=target_connections.run,
            target_host_connection_runner=make_target_host_connection_runner(
                options, target_connections
            ),
        )
        progress.mark_finalization_complete()
        time_print(
            f"Completed destination finalization: mode {options.write_mode}; "
            f"{total_rows:,} rows in "
            f"{format_duration(time.monotonic() - finalization_started)}"
        )
        capture_final_target_count(options, target_connections)
    except BaseException as exc:
        transfer_error = exc
        target_connections.interrupt_active()
        attach_attempt_summary(exc, progress.snapshot(), attempt_phase)
        raise
    finally:
        if transfer_error is not None:
            try:
                cleanup_failed_empty_source_stages(
                    options,
                    runtime,
                    source_connections,
                )
            except BaseException:
                time_print(
                    "Failed to clean an empty attempt-owned source stage; "
                    "startup cleanup will retry it",
                    level="warning",
                )
        source_connections.close_preserving(transfer_error)
        progress_bar.close()
        post_attempt_error: BaseException | None = None
        try:
            if stage_state is not None:
                _sync_stage_state(options, stage_state, runtime, require_complete=False)
                preserve_verified_stages = isinstance(
                    transfer_error,
                    AcknowledgedSourceStageDropError,
                )
                original_stage_created = stage_state.stage_table_created
                if preserve_verified_stages:
                    stage_state.stage_table_created = False
                try:
                    target_connections.resume_for_cleanup()
                    cleanup_stage(
                        options,
                        TransferConnectionRefs(),
                        stage_state,
                        options.retry_cnt,
                        drop_created_target=transfer_error is not None,
                        target_connection_runner=target_connections.run,
                        safe_exception_logging=True,
                    )
                except BaseException as exc:
                    if transfer_error is None:
                        raise FinalizedTargetCleanupError(
                            "Target-stage cleanup failed after destination finalization; "
                            "the completed transfer will not be retransmitted."
                        ) from exc
                    try:
                        transfer_error.__dict__["analytics_toolkit_sql_retry_safe"] = False
                        add_note = getattr(transfer_error, "add_note", None)
                        if callable(add_note):
                            add_note(
                                "Attempt-owned target cleanup also failed; the full transfer "
                                "will not be retried."
                            )
                    except (AttributeError, TypeError):
                        raise FinalizedTargetCleanupError(
                            "Target-stage cleanup failed while handling a transfer error; "
                            "the attempt will not be retried."
                        ) from exc
                    time_print(
                        "Cleanup failed while handling the transfer error; "
                        "attempt-owned objects may require startup cleanup and the full "
                        "transfer will not be retried",
                        level="warning",
                    )
                finally:
                    stage_state.stage_table_created = original_stage_created
        except BaseException as exc:
            post_attempt_error = exc
        finally:
            target_connections.close_preserving(transfer_error or post_attempt_error)
        if post_attempt_error is not None:
            raise post_attempt_error.with_traceback(post_attempt_error.__traceback__)
        elapsed = time.monotonic() - attempt_started
        if transfer_error is None:
            log_transfer_complete(options, progress.snapshot(), runtime, elapsed)
    return total_rows


def _prepare_attempt(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    stage_state: TransferStageState,
) -> AttemptMetadata:
    representative_sql = (options.transfer_slices or [])[0].source_sql
    source_schema = inspect_source_query_schema(
        options.from_db_backend,
        refs.source["connection"],
        representative_sql,
    )
    source_columns = [column.name for column in source_schema]
    if not source_columns:
        raise ValueError(
            "Source staging requires an inspectable source schema with at least one column."
        )
    source_column_types = {column.name: column.native_type for column in source_schema}
    internal_columns = resolve_internal_columns(
        source_columns,
        options.from_db_backend,
        table_schema_names=(options.table_schema or {}).keys(),
    )
    cleanup_superseded_transfer_stages(
        options=options,
        connection=refs.source["connection"],
        backend=options.from_db_backend,
        connection_key=options.from_db_key,
        staging_schema=options.source_transfer_staging_schema,
        internal_columns=internal_columns,
        include_current_transfer_id=True,
    )
    cleanup_superseded_transfer_stages(
        options=options,
        connection=refs.target["connection"],
        backend=options.to_db_backend,
        connection_key=options.to_db_key,
        staging_schema=options.transfer_staging_schema,
        internal_columns=internal_columns,
        include_current_transfer_id=True,
    )
    source_types = (
        validate_table_schema_columns(options.table_schema, source_columns)
        if options.table_schema is not None
        else map_source_schema_to_target(
            source_schema,
            options.to_db_backend,
            source_backend=options.from_db_backend,
        )
    )
    stage_state.source_columns = source_columns
    stage_state.source_column_types = source_column_types
    stage_state.internal_columns = internal_columns
    stage_state.stage_column_types = _with_internal_column_types(
        source_types,
        options,
        stage_state,
    )
    ensure_transfer_target_table(options, refs, stage_state, source_columns)
    if stage_state.target_exists and (
        not options.replace_target_table or options.write_mode == "upsert"
    ):
        stage_state.insert_column_types = get_existing_target_insert_types(
            options.to_db_backend,
            refs.target["connection"],
            options.target_table,
            source_types,
            connection_key=options.to_db_key,
        )
    return freeze_attempt_metadata(
        source_columns=source_columns,
        source_column_types=source_column_types,
        stage_column_types=stage_state.stage_column_types,
        internal_columns=internal_columns,
    )


def _run_lazy_workers(
    options: TransferOptions,
    metadata: AttemptMetadata,
    stage_state: TransferStageState,
    runtime: LazyKeyedRuntime,
    source_connections: BoundedConnectionManager,
    target_connections: BoundedConnectionManager,
    progress: TransferProgressTracker,
    *,
    insert_retry_cnt: int,
) -> None:
    read_workers = options.transfer_concurrency.effective_read
    write_workers = options.transfer_concurrency.effective_write
    state_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=write_workers) as writer_executor, ThreadPoolExecutor(
        max_workers=read_workers
    ) as reader_executor:
        writer_futures = [
            writer_executor.submit(
                contextvars.copy_context().run,
                _writer_worker,
                options,
                metadata,
                stage_state,
                runtime,
                target_connections,
                progress,
                state_lock,
                writer_index,
                insert_retry_cnt,
            )
            for writer_index in range(write_workers)
        ]
        reader_futures = [
            reader_executor.submit(
                contextvars.copy_context().run,
                _reader_worker,
                options,
                metadata,
                runtime,
                source_connections,
                progress,
                state_lock,
                reader_index,
            )
            for reader_index in range(read_workers)
        ]
        _collect_workers(reader_futures, runtime)
        try:
            if not runtime.cancellation.is_set():
                for _ in range(write_workers):
                    _put_with_cancellation(runtime.ready, None, runtime)
        except BaseException as exc:
            runtime.fail(exc)
        finally:
            _collect_workers(writer_futures, runtime)
    try:
        _drain_drop_ready(options, runtime, source_connections, limit=None)
    except BaseException as exc:
        runtime.fail(exc)
    runtime.raise_first_error()
    if runtime.source_stage_tables:
        raise RuntimeError("Acknowledged source stages remain after the mandatory final drain.")


def _collect_workers(futures: list[Any], runtime: LazyKeyedRuntime) -> None:
    for future in futures:
        _collect_worker(future, runtime)


def _collect_worker(future: Any, runtime: LazyKeyedRuntime) -> None:
    try:
        future.result()
    except BaseException as exc:
        runtime.fail(exc)


def _reader_worker(
    options: TransferOptions,
    metadata: AttemptMetadata,
    runtime: LazyKeyedRuntime,
    source_connections: BoundedConnectionManager,
    progress: TransferProgressTracker,
    state_lock: threading.Lock,
    reader_index: int,
) -> None:
    try:
        while not runtime.cancellation.is_set():
            _drain_drop_ready(options, runtime, source_connections, limit=None)
            transfer_slice = runtime.claim_pending()
            if transfer_slice is None:
                return
            _acquire_live_stage_credit(options, runtime, source_connections)
            tag = _slice_tag(options, transfer_slice)
            progress.start_key(transfer_slice.index)
            materialize_started = time.monotonic()
            with source_connections.lease(cancellation=runtime.cancellation) as source_ref:
                with sql_log_context(f"{tag} ", suppress_sql=True):
                    source_stage = allocate_source_stage_name(
                        options,
                        source_ref,
                        transfer_slice.index,
                    )
                runtime.reserve_source_stage(source_stage)
                time_print(
                    f"{tag} Source reader {reader_index + 1}/"
                    f"{options.transfer_concurrency.effective_read} started source-stage CTAS"
                )
                try:
                    expected_rows = materialize_source_key(
                        options,
                        source_ref,
                        metadata,
                        transfer_slice,
                        source_stage,
                    )
                except BaseException:
                    try:
                        with sql_log_context(f"{tag} ", suppress_sql=True):
                            cleanup_source_stages(options, source_ref, [source_stage])
                        runtime.mark_source_stage_dropped(source_stage)
                    except BaseException:
                        time_print(
                            f"{tag} Source-stage CTAS/count failed and its exact stage "
                            "could not be removed; startup cleanup will retry it"
                        )
                    raise
            task = ReadyKeyTask(
                transfer_slice=transfer_slice,
                source_stage=source_stage,
                expected_rows=expected_rows,
                tag=tag,
                materialized_at=time.monotonic(),
            )
            runtime.publish_source_stage(task)
            with state_lock:
                progress.materialize_key(transfer_slice.index, expected_rows)
                snapshot = progress.snapshot()
                time_print(
                    f"{tag} Source reader {reader_index + 1}/"
                    f"{options.transfer_concurrency.effective_read} materialized "
                    f"{expected_rows:,} rows in "
                    f"{format_duration(time.monotonic() - materialize_started)}; "
                    f"known source rows {snapshot.exact_known_rows:,} across "
                    f"{snapshot.known_key_count}/{snapshot.total_key_count} keys; "
                    f"live source stages {runtime.live_source_stage_count}/"
                    f"{runtime.live_stage_limit}"
                )
            _put_with_cancellation(runtime.ready, task, runtime)
            _wait_for_assignment(task, runtime)
            _stream_ready_key(
                options,
                metadata,
                task,
                runtime,
                source_connections,
                progress,
            )
    except BaseException as exc:
        runtime.fail(exc)
        raise


def _stream_ready_key(
    options: TransferOptions,
    metadata: AttemptMetadata,
    task: ReadyKeyTask,
    runtime: LazyKeyedRuntime,
    source_connections: BoundedConnectionManager,
    progress: TransferProgressTracker | None = None,
) -> None:
    stream_ready_key(
        KeyStreamContext(
            options=options,
            metadata=metadata,
            runtime=runtime,
            source_connections=source_connections,
            progress=progress,
        ),
        task,
        KeyStreamCallbacks(
            drain_drop_ready=_drain_drop_ready,
            read_batch=read_key_batch,
            put_batch=_put_batch_with_cancellation,
            put_item=_put_with_cancellation,
        ),
    )


def _writer_worker(
    options: TransferOptions,
    metadata: AttemptMetadata,
    stage_state: TransferStageState,
    runtime: LazyKeyedRuntime,
    target_connections: BoundedConnectionManager,
    progress: TransferProgressTracker,
    state_lock: threading.Lock,
    writer_index: int,
    insert_retry_cnt: int,
) -> None:
    stage_table: str | None = None
    sizer = _make_batch_sizer(options)
    try:
        while not runtime.cancellation.is_set():
            item = _get_with_cancellation(runtime.ready, runtime)
            if item is None:
                return
            task = item
            task.writer_index = writer_index
            task.batch_queue = runtime.writer_queues[writer_index]
            task.batch_slot = runtime.writer_batch_slots[writer_index]
            task.batch_size = sizer.current_size
            progress.assign_key(task.transfer_slice.index, writer_index)
            task.assignment.set()
            if task.expected_rows > 0 and stage_table is None:
                stage_started = time.monotonic()
                with target_connections.lease(cancellation=runtime.cancellation) as target_ref:
                    stage_table = create_target_writer_stage(
                        options,
                        target_ref,
                        metadata,
                        writer_index,
                        on_stage_candidate=runtime.register_target_stage_candidate,
                        log_prefix=f"{task.tag} ",
                    )
                runtime.register_target_stage(writer_index, stage_table)
                progress.set_primary_writer(min(runtime.target_stages))
                time_print(
                    f"{task.tag} Target writer {writer_index + 1}/"
                    f"{options.transfer_concurrency.effective_write} created its private stage "
                    "on first non-empty key in "
                    f"{format_duration(time.monotonic() - stage_started)}"
                )
            _consume_key(
                options,
                metadata,
                stage_state,
                runtime,
                target_connections,
                progress,
                state_lock,
                writer_index,
                stage_table,
                task,
                sizer,
                insert_retry_cnt,
            )
    except BaseException as exc:
        runtime.fail(exc)
        raise


def _consume_key(  # noqa: C901, PLR0912 -- validates a full ordered key stream
    options: TransferOptions,
    metadata: AttemptMetadata,
    stage_state: TransferStageState,
    runtime: LazyKeyedRuntime,
    target_connections: BoundedConnectionManager,
    progress: TransferProgressTracker,
    state_lock: threading.Lock,
    writer_index: int,
    stage_table: str | None,
    task: ReadyKeyTask,
    sizer: AdaptiveBatchSizer,
    insert_retry_cnt: int,
) -> None:
    batch_queue = task.batch_queue
    if batch_queue is None:
        message = "Writer queue assignment was not initialized."
        raise RuntimeError(message)
    streamed_rows = 0
    batch_count = 0
    while True:
        item = _get_with_cancellation(batch_queue, runtime)
        if isinstance(item, KeyReadComplete):
            if item.task is not task or item.batch_count != batch_count:
                message = f"{task.tag} Source batch completion marker is inconsistent."
                raise RuntimeError(message)
            if item.streamed_rows != streamed_rows:
                message = f"{task.tag} Source and writer batch totals do not match."
                raise RuntimeError(message)
            break
        batch_count += 1
        if item.batch_index != batch_count:
            message = f"{task.tag} Logical source batch order is not contiguous."
            raise RuntimeError(message)
        if item.task is not task:
            message = f"{task.tag} Source batch belongs to a different key."
            raise RuntimeError(message)
        release_queued_batch_slot(item)
        if stage_table is None:
            message = f"{task.tag} A non-empty batch has no target writer stage."
            raise RuntimeError(message)
        insert_started = time.monotonic()
        with target_connections.lease(cancellation=runtime.cancellation) as target_ref:
            inserted_rows = insert_target_batch(
                options,
                target_ref,
                stage_table,
                item,
                metadata,
                insert_retry_cnt=insert_retry_cnt,
                committed_rows_getter=lambda: progress.snapshot().committed_rows,
            )
        insert_completed = time.monotonic()
        if inserted_rows != item.batch.row_count:
            message = (
                f"{task.tag} Target batch {item.batch_index} committed {inserted_rows} row(s); "
                f"expected {item.batch.row_count}."
            )
            raise RuntimeError(message)
        timing = BatchTiming(
            read_started_at=item.read_started_at,
            read_completed_at=item.read_completed_at,
            queued_at=item.queued_at or item.read_completed_at,
            insert_completed_at=insert_completed,
            approximate_memory_bytes=item.approximate_memory_bytes,
        )
        with state_lock:
            runtime.mark_batch_success(item.logical_id)
            batch_progress = progress.commit_batch(
                logical_batch_id=item.logical_id,
                key_id=task.transfer_slice.index,
                batch_index=item.batch_index,
                rows=inserted_rows,
                timing=timing,
                writer_id=writer_index,
            )
            if batch_progress is None:
                message = f"{task.tag} Logical target batch was acknowledged twice."
                raise RuntimeError(message)
            if stage_state.first_non_empty_batch is None:
                source_count = len(metadata.source_columns)
                stage_state.first_non_empty_batch = pd.DataFrame.from_records(
                    [item.batch.rows[0][:source_count]],
                    columns=metadata.source_columns,
                )
            log_batch_progress(task, batch_progress)
        streamed_rows += inserted_rows
        sizer.update(
            insert_completed - insert_started,
            inserted_rows=inserted_rows,
            memory_bytes=timing.approximate_memory_bytes,
        )
        task.batch_size = sizer.current_size

    try:
        validation_stage: str | list[str] | None = stage_table
        if validation_stage is None:
            validation_stage = list(runtime.target_stages.values())
        with target_connections.lease(cancellation=runtime.cancellation) as target_ref:
            validate_target_key(
                options,
                target_ref,
                metadata,
                task,
                validation_stage,
                streamed_rows,
            )
    except BaseException:
        time_print(
            f"{task.tag} Validation failed: target slice integrity did not match the "
            "materialized source count; source stage retained; final destination unchanged"
        )
        raise
    checkpoint = VerifiedKey(
        slice_index=task.transfer_slice.index,
        expected_rows=task.expected_rows,
        streamed_rows=streamed_rows,
        target_stage=stage_table,
    )
    with state_lock:
        runtime.mark_verified(checkpoint)
        verification = progress.verify_key(task.transfer_slice.index)
        if verification is None:
            message = f"{task.tag} Target key was verified twice."
            raise RuntimeError(message)
        log_key_verification(task, verification)
    runtime.drop_ready.put_nowait(DropReady(task, stage_table))


def _acquire_live_stage_credit(
    options: TransferOptions,
    runtime: LazyKeyedRuntime,
    source_connections: BoundedConnectionManager,
) -> None:
    while not runtime.cancellation.is_set():
        raise_if_cancelled()
        if runtime.live_stage_credits.acquire(timeout=0.1):
            return
        _drain_drop_ready(options, runtime, source_connections, limit=None)
    raise RuntimeError("Source stage scheduling was cancelled.")


def _drain_drop_ready(
    options: TransferOptions,
    runtime: LazyKeyedRuntime,
    source_connections: BoundedConnectionManager,
    *,
    limit: int | None,
) -> int:
    dropped = 0
    while limit is None or dropped < limit:
        try:
            acknowledgement = runtime.drop_ready.get_nowait()
        except queue.Empty:
            return dropped
        started = time.monotonic()
        try:
            with source_connections.lease() as source_ref:
                drop_source_stage(options, source_ref, acknowledgement.task)
        except BaseException as exc:
            message = (
                f"{acknowledgement.task.tag} Could not drop an acknowledged source stage; "
                "the verified target checkpoint will not be retransmitted."
            )
            error = AcknowledgedSourceStageDropError(message)
            runtime.fail(error)
            raise error from exc
        runtime.mark_source_stage_dropped(acknowledgement.task.source_stage)
        dropped += 1
        time_print(
            f"{acknowledgement.task.tag} Dropped acknowledged source stage in "
            f"{format_duration(time.monotonic() - started)}; live source stages "
            f"{runtime.live_source_stage_count}/{runtime.live_stage_limit}"
        )
    return dropped


def _validate_target_stages(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    stage_tables: list[str],
    source_counts: dict[int, int],
    staged_counts: dict[int, int],
) -> None:
    if staged_counts != source_counts:
        message = (
            "Source/stage keyed row-count mismatch: "
            f"source={source_counts}, staged={staged_counts}."
        )
        raise RuntimeError(message)
    if not stage_tables:
        if any(source_counts.values()):
            raise RuntimeError("Non-empty transfer has no target stage.")
        return
    if stage_state.internal_columns is None:
        raise RuntimeError("Transfer internal columns were not resolved.")
    validate_transfer_stage_identity(
        options=options,
        connection=target_ref["connection"],
        stage_tables=stage_tables,
        internal_columns=stage_state.internal_columns,
        expected_slice_counts=source_counts,
    )


def _sync_stage_state(
    options: TransferOptions,
    stage_state: TransferStageState,
    runtime: LazyKeyedRuntime,
    *,
    require_complete: bool,
) -> None:
    verified = runtime.verified
    expected_ids = {item.index for item in options.transfer_slices or []}
    if require_complete and set(verified) != expected_ids:
        raise RuntimeError("Not every transfer key reached a verified checkpoint.")
    stage_state.source_stage_tables = runtime.source_stage_tables
    stage_map = runtime.target_stages
    registered_stage_tables = [stage_map[index] for index in sorted(stage_map)]
    stage_tables = list(registered_stage_tables)
    if not require_complete:
        registered = set(stage_tables)
        stage_tables.extend(sorted(runtime.target_stage_candidates - registered))
    stage_state.stage_tables = stage_tables
    stage_state.stage_table = (
        registered_stage_tables[0]
        if registered_stage_tables
        else (stage_tables[0] if stage_tables else None)
    )
    stage_state.stage_table_created = bool(stage_tables)
    stage_state.expected_source_rows = sum(item.expected_rows for item in verified.values())
    stage_state.slice_counts = [
        TransferSliceRowCount(
            index=transfer_slice.index,
            label=_slice_tag(options, transfer_slice),
            expected_rows=verified[transfer_slice.index].expected_rows,
            streamed_rows=verified[transfer_slice.index].streamed_rows,
        )
        for transfer_slice in options.transfer_slices or []
        if transfer_slice.index in verified
    ]
