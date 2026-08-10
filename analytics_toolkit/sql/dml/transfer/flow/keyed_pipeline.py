from __future__ import annotations

# ruff: noqa: BLE001, EM101, EM102, PERF203, PLR0913, TID252, TRY003, TRY004, TRY300, TRY301
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from queue import Full, Queue
from typing import Any

from analytics_toolkit.general import time_print

from ....backends import get_backend_adapter
from ....connection.get_sql_connection import get_sql_connection
from ...load.load_sql_table import insert_rows_batch
from ..io.source import iter_source_batches
from ..runtime.models import (
    AdaptiveBatchSizer,
    QueuedTransferBatch,
    SliceReadComplete,
    TransferConnectionRefs,
    TransferOptions,
    TransferSliceRowCount,
    TransferStageState,
)
from ..runtime.retry import (
    close_connection_ref,
    replace_connection,
    rollback_quietly,
    run_with_retry,
)
from .logging import (
    format_transfer_slice_log_label,
    pipeline_phase_message,
    pipeline_start_message,
    reader_slice_message,
    writer_batch_message,
)
from .parquet_batches import append_transfer_identity_columns
from .parquet_stage import (
    ensure_parquet_staging_dependencies,
    parquet_row_group_size,
    write_batch_to_parquet_stage,
)
from .row_counts import (
    TransferRowCountMismatchError,
    cleanup_sources_and_close,
    disable_query_limit_for_transfer_reads,
    prepare_row_count_validated_options,
)

_STOP = object()


@dataclass
class PipelineState:
    source_rows: dict[int, int] = field(default_factory=dict)
    staged_rows: dict[int, int] = field(default_factory=dict)
    expected_rows: dict[int, int] = field(default_factory=dict)
    batch_counts: dict[int, int] = field(default_factory=dict)
    acknowledged_by_slice: dict[int, int] = field(default_factory=dict)
    reader_complete: set[int] = field(default_factory=set)
    completed_slices: set[int] = field(default_factory=set)
    queued_batches: int = 0
    acknowledged_batches: int = 0
    failed_batches: int = 0
    first_error: BaseException | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancellation: threading.Event = field(default_factory=threading.Event)

    def fail(self, exc: BaseException) -> None:
        with self.lock:
            if self.first_error is None:
                self.first_error = exc
            self.cancellation.set()


class AdaptiveBatchController:
    def __init__(self, options: TransferOptions) -> None:
        self._lock = threading.Lock()
        self._sizer = AdaptiveBatchSizer(
            enabled=options.adaptive_batch_size,
            current_size=options.batch_size,
            min_size=options.min_batch_size,
            max_size=options.max_batch_size,
            min_target_seconds=options.min_batch_seconds,
            max_target_seconds=options.max_batch_seconds,
            optimize_by_rows_per_second=options.target_rows_per_second,
            target_seconds=options.target_batch_seconds,
            target_rows_per_second_window=options.target_rows_per_second_window,
            target_rows_per_second_deadband=options.target_rows_per_second_deadband,
            adaptive_batch_size_step=options.adaptive_batch_size_step,
            target_memory_bytes=options.target_batch_memory_bytes,
            min_target_memory_bytes=options.min_batch_memory_bytes,
            max_target_memory_bytes=options.max_batch_memory_bytes,
        )

    def current_size(self) -> int:
        with self._lock:
            return self._sizer.current_size

    def update(self, duration: float, rows: int, memory_bytes: int | None) -> None:
        with self._lock:
            self._sizer.update(duration, inserted_rows=rows, memory_bytes=memory_bytes)


