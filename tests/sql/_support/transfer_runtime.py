from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.dml.transfer.flow import lazy_keyed_runtime
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    VerifiedKey,
)
from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import (
    BoundedConnectionCloseError,
    BoundedConnectionManager,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch, TransferSlice


class _CloseError(OSError):
    pass


class _OpenError(OSError):
    pass


class _Connection:
    def __init__(self, *, close_failures: int = 0, cancel_fails: bool = False) -> None:
        self.close_failures = close_failures
        self.cancel_fails = cancel_fails
        self.close_calls = 0
        self.cancel_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.close_failures:
            raise _CloseError

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self.cancel_fails:
            raise RuntimeError


class _ConnectionWithoutCancel:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _StateRejectingError(Exception):
    def __getattribute__(self, name: str) -> Any:
        if name == "__dict__":
            raise AttributeError
        return super().__getattribute__(name)


class _NoNoteError(Exception):
    add_note = None


def _manager_with_close_failure(role: str = "strict close pool") -> BoundedConnectionManager:
    manager = BoundedConnectionManager(
        "target",
        1,
        role=role,
        open_connection=lambda _key: _Connection(close_failures=10),
    )
    with manager.lease():
        pass
    return manager


def _slice(index: int) -> TransferSlice:
    return TransferSlice(index, (index,), "", f"SELECT {index}", f"key={index}")


def _task(index: int, table: str = "source.stage") -> ReadyKeyTask:
    return ReadyKeyTask(
        transfer_slice=_slice(index),
        source_stage=table,
        expected_rows=1,
        tag=f"[slice={index + 1}/2]",
        materialized_at=time.monotonic(),
    )


def _queued_batch(task: ReadyKeyTask) -> QueuedKeyBatch:
    batch = RowBatch(["id"], [(1,)])
    queued = QueuedKeyBatch(
        task=task,
        batch_index=1,
        start_ordinal=1,
        stop_ordinal=2,
        batch=batch,
        read_started_at=1.0,
        read_completed_at=2.0,
        approximate_memory_bytes=batch.approx_memory_bytes(),
    )
    assert queued.logical_id == (task.transfer_slice.index, 1, 1, 2)
    return queued


__all__ = [
    "Any",
    "BoundedConnectionCloseError",
    "BoundedConnectionManager",
    "LazyKeyedRuntime",
    "QueuedKeyBatch",
    "ReadyKeyTask",
    "RowBatch",
    "SimpleNamespace",
    "ThreadPoolExecutor",
    "TransferSlice",
    "VerifiedKey",
    "_CloseError",
    "_Connection",
    "_ConnectionWithoutCancel",
    "_NoNoteError",
    "_OpenError",
    "_StateRejectingError",
    "_manager_with_close_failure",
    "_queued_batch",
    "_slice",
    "_task",
    "lazy_keyed_runtime",
    "pytest",
    "queue",
    "threading",
    "time",
]
