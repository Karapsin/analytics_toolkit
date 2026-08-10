from __future__ import annotations

import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.dml.transfer.flow import (
    staged_attempt,
    staged_unkeyed_progress,
)
from analytics_toolkit.sql.dml.transfer.flow.range_scheduler import AdaptiveRangeScheduler
from analytics_toolkit.sql.dml.transfer.flow.staged_unkeyed_progress import (
    UnkeyedStagedProgress,
)
from analytics_toolkit.sql.dml.transfer.runtime import retry as retry_module
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch, TransferOptions


@dataclass
class FakeClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = value


class RecordingProgressBar:
    def __init__(self) -> None:
        self.updates: list[int] = []
        self.reset_calls = 0
        self.close_calls = 0

    def update(self, rows: int) -> None:
        self.updates.append(rows)

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _options(**overrides: Any) -> TransferOptions:
    values: dict[str, Any] = {
        "from_db_key": "source_alias",
        "from_db_backend": "gp",
        "to_db_key": "target_alias",
        "to_db_backend": "gp",
        "source_sql": "SELECT id FROM source_table",
        "target_table": "public.target_table",
        "transfer_id": "a" * 32,
        "canonical_destination_identity": "public.target_table",
        "source_transfer_staging_schema": "source_stage",
        "transfer_staging_schema": "target_stage",
        "write_mode": "replace",
        "attempt_number": 2,
    }
    values.update(overrides)
    return TransferOptions(**values)


def _batch(start: int, stop: int) -> RowBatch:
    return RowBatch(["id"], [(value,) for value in range(start, stop)])


def test_unkeyed_exact_progress_rates_eta_and_phase_transitions(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    progress_bar = RecordingProgressBar()
    logs: list[str] = []
    monkeypatch.setattr(
        staged_unkeyed_progress,
        "time_print",
        lambda message, **_kwargs: logs.append(message),
    )
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=300,
        worker_count=2,
        attempt_started_at=0,
        progress_bar=progress_bar,
        clock=clock,
    )

    first_batch = _batch(0, 100)
    first = progress.commit_batch(
        logical_batch_id=(0, 1, 101),
        worker_id=0,
        batch=first_batch,
        read_started_at=1,
        read_completed_at=1.5,
        insert_completed_at=2,
    )
    assert first.snapshot.committed_rows == 100
    assert first.batch_rows_per_second == 100
    assert first.batch_memory_bytes_per_second == pytest.approx(first_batch.approx_memory_bytes())
    assert first.snapshot.rolling_rows_per_second is None

    second = progress.commit_batch(
        logical_batch_id=(0, 101, 201),
        worker_id=1,
        batch=_batch(100, 200),
        read_started_at=3,
        read_completed_at=3.5,
        insert_completed_at=4,
    )
    assert second.snapshot.committed_rows == 200
    assert second.snapshot.average_rows_per_second == pytest.approx(200 / 3)
    assert second.snapshot.rolling_rows_per_second == pytest.approx(200 / 3)
    assert second.snapshot.remaining_load_rows == 100
    assert second.snapshot.load_eta_seconds == 1.5
    assert second.snapshot.total_transfer_eta_seconds == 8.25

    progress.commit_batch(
        logical_batch_id=(0, 201, 301),
        worker_id=1,
        batch=_batch(200, 300),
        read_started_at=5,
        read_completed_at=5.5,
        insert_completed_at=6,
    )
    clock.set(6)
    loading = progress.mark_loading_complete()
    assert loading.loading_complete is True
    assert loading.remaining_load_rows == 0
    assert loading.remaining_consolidation_rows == 200
    assert loading.remaining_finalization_rows == 300
    assert loading.total_transfer_eta_seconds == pytest.approx(25 / 3)
    assert progress.expected_consolidation_rows == 200

    clock.set(8)
    consolidated = progress.mark_consolidation_complete(
        stage_count=2,
        copied_rows=200,
        elapsed_seconds=2,
    )
    assert consolidated.consolidation_complete is True
    assert consolidated.remaining_consolidation_rows == 0
    assert consolidated.total_transfer_eta_seconds == 5

    finalizing = progress.mark_finalization_started()
    assert finalizing.finalization_started is True
    assert finalizing.remaining_finalization_rows == 300
    clock.set(10)
    finalized = progress.mark_finalization_complete()
    assert finalized.finalization_complete is True
    assert finalized.total_transfer_eta_seconds == 0
    clock.set(11)
    progress.log_transfer_complete(
        source_stages_dropped=1,
        target_stages_cleaned=2,
    )
    progress.close()

    batch_logs = [message for message in logs if "Staged batch" in message]
    assert len(batch_logs) == 3
    assert all(message.startswith("[slice=1/1]") for message in batch_logs)
    assert "rolling rate unavailable" in batch_logs[0]
    assert "load ETA unavailable" in batch_logs[0]
    assert "total transfer ETA unavailable" in batch_logs[0]
    assert "rolling rate 67 rows/s" in batch_logs[1]
    assert "rolling approximate RAM rate" in batch_logs[1]
    assert "load ETA 1.5 seconds" in batch_logs[1]
    assert "total transfer ETA ~8.2 seconds" in batch_logs[1]
    assert any("remaining total transfer ETA ~8.3 seconds" in message for message in logs)
    assert any("200 copied rows in 2 seconds" in message for message in logs)
    assert any("Completed destination finalization" in message for message in logs)
    assert any("source stages dropped 1/1" in message for message in logs)
    assert progress_bar.updates == [100, 100, 100]
    assert progress_bar.reset_calls == 1
    assert progress_bar.close_calls == 1