def run_keyed_transfer_pipeline(
    *,
    options: TransferOptions,
    stage_state: TransferStageState,
    writer_stage_states: list[Any],
    read_retry_cnt: int,
    insert_retry_cnt: int,
) -> int:
    slices = options.transfer_slices or []
    read_workers = options.transfer_concurrency.effective_read
    write_workers = options.transfer_concurrency.effective_write
    if len(writer_stage_states) != (1 if options.trino_mode == "parquet" else write_workers):
        raise RuntimeError("Keyed pipeline writer stage count does not match concurrency.")

    queue: Queue[object] = Queue(maxsize=write_workers)
    state = PipelineState()
    controller = AdaptiveBatchController(options)
    time_print(pipeline_start_message(len(slices), read_workers, write_workers))

    writer_states = (
        writer_stage_states * write_workers
        if options.trino_mode == "parquet"
        else writer_stage_states
    )
    with ThreadPoolExecutor(max_workers=write_workers) as writer_pool:
        writer_futures = [
            writer_pool.submit(
                _run_in_context,
                contextvars.copy_context(),
                _writer,
                worker_index,
                write_workers,
                options,
                writer_state.stage_state,
                queue,
                state,
                controller,
                insert_retry_cnt,
            )
            for worker_index, writer_state in enumerate(writer_states)
        ]
        with ThreadPoolExecutor(max_workers=read_workers) as reader_pool:
            reader_futures = [
                reader_pool.submit(
                    _run_in_context,
                    contextvars.copy_context(),
                    _reader,
                    worker_index,
                    read_workers,
                    options,
                    slices[worker_index::read_workers],
                    queue,
                    state,
                    controller,
                    stage_state,
                    read_retry_cnt,
                )
                for worker_index in range(read_workers)
            ]
            for future in reader_futures:
                try:
                    future.result()
                except BaseException as exc:
                    state.fail(exc)

        time_print(pipeline_phase_message("waiting for queued batches"))
        for _ in range(write_workers):
            _put_until_accepted(queue, _STOP, state, ignore_cancellation=True)
        queue.join()
        if state.first_error is None:
            time_print(pipeline_phase_message("waiting for queued batches", complete=True))
        for future in writer_futures:
            try:
                future.result()
            except BaseException as exc:
                state.fail(exc)

    if state.first_error is not None:
        raise state.first_error.with_traceback(state.first_error.__traceback__)
    if state.queued_batches != state.acknowledged_batches:
        raise RuntimeError(
            "Keyed transfer pipeline did not acknowledge every queued batch exactly once."
        )
    _publish_row_counts(options, stage_state, state)
    time_print(pipeline_phase_message("loading", complete=True))
    return sum(state.staged_rows.values())


def _run_in_context(context: contextvars.Context, function: Any, *args: Any) -> Any:
    return context.run(function, *args)


def _reader(
    worker_index: int,
    worker_count: int,
    options: TransferOptions,
    slices: list[Any],
    queue: Queue[object],
    state: PipelineState,
    controller: AdaptiveBatchController,
    shared_stage_state: TransferStageState,
    read_retry_cnt: int,
) -> None:
    connection_refs = TransferConnectionRefs(
        source={"connection": get_sql_connection(options.from_db_key)},
        target={},
    )
    local_stage_state = TransferStageState(
        target_exists=shared_stage_state.target_exists,
        source_column_types=shared_stage_state.source_column_types,
        source_columns=list(shared_stage_state.source_columns),
        internal_columns=shared_stage_state.internal_columns,
    )
    try:
        for transfer_slice in slices:
            if state.cancellation.is_set():
                break
            position = transfer_slice.index + 1
            label = format_transfer_slice_log_label(options, transfer_slice)
            time_print(
                reader_slice_message(
                    worker_index,
                    worker_count,
                    position,
                    len(options.transfer_slices or []),
                    label,
                    "starting",
                )
            )
            slice_options = replace(options, source_sql=transfer_slice.source_sql)
            slice_options = prepare_row_count_validated_options(
                options=slice_options,
                connection_refs=connection_refs,
                stage_state=local_stage_state,
                slice_index=transfer_slice.index,
                transfer_key_label=label,
            )
            source_rows = 0
            batch_count = 0
            next_ordinal = 1
            for source_batch in iter_source_batches(
                options.from_db_key,
                options.from_db_backend,
                connection_refs.source,
                slice_options.source_sql,
                options.batch_size,
                retry_cnt=read_retry_cnt,
                timeout_increment=options.timeout_increment,
                query_label=options.query_label,
                get_batch_size=controller.current_size,
                disable_ch_query_limit=disable_query_limit_for_transfer_reads(
                    options.from_db_backend
                ),
            ):
                if state.cancellation.is_set():
                    break
                if source_batch.empty:
                    continue
                batch = get_backend_adapter(
                    options.from_db_backend
                ).normalize_transfer_source_batch(
                    source_batch, shared_stage_state.source_column_types
                )
                batch = append_transfer_identity_columns(
                    batch,
                    options=options,
                    stage_state=shared_stage_state,
                    slice_id=transfer_slice.index,
                    start_ordinal=next_ordinal,
                )
                batch_count += 1
                item = QueuedTransferBatch(
                    slice_index=transfer_slice.index,
                    slice_position=position,
                    slice_count=len(options.transfer_slices or []),
                    key_label=label,
                    batch_index=batch_count,
                    start_ordinal=next_ordinal,
                    batch=batch,
                )
                next_ordinal += batch.row_count
                source_rows += batch.row_count
                time_print(
                    reader_slice_message(
                        worker_index,
                        worker_count,
                        position,
                        item.slice_count,
                        label,
                        f"read batch {batch_count} with {batch.row_count:,} row(s) for",
                    )
                    + "; waiting for target stage"
                )
                _put_until_accepted(queue, item, state)
                with state.lock:
                    state.queued_batches += 1
            _record_reader_complete(
                state,
                SliceReadComplete(transfer_slice.index, source_rows, batch_count),
                expected_rows=local_stage_state.current_expected_source_rows,
                label=label,
                position=position,
                slice_count=len(options.transfer_slices or []),
            )
            time_print(
                reader_slice_message(
                    worker_index,
                    worker_count,
                    position,
                    len(options.transfer_slices or []),
                    label,
                    f"finished source with {source_rows:,} row(s) read in "
                    f"{batch_count} batch(es) for",
                )
            )
    except BaseException as exc:
        state.fail(exc)
        raise
    finally:
        cleanup_sources_and_close(options, connection_refs.source, local_stage_state)


