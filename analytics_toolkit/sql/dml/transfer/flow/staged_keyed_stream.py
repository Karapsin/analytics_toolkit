from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    KeyReadComplete,
    QueuedKeyBatch,
    acquire_batch_slot_with_cancellation,
    put_batch_with_cancellation,
    put_with_cancellation,
)
from analytics_toolkit.sql.dml.transfer.flow.staged_keyed_io import read_key_batch

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
        AttemptMetadata,
        LazyKeyedRuntime,
        ReadyKeyTask,
    )
    from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import (
        TransferProgressTracker,
    )
    from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import (
        BoundedConnectionManager,
    )
    from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch, TransferOptions


@dataclass(frozen=True)
class KeyStreamContext:
    options: TransferOptions
    metadata: AttemptMetadata
    runtime: LazyKeyedRuntime
    source_connections: BoundedConnectionManager
    progress: TransferProgressTracker | None = None


@dataclass(frozen=True)
class KeyStreamCallbacks:
    drain_drop_ready: Callable[..., int]
    read_batch: Callable[..., RowBatch] = read_key_batch
    put_batch: Callable[..., None] = put_batch_with_cancellation
    put_item: Callable[..., None] = put_with_cancellation


def stream_ready_key(
    context: KeyStreamContext,
    task: ReadyKeyTask,
    callbacks: KeyStreamCallbacks,
) -> None:
    """Read one whole key while enforcing one prefetched batch per writer."""
    options = context.options
    runtime = context.runtime
    batch_queue = task.batch_queue
    batch_slot = task.batch_slot
    if batch_queue is None or batch_slot is None:
        message = "Target writer did not attach its capacity-one batch queue."
        raise RuntimeError(message)
    callbacks.drain_drop_ready(options, runtime, context.source_connections, limit=1)
    start_ordinal = 1
    batch_index = 0
    streamed_rows = 0
    while start_ordinal <= task.expected_rows:
        batch_index += 1
        acquire_batch_slot_with_cancellation(batch_slot, runtime)
        slot_owned = True
        try:
            batch_size = task.batch_size or options.batch_size
            stop_ordinal = min(task.expected_rows + 1, start_ordinal + batch_size)
            read_started = time.monotonic()
            with context.source_connections.lease(cancellation=runtime.cancellation) as source_ref:
                batch = callbacks.read_batch(
                    options,
                    source_ref,
                    task,
                    context.metadata,
                    start_ordinal,
                    stop_ordinal,
                    batch_index=batch_index,
                    committed_rows_getter=(
                        (lambda: context.progress.snapshot().committed_rows)
                        if context.progress is not None
                        else None
                    ),
                )
            read_completed = time.monotonic()
            expected = stop_ordinal - start_ordinal
            if batch.row_count != expected:
                message = (
                    f"{task.tag} Source batch {batch_index} returned {batch.row_count} row(s); "
                    f"expected {expected}."
                )
                raise RuntimeError(message)
            queued = QueuedKeyBatch(
                task=task,
                batch_index=batch_index,
                start_ordinal=start_ordinal,
                stop_ordinal=stop_ordinal,
                batch=batch,
                read_started_at=read_started,
                read_completed_at=read_completed,
                approximate_memory_bytes=batch.approx_memory_bytes(),
                prefetch_slot=batch_slot,
            )
            callbacks.put_batch(batch_queue, queued, runtime)
            slot_owned = False
        finally:
            if slot_owned:
                batch_slot.release()
        streamed_rows += batch.row_count
        start_ordinal = stop_ordinal
        callbacks.drain_drop_ready(options, runtime, context.source_connections, limit=1)
    callbacks.put_item(
        batch_queue,
        KeyReadComplete(task, streamed_rows, batch_index),
        runtime,
    )