def test_unkeyed_one_batch_keeps_eta_unavailable_until_loading_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    monkeypatch.setattr(
        staged_unkeyed_progress,
        "time_print",
        lambda message, **_kwargs: logs.append(message),
    )
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=10,
        worker_count=1,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=FakeClock(),
    )
    progress.commit_batch(
        logical_batch_id=(0, 1, 11),
        worker_id=0,
        batch=_batch(0, 10),
        read_started_at=0,
        read_completed_at=0.5,
        insert_completed_at=1,
    )

    batch_log = next(message for message in logs if "Staged batch" in message)
    assert "load ETA unavailable" in batch_log
    assert "total transfer ETA unavailable" in batch_log
    loading = progress.mark_loading_complete()
    assert loading.load_eta_seconds is None
    assert loading.total_transfer_eta_seconds is None
    progress.close()


def test_unkeyed_upsert_excludes_consolidation_from_eta() -> None:
    progress = UnkeyedStagedProgress(
        _options(write_mode="upsert"),
        total_rows=200,
        worker_count=2,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=FakeClock(),
    )
    progress.commit_batch(
        logical_batch_id=(0, 1, 101),
        worker_id=1,
        batch=_batch(0, 100),
        read_started_at=0,
        read_completed_at=1,
        insert_completed_at=2,
    )
    second = progress.commit_batch(
        logical_batch_id=(0, 101, 201),
        worker_id=1,
        batch=_batch(100, 200),
        read_started_at=2,
        read_completed_at=3,
        insert_completed_at=4,
    )

    assert second.snapshot.remaining_consolidation_rows == 0
    assert progress.expected_consolidation_rows == 0
    assert progress.mark_loading_complete().remaining_consolidation_rows == 0
    progress.close()


def test_unkeyed_eta_uses_observed_worker_assignments() -> None:
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=300,
        worker_count=4,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=FakeClock(),
    )
    progress.commit_batch(
        logical_batch_id=(0, 1, 101),
        worker_id=0,
        batch=_batch(0, 100),
        read_started_at=0,
        read_completed_at=1,
        insert_completed_at=2,
    )
    second = progress.commit_batch(
        logical_batch_id=(0, 101, 201),
        worker_id=0,
        batch=_batch(100, 200),
        read_started_at=2,
        read_completed_at=3,
        insert_completed_at=4,
    )

    assert second.snapshot.remaining_consolidation_rows == 0
    assert second.snapshot.total_transfer_eta_seconds == 8
    progress.close()


def test_concurrent_batch_logs_follow_monotonic_completion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=2,
        worker_count=2,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=FakeClock(),
    )
    first_log_started = threading.Event()
    release_first_log = threading.Event()
    second_log_started = threading.Event()
    logged_indices: list[int] = []

    def log_batch(batch_progress: Any) -> None:
        if batch_progress.batch_index == 1:
            first_log_started.set()
            assert release_first_log.wait(2)
        else:
            second_log_started.set()
        logged_indices.append(batch_progress.batch_index)

    monkeypatch.setattr(progress, "_log_batch", log_batch)

    def commit(batch_id: int, worker_id: int) -> None:
        progress.commit_batch(
            logical_batch_id=(0, batch_id, batch_id + 1),
            worker_id=worker_id,
            batch=_batch(batch_id, batch_id + 1),
            read_started_at=0,
            read_completed_at=1,
            insert_completed_at=2,
        )

    first = threading.Thread(target=commit, args=(0, 0))
    second = threading.Thread(target=commit, args=(1, 1))
    first.start()
    assert first_log_started.wait(2)
    second.start()
    assert not second_log_started.wait(0.1)
    release_first_log.set()
    first.join(2)
    second.join(2)

    assert logged_indices == [1, 2]
    progress.close()