def _writer(
    worker_index: int,
    worker_count: int,
    options: TransferOptions,
    stage_state: TransferStageState,
    queue: Queue[object],
    state: PipelineState,
    controller: AdaptiveBatchController,
    insert_retry_cnt: int,
) -> None:
    try:
        _writer_impl(
            worker_index,
            worker_count,
            options,
            stage_state,
            queue,
            state,
            controller,
            insert_retry_cnt,
        )
    except BaseException as exc:
        state.fail(exc)
        while True:
            item = queue.get()
            queue.task_done()
            if item is _STOP:
                break
        raise


def _writer_impl(
    worker_index: int,
    worker_count: int,
    options: TransferOptions,
    stage_state: TransferStageState,
    queue: Queue[object],
    state: PipelineState,
    controller: AdaptiveBatchController,
    insert_retry_cnt: int,
) -> None:
    target_ref: dict[str, Any] = {}
    parquet_dependencies = None
    if options.trino_mode == "parquet":
        parquet_dependencies = ensure_parquet_staging_dependencies()
    else:
        target_ref["connection"] = get_sql_connection(options.to_db_key)
    gp_insert_sizer = _build_gp_insert_sizer(options)
    try:
        while True:
            item = queue.get()
            try:
                if item is _STOP:
                    return
                if not isinstance(item, QueuedTransferBatch):
                    raise RuntimeError("Unexpected keyed pipeline queue item.")
                if state.cancellation.is_set():
                    continue
                stage_table = stage_state.stage_table or "<external-stage>"
                time_print(
                    writer_batch_message(
                        worker_index,
                        worker_count,
                        item.batch_index,
                        item.slice_position,
                        item.slice_count,
                        stage_table,
                    )
                )
                started = time.monotonic()
                inserted_rows = _stage_batch(
                    options,
                    stage_state,
                    target_ref,
                    item,
                    worker_index,
                    insert_retry_cnt,
                    parquet_dependencies,
                    gp_insert_sizer,
                )
                duration = time.monotonic() - started
                controller.update(
                    duration,
                    inserted_rows,
                    item.batch.approx_memory_bytes()
                    if options.target_batch_memory_bytes is not None
                    else None,
                )
                _acknowledge(state, item, inserted_rows)
                with state.lock:
                    slice_total = state.staged_rows[item.slice_index]
                label = f" for {item.key_label}" if item.key_label else ""
                time_print(
                    f"Target writer {worker_index + 1}/{worker_count} staged "
                    f"{inserted_rows:,} row(s){label} into {stage_table}; "
                    f"slice staged total {slice_total:,}"
                )
            except BaseException as exc:
                with state.lock:
                    state.failed_batches += 1
                state.fail(exc)
            finally:
                queue.task_done()
    finally:
        if target_ref:
            close_connection_ref(target_ref, options.to_db_key, "target writer")


def _stage_batch(
    options: TransferOptions,
    stage_state: TransferStageState,
    target_ref: dict[str, Any],
    item: QueuedTransferBatch,
    worker_index: int,
    insert_retry_cnt: int,
    parquet_dependencies: tuple[Any, Any, Any] | None,
    gp_insert_sizer: AdaptiveBatchSizer | None,
) -> int:
    if parquet_dependencies is not None:
        pa, pq, fsspec_module = parquet_dependencies
        if stage_state.stage_external_location is None:
            raise RuntimeError("Expected Parquet stage external location.")
        return write_batch_to_parquet_stage(
            item.batch,
            file_index=item.batch_index,
            slice_index=item.slice_index,
            stage_external_location=stage_state.stage_external_location,
            pa=pa,
            pq=pq,
            fsspec_module=fsspec_module,
            storage_options=options.parquet_storage_options,
            row_group_size=parquet_row_group_size(options),
            transfer_id=options.transfer_id,
            worker_id=worker_index,
            start_ordinal=item.start_ordinal,
            stop_ordinal=item.start_ordinal + item.batch.row_count - 1,
        )
    stage_table = stage_state.stage_table
    if stage_table is None:
        raise RuntimeError("Expected target writer stage table.")
    return insert_rows_batch(
        options.to_db_backend,
        target_ref,
        stage_table,
        item.batch.columns,
        item.batch.rows,
        retry_fn=run_with_retry,
        retry_cnt=insert_retry_cnt,
        timeout_increment=options.timeout_increment,
        target_column_types=stage_state.stage_column_types,
        gp_insert_chunk_size=options.gp_insert_chunk_size,
        trino_insert_chunk_size=options.trino_insert_chunk_size,
        query_label=options.query_label,
        connection_key=options.to_db_key,
        rollback_fn=rollback_quietly,
        replace_connection_fn=replace_connection,
        gp_insert_page_size_getter=(
            (lambda: gp_insert_sizer.current_size) if gp_insert_sizer is not None else None
        ),
        on_gp_insert_page_success=(
            (lambda duration, rows: gp_insert_sizer.update(duration, inserted_rows=rows))
            if gp_insert_sizer is not None
            else None
        ),
    )