def test_unkeyed_zero_rows_reports_zero_completion_without_batch_throughput(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    logs: list[str] = []
    progress_bar = RecordingProgressBar()
    monkeypatch.setattr(
        staged_unkeyed_progress,
        "time_print",
        lambda message, **_kwargs: logs.append(message),
    )
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=0,
        worker_count=1,
        attempt_started_at=0,
        progress_bar=progress_bar,
        clock=clock,
    )

    loading = progress.mark_loading_complete()
    assert loading.load_eta_seconds == 0
    assert loading.total_transfer_eta_seconds == 0
    progress.mark_consolidation_complete(
        stage_count=1,
        copied_rows=0,
        elapsed_seconds=0,
    )
    progress.mark_finalization_started()
    clock.set(1)
    progress.mark_finalization_complete()
    clock.set(2)
    progress.log_transfer_complete(
        source_stages_dropped=1,
        target_stages_cleaned=1,
    )
    progress.close()

    assert not any("Staged batch" in message for message in logs)
    assert any(
        "Completed transfer: 0 rows" in message
        and "no batch throughput" in message
        and "load ETA 0 seconds" in message
        for message in logs
    )
    assert progress_bar.updates == []
    assert progress_bar.close_calls == 1


@pytest.mark.parametrize(
    ("total_rows", "worker_count", "message"),
    [
        (-1, 1, "total_rows"),
        (True, 1, "total_rows"),
        (0, 0, "worker_count"),
        (0, True, "worker_count"),
    ],
)
def test_unkeyed_progress_rejects_invalid_counts(
    total_rows: Any,
    worker_count: Any,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        UnkeyedStagedProgress(
            _options(),
            total_rows=total_rows,
            worker_count=worker_count,
            attempt_started_at=0,
            progress_bar=RecordingProgressBar(),
            clock=FakeClock(),
        )


class RecordingCommitProgress:
    def __init__(self, events: list[str] | None = None) -> None:
        self._timestamps = iter((1.0, 2.0, 3.0))
        self.commits: list[dict[str, Any]] = []
        self._events = events

    def now(self) -> float:
        return next(self._timestamps)

    def commit_batch(self, **kwargs: Any) -> None:
        if self._events is not None:
            self._events.append("commit")
        self.commits.append(kwargs)

    @property
    def log_prefix(self) -> str:
        return "[slice=1/1] "

    @property
    def committed_rows(self) -> int:
        return sum(call["batch"].row_count for call in self.commits)

    def target_insert_retry_status(self, *_args: Any) -> str:
        return "ETA unchanged"


class OneRangeScheduler:
    def __init__(self) -> None:
        self.claimed = False
        self.completed: list[Any] = []

    def claim(self, _worker_id: int, _batch_size: int) -> Any:
        if self.claimed:
            return None
        self.claimed = True
        return SimpleNamespace(
            slice_id=0,
            start_ordinal=1,
            stop_ordinal=3,
            row_count=2,
        )

    def complete(self, _worker_id: int, claimed: Any) -> None:
        self.completed.append(claimed)


def test_range_worker_records_progress_only_after_successful_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    progress = RecordingCommitProgress(events)
    scheduler = OneRangeScheduler()
    batch = _batch(0, 2)
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "_read_snapshot_range", lambda *_args: batch)
    monkeypatch.setattr(
        staged_attempt,
        "insert_rows_batch",
        lambda *_args, **_kwargs: events.append("insert"),
    )

    staged_attempt._range_worker(
        _options(),
        "source_stage.snapshot",
        ["id"],
        SimpleNamespace(stage_column_types={"id": "BIGINT"}),
        "target_stage.worker_0",
        scheduler,
        0,
        1,
        transfer_progress=progress,  # type: ignore[arg-type]
    )

    assert events == ["insert", "commit"]
    assert len(progress.commits) == 1
    assert progress.commits[0]["logical_batch_id"] == (0, 1, 3)
    assert progress.commits[0]["read_started_at"] == 1
    assert progress.commits[0]["read_completed_at"] == 2
    assert progress.commits[0]["insert_completed_at"] == 3
    assert len(scheduler.completed) == 1