def _build_gp_insert_sizer(options: TransferOptions) -> AdaptiveBatchSizer | None:
    sizing = get_backend_adapter(options.to_db_backend).transfer_insert_page_sizing(
        gp_insert_chunk_size=options.gp_insert_chunk_size
    )
    if sizing is None:
        return None
    return AdaptiveBatchSizer(
        enabled=options.adaptive_batch_size,
        current_size=sizing.initial_size,
        min_size=sizing.min_size,
        max_size=sizing.max_size,
        target_seconds=options.target_batch_seconds,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=options.target_rows_per_second_window,
        target_rows_per_second_deadband=options.target_rows_per_second_deadband,
        adaptive_batch_size_step=options.adaptive_batch_size_step,
    )


def _put_until_accepted(
    queue: Queue[object],
    item: object,
    state: PipelineState,
    *,
    ignore_cancellation: bool = False,
) -> None:
    while True:
        if state.cancellation.is_set() and not ignore_cancellation:
            raise RuntimeError("Keyed transfer pipeline cancelled while enqueueing a batch.")
        try:
            queue.put(item, timeout=0.1)
            return
        except Full:
            continue


def _record_reader_complete(
    state: PipelineState,
    complete: SliceReadComplete,
    *,
    expected_rows: int | None,
    label: str | None,
    position: int,
    slice_count: int,
) -> None:
    with state.lock:
        state.source_rows[complete.slice_index] = complete.source_rows
        state.batch_counts[complete.slice_index] = complete.batch_count
        state.expected_rows[complete.slice_index] = (
            complete.source_rows if expected_rows is None else expected_rows
        )
        state.reader_complete.add(complete.slice_index)
        _log_slice_complete_locked(state, complete.slice_index, label, position, slice_count)


def _acknowledge(state: PipelineState, item: QueuedTransferBatch, rows: int) -> None:
    with state.lock:
        state.acknowledged_batches += 1
        state.acknowledged_by_slice[item.slice_index] = (
            state.acknowledged_by_slice.get(item.slice_index, 0) + 1
        )
        state.staged_rows[item.slice_index] = state.staged_rows.get(item.slice_index, 0) + rows
        _log_slice_complete_locked(
            state, item.slice_index, item.key_label, item.slice_position, item.slice_count
        )


def _log_slice_complete_locked(
    state: PipelineState,
    slice_index: int,
    label: str | None,
    position: int,
    slice_count: int,
) -> None:
    if slice_index in state.completed_slices or slice_index not in state.reader_complete:
        return
    if state.acknowledged_by_slice.get(slice_index, 0) != state.batch_counts[slice_index]:
        return
    state.completed_slices.add(slice_index)
    suffix = f" for {label}" if label else ""
    time_print(
        f"Completed slice {position}/{slice_count}{suffix}: "
        f"{state.source_rows[slice_index]:,} row(s) read and staged"
    )


def _publish_row_counts(
    options: TransferOptions,
    stage_state: TransferStageState,
    state: PipelineState,
) -> None:
    total = sum(state.staged_rows.values())
    expected = sum(state.expected_rows.values())
    if options.validate_row_count:
        mismatches = [
            index
            for index, count in state.expected_rows.items()
            if count != state.staged_rows.get(index, 0)
        ]
        if mismatches:
            index = mismatches[0]
            raise TransferRowCountMismatchError(
                "Keyed transfer source/stage row-count mismatch for slice "
                f"{index + 1}: expected {state.expected_rows[index]}, "
                f"staged {state.staged_rows.get(index, 0)}."
            )
    stage_state.expected_source_rows = expected
    stage_state.streamed_rows = total
    labels = {
        item.index: format_transfer_slice_log_label(options, item)
        for item in options.transfer_slices or []
    }
    stage_state.slice_counts = [
        TransferSliceRowCount(
            index=index,
            label=labels.get(index),
            expected_rows=state.expected_rows[index],
            streamed_rows=state.staged_rows.get(index, 0),
        )
        for index in sorted(state.expected_rows)
    ]