def test_range_worker_does_not_commit_progress_after_failed_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = RecordingCommitProgress()
    scheduler = OneRangeScheduler()
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "_read_snapshot_range", lambda *_args: _batch(0, 2))

    def fail_insert(*_args: Any, **_kwargs: Any) -> None:
        msg = "insert failed"
        raise OSError(msg)

    monkeypatch.setattr(staged_attempt, "insert_rows_batch", fail_insert)

    with pytest.raises(OSError, match="insert failed"):
        staged_attempt._range_worker(
            _options(),
            "source_stage.snapshot",
            ["id"],
            SimpleNamespace(stage_column_types={"id": "BIGINT"}),
            "target_stage.worker_0",
            scheduler,
            0,
            1,
            transfer_progress=progress,  # type: ignore[arg-type]
        )

    assert progress.commits == []
    assert scheduler.completed == []


def test_unkeyed_source_read_retry_is_tagged_safe_and_uses_operation_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    reads = 0
    progress = RecordingCommitProgress()
    progress._timestamps = iter(range(1, 20))
    scheduler = AdaptiveRangeScheduler({0: 2})
    options = _options(batch_size=2, min_batch_size=1, retry_cnt=2, full_retry_cnt=7)
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "rollback_quietly", lambda _connection: None)
    monkeypatch.setattr(staged_attempt, "replace_connection", lambda *_args: None)
    monkeypatch.setattr(
        staged_attempt,
        "time_print",
        lambda message, **_kwargs: logs.append(message),
    )

    def read(*args: Any) -> RowBatch:
        nonlocal reads
        reads += 1
        if reads == 1:
            message = "password=hunter2 row=('secret', 1)"
            raise OSError(message)
        return _batch(0, args[-1].row_count)

    monkeypatch.setattr(staged_attempt, "_read_snapshot_range", read)
    monkeypatch.setattr(
        staged_attempt,
        "insert_rows_batch",
        lambda _backend, _ref, _stage, _columns, rows, **_kwargs: len(rows),
    )

    staged_attempt._range_worker(
        options,
        "source_stage.snapshot",
        ["id"],
        SimpleNamespace(stage_column_types={"id": "BIGINT"}),
        "target_stage.worker_0",
        scheduler,
        0,
        1,
        transfer_progress=progress,  # type: ignore[arg-type]
    )

    retry_logs = [message for message in logs if "Retrying source-stage range" in message]
    assert len(retry_logs) == 1
    assert retry_logs[0].startswith("[slice=1/1] ")
    assert "attempt 2/2" in retry_logs[0]
    assert "hunter2" not in retry_logs[0]


def test_unkeyed_insert_retry_logs_stable_safe_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    secret = "password=hunter2 row=('customer-secret', 42)"
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=2,
        worker_count=1,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=FakeClock(),
    )
    monkeypatch.setattr(retry_module, "time_print", lambda message, **_kwargs: logs.append(message))

    def insert_rows(*_args: Any, **kwargs: Any) -> int:
        assert kwargs["safe_exception_logging"] is True
        assert kwargs["log_prefix"] == "[slice=1/1] "

        def operation(attempt: int) -> int:
            if attempt == 1:
                raise RuntimeError(secret)
            return 2

        return kwargs["retry_fn"](
            operation_name="unkeyed stage insert",
            retry_cnt=2,
            timeout_increment=0,
            operation=operation,
        )

    monkeypatch.setattr(staged_attempt, "insert_rows_batch", insert_rows)
    staged_attempt._insert_unkeyed_range_batch(
        _options(),
        {"connection": object()},
        SimpleNamespace(stage_column_types={"id": "BIGINT"}),
        "target_stage.worker_0",
        _batch(0, 2),
        (0, 1, 3),
        insert_retry_cnt=2,
        transfer_progress=progress,
    )

    output = "\n".join(logs)
    assert logs
    assert all(message.startswith("[slice=1/1] ") for message in logs)
    assert "RuntimeError" in output
    assert secret not in output
    assert "committed total remains 0 rows; ETA unchanged" in output
    progress.close()


def test_unkeyed_attempt_summary_records_phase_rows_and_elapsed() -> None:
    clock = FakeClock()
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=2,
        worker_count=1,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=clock,
    )
    progress.commit_batch(
        logical_batch_id=(0, 1, 2),
        worker_id=0,
        batch=_batch(0, 1),
        read_started_at=0,
        read_completed_at=1,
        insert_completed_at=2,
    )
    clock.set(3)
    error = RuntimeError("failed")

    progress.attach_attempt_summary(error, "source-stage loading")

    assert error.analytics_toolkit_transfer_attempt_summary == {
        "phase": "source-stage loading",
        "committed_rows": 1,
        "elapsed_seconds": 3,
    }
    progress.close()
