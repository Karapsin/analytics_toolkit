from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from analytics_toolkit.sql.dml.transfer.flow.logging import transfer_retry_message
from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import (
    BatchTiming,
    LogicalBatchConflictError,
    TransferProgressTracker,
    format_duration,
    format_eta,
    format_memory_rate,
    format_rows_rate,
    format_slice_tag,
)


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
        self._lock = threading.Lock()

    def update(self, rows: int) -> None:
        with self._lock:
            self.updates.append(rows)

    def reset(self) -> None:
        with self._lock:
            self.reset_calls += 1


class SecretObject:
    def __repr__(self) -> str:
        return "secret row contents"


def make_timing(
    read_started_at: float,
    insert_completed_at: float,
    *,
    memory_bytes: int = 1024,
) -> BatchTiming:
    duration = insert_completed_at - read_started_at
    return BatchTiming(
        read_started_at=read_started_at,
        read_completed_at=read_started_at + duration / 2,
        queued_at=read_started_at + duration * 3 / 4,
        insert_completed_at=insert_completed_at,
        approximate_memory_bytes=memory_bytes,
    )


def test_slice_tags_are_stable_safe_bounded_and_redacted() -> None:
    assert format_slice_tag(1, 1) == "[slice=1/1]"
    assert (
        format_slice_tag(
            2,
            12,
            [("event_date", "2026-08-02")],
        )
        == "[slice=2/12 key=event_date:'2026-08-02']"
    )
    assert (
        format_slice_tag(
            5,
            20,
            [("event_date", date(2026, 8, 2)), ("region", "EU")],
        )
        == "[slice=5/20 key=event_date:'2026-08-02',region:'EU']"
    )
    escaped = format_slice_tag(
        1,
        3,
        [
            ("bad key\n", "O'Reilly\nEU"),
            ("none", None),
            ("flag", True),
            ("number", Decimal("1.25")),
            ("blob", b"do-not-log"),
            ("opaque", SecretObject()),
        ],
    )
    assert "bad_key_:'O\\'Reilly\\nEU'" in escaped
    assert "none:NULL" in escaped
    assert "flag:TRUE" in escaped
    assert "number:'1.25'" in escaped
    assert "blob:<bytes:10>" in escaped
    assert "secret row contents" not in escaped
    assert "opaque:<SecretObject>" in escaped

    assert "top-secret" not in format_slice_tag(1, 1, [("access_token", "top-secret")])
    assert "<redacted>" in format_slice_tag(1, 1, [("access_token", "top-secret")])
    assert "bearer-value" not in format_slice_tag(
        1,
        1,
        [("id", "Bearer bearer-value")],
    )
    assert "hunter2" not in format_slice_tag(
        1,
        1,
        [("id", "postgres://user:hunter2@host/db")],
    )

    bounded = format_slice_tag(9, 99, [("key", "x" * 500)], max_length=32)
    assert len(bounded) == 32
    assert bounded.endswith("…]")
    hard_bounded = format_slice_tag(9, 99, [("key", "x" * 500)], max_length=1000)
    assert len(hard_bounded) <= 240


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((0, 1), "slice_position"),
        ((1, 0), "slice_count"),
        ((2, 1), "cannot exceed"),
    ],
)
def test_slice_tag_rejects_invalid_positions(
    args: tuple[int, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        format_slice_tag(*args)
    with pytest.raises(ValueError, match="at least"):
        format_slice_tag(1, 1, max_length=11)


def test_formatters_report_rows_memory_duration_and_approximate_eta() -> None:
    assert format_memory_rate(None) == "unavailable"
    assert format_memory_rate(0) == "0 B/s"
    assert format_memory_rate(1024) == "1.0 KiB/s"
    assert format_memory_rate(17.4 * 1024**2) == "17.4 MiB/s"
    assert format_memory_rate(1024**5) == "1024.0 TiB/s"
    assert format_rows_rate(None) == "unavailable"
    assert format_rows_rate(11_111.4) == "11,111 rows/s"
    assert format_duration(0.9) == "0.9 seconds"
    assert format_duration(1) == "1 second"
    assert format_duration(13) == "13 seconds"
    assert format_duration(64) == "1 minute 4 seconds"
    assert format_duration(120) == "2 minutes"
    assert format_eta(48, approximate=True) == "~48 seconds"
    assert format_eta(12, approximate=False) == "12 seconds"
    assert format_eta(None, approximate=True) == "unavailable"

    for invalid in (-1.0, math.inf, math.nan):
        with pytest.raises(ValueError, match="finite"):
            format_memory_rate(invalid)
        with pytest.raises(ValueError, match="finite"):
            format_rows_rate(invalid)
        with pytest.raises(ValueError, match="finite"):
            format_duration(invalid)


def test_full_retry_message_reports_prior_attempt_and_sanitizes_phase() -> None:
    reset = "rows/second, memory/second, load ETA, and total transfer ETA reset"
    assert transfer_retry_message(2, 5, None) == f"Restarting transfer attempt 2/5; {reset}"
    message = transfer_retry_message(
        2,
        5,
        {
            "phase": "source-stage loading",
            "committed_rows": 720_000,
            "elapsed_seconds": 49.04,
        },
    )
    assert "after source-stage loading" in message
    assert "previous attempt staged 720,000 rows in 49.0 seconds" in message
    assert message.endswith(reset)
    assert "credential-secret" not in transfer_retry_message(
        2,
        5,
        {"phase": "credential-secret", "committed_rows": "secret"},
    )


def test_lazy_key_estimate_and_two_batch_eta_threshold_use_committed_rows_only() -> None:
    clock = FakeClock()
    tracker = TransferProgressTracker(
        total_key_count=4,
        active_writers=2,
        clock=clock,
    )
    initial = tracker.snapshot()
    assert initial.estimated_total_rows is None
    assert initial.unknown_key_count == 4
    assert initial.load_eta_seconds is None

    assert tracker.materialize_key("a", 100, started_at=0)
    assert tracker.materialize_key("b", 300, started_at=0)
    assert tracker.assign_key("a", 0)
    assert tracker.assign_key("b", 1)
    estimated = tracker.snapshot()
    assert estimated.exact_known_rows == 400
    assert estimated.estimated_total_rows == 800
    assert estimated.total_rows_are_approximate is True
    assert estimated.remaining_consolidation_rows == 600
    assert estimated.remaining_finalization_rows == 800

    first = tracker.commit_batch(
        logical_batch_id=("a", 1),
        key_id="a",
        batch_index=1,
        rows=50,
        timing=make_timing(1, 2, memory_bytes=1024),
        writer_id=0,
    )
    assert first is not None
    assert first.batch_rows_per_second == 50
    assert first.batch_memory_bytes_per_second == 1024
    assert first.snapshot.committed_rows == 50
    assert first.snapshot.rolling_rows_per_second is None
    assert first.snapshot.load_eta_seconds is None
    assert first.snapshot.total_transfer_eta_seconds is None

    second = tracker.commit_batch(
        logical_batch_id=("a", 2),
        key_id="a",
        batch_index=2,
        rows=50,
        timing=make_timing(3, 4, memory_bytes=3072),
        writer_id=0,
    )
    assert second is not None
    snapshot = second.snapshot
    assert snapshot.committed_rows == 100
    assert snapshot.approximate_committed_memory_bytes == 4096
    assert snapshot.average_rows_per_second == 25
    assert snapshot.rolling_rows_per_second == 25
    assert snapshot.average_memory_bytes_per_second == 1024
    assert snapshot.rolling_memory_bytes_per_second == 1024
    assert snapshot.eta_rows_per_second == 25
    assert snapshot.remaining_load_rows == 700
    assert snapshot.load_eta_seconds == 28
    assert snapshot.total_transfer_eta_seconds == 84


def test_consolidation_eta_uses_actual_primary_and_is_disabled_for_upsert() -> None:
    clock = FakeClock()
    tracker = TransferProgressTracker(total_key_count=2, active_writers=2, clock=clock)
    tracker.materialize_key("zero", 0)
    tracker.assign_key("zero", 0)
    tracker.materialize_key("rows", 20)
    tracker.assign_key("rows", 1)
    tracker.set_primary_writer(1)
    assert tracker.snapshot().remaining_consolidation_rows == 0

    upsert = TransferProgressTracker(
        total_key_count=2,
        active_writers=2,
        consolidation_enabled=False,
        clock=clock,
    )
    upsert.materialize_key("a", 10)
    upsert.assign_key("a", 0)
    assert upsert.snapshot().remaining_consolidation_rows == 0


def test_eta_uses_lower_positive_global_rate_and_ages_rolling_samples() -> None:
    clock = FakeClock()
    tracker = TransferProgressTracker(
        total_key_count=1,
        rolling_window_seconds=5,
        clock=clock,
    )
    tracker.materialize_key("only", 300)
    tracker.assign_key("only", 0)
    tracker.commit_batch(
        logical_batch_id=1,
        key_id="only",
        batch_index=1,
        rows=100,
        timing=make_timing(0, 10),
    )
    second = tracker.commit_batch(
        logical_batch_id=2,
        key_id="only",
        batch_index=2,
        rows=100,
        timing=make_timing(10, 11),
    )
    assert second is not None
    assert second.snapshot.average_rows_per_second == pytest.approx(200 / 11)
    assert second.snapshot.rolling_rows_per_second == 40
    assert second.snapshot.eta_rows_per_second == pytest.approx(200 / 11)

    clock.set(20)
    aged = tracker.snapshot()
    assert aged.rolling_rows_per_second == 0
    assert aged.average_rows_per_second == 10
    assert aged.eta_rows_per_second == 10


def test_overlapping_batches_use_global_cumulative_wall_time() -> None:
    clock = FakeClock()
    tracker = TransferProgressTracker(total_key_count=1, clock=clock)
    tracker.materialize_key("one", 2000)
    results: list[object] = []
    results_lock = threading.Lock()

    def commit(index: int) -> None:
        result = tracker.commit_batch(
            logical_batch_id=index,
            key_id="one",
            batch_index=index,
            rows=100,
            timing=make_timing(0, 10, memory_bytes=1000),
            writer_id=0,
        )
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=commit, args=(index,)) for index in range(1, 21)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = tracker.snapshot(at=10)
    assert len(results) == 20
    assert all(result is not None for result in results)
    assert snapshot.committed_rows == 2000
    assert snapshot.successful_batch_count == 20
    assert snapshot.average_rows_per_second == 200
    assert snapshot.rolling_rows_per_second == 200
    assert snapshot.average_memory_bytes_per_second == 2000


def test_duplicate_logical_batches_are_idempotent_and_conflicts_fail() -> None:
    progress_bar = RecordingProgressBar()
    tracker = TransferProgressTracker(
        total_key_count=1,
        clock=FakeClock(),
        progress_bar=progress_bar,
    )
    tracker.materialize_key("key", 10)
    timing = make_timing(0, 1, memory_bytes=20)
    first = tracker.commit_batch(
        logical_batch_id="batch-1",
        key_id="key",
        batch_index=1,
        rows=10,
        timing=timing,
        writer_id=0,
    )
    duplicate = tracker.commit_batch(
        logical_batch_id="batch-1",
        key_id="key",
        batch_index=1,
        rows=10,
        timing=make_timing(0.5, 2, memory_bytes=20),
        writer_id=0,
    )
    assert first is not None
    assert duplicate is None
    assert tracker.snapshot().committed_rows == 10
    assert progress_bar.updates == [10]

    with pytest.raises(LogicalBatchConflictError, match="different data"):
        tracker.commit_batch(
            logical_batch_id="batch-1",
            key_id="key",
            batch_index=1,
            rows=9,
            timing=timing,
            writer_id=0,
        )
    tracker.reset(attempt_number=2, started_at=3)
    assert progress_bar.reset_calls == 1
    assert tracker.snapshot(at=3).committed_rows == 0


def test_phase_aware_total_eta_removes_completed_work() -> None:
    clock = FakeClock()
    tracker = TransferProgressTracker(
        total_key_count=2,
        active_writers=2,
        clock=clock,
    )
    tracker.materialize_key("primary", 100)
    tracker.materialize_key("secondary", 100)
    tracker.assign_key("primary", 0)
    tracker.assign_key("secondary", 1)
    tracker.commit_batch(
        logical_batch_id=1,
        key_id="primary",
        batch_index=1,
        rows=100,
        timing=make_timing(0, 1),
    )
    tracker.commit_batch(
        logical_batch_id=2,
        key_id="secondary",
        batch_index=1,
        rows=100,
        timing=make_timing(1, 2),
    )

    loading = tracker.mark_loading_complete()
    assert loading.load_eta_seconds == 0
    assert loading.remaining_load_rows == 0
    assert loading.remaining_consolidation_rows == 100
    assert loading.remaining_finalization_rows == 200
    assert loading.total_transfer_eta_seconds == 3

    assert tracker.record_consolidated_rows(logical_operation_id="copy-1", rows=50)
    assert not tracker.record_consolidated_rows(logical_operation_id="copy-1", rows=50)
    during_consolidation = tracker.snapshot(at=2)
    assert during_consolidation.remaining_consolidation_rows == 50
    assert during_consolidation.total_transfer_eta_seconds == 2.5

    consolidated = tracker.mark_consolidation_complete()
    assert consolidated.remaining_consolidation_rows == 0
    assert consolidated.total_transfer_eta_seconds == 2
    finalizing = tracker.mark_finalization_started()
    assert finalizing.remaining_load_rows == 0
    assert finalizing.remaining_consolidation_rows == 0
    assert finalizing.remaining_finalization_rows == 200
    assert finalizing.total_transfer_eta_seconds == 2
    finalized = tracker.mark_finalization_complete()
    assert finalized.remaining_finalization_rows == 0
    assert finalized.total_transfer_eta_seconds == 0


def test_zero_row_verification_and_full_retry_reset() -> None:
    clock = FakeClock()
    tracker = TransferProgressTracker(total_key_count=1, clock=clock)
    tracker.start_key("empty", started_at=0)
    assert not tracker.start_key("empty", started_at=0)
    tracker.materialize_key("empty", 0)
    zero = tracker.snapshot()
    assert zero.estimated_total_rows == 0
    assert zero.load_eta_seconds == 0
    assert zero.total_transfer_eta_seconds == 0

    clock.set(1.4)
    verified = tracker.verify_key("empty")
    assert verified is not None
    assert verified.expected_rows == 0
    assert verified.key_elapsed_seconds == 1.4
    assert verified.snapshot.verified_key_count == 1
    assert tracker.verify_key("empty") is None
    tracker.mark_loading_complete()

    clock.set(10)
    tracker.reset(attempt_number=2)
    reset = tracker.snapshot()
    assert reset.attempt_number == 2
    assert reset.attempt_elapsed_seconds == 0
    assert reset.committed_rows == 0
    assert reset.successful_batch_count == 0
    assert reset.known_key_count == 0
    assert reset.unknown_key_count == 1
    assert reset.eta_rows_per_second is None


def test_tracker_rejects_invalid_timing_counts_ownership_and_phase_order() -> None:
    with pytest.raises(ValueError, match="timing"):
        BatchTiming(2, 1, 3, 4, 10)
    with pytest.raises(ValueError, match="memory"):
        BatchTiming(1, 2, 3, 4, -1)
    with pytest.raises(ValueError, match="total_key_count"):
        TransferProgressTracker(total_key_count=True)
    with pytest.raises(ValueError, match="active_writers"):
        TransferProgressTracker(total_key_count=1, active_writers=0)
    with pytest.raises(ValueError, match="rolling_window"):
        TransferProgressTracker(total_key_count=1, rolling_window_seconds=0)

    tracker = TransferProgressTracker(total_key_count=1, clock=FakeClock())
    tracker.materialize_key("key", 1)
    assert not tracker.materialize_key("key", 1)
    with pytest.raises(ValueError, match="cannot change"):
        tracker.materialize_key("key", 2)
    tracker.assign_key("key", "writer-a")
    assert not tracker.assign_key("key", "writer-a")
    with pytest.raises(ValueError, match="cannot move"):
        tracker.assign_key("key", "writer-b")
    with pytest.raises(ValueError, match="does not own"):
        tracker.commit_batch(
            logical_batch_id=1,
            key_id="key",
            batch_index=1,
            rows=1,
            timing=make_timing(0, 1),
            writer_id="writer-b",
        )
    with pytest.raises(RuntimeError, match="before exactly"):
        tracker.verify_key("key")
    with pytest.raises(RuntimeError, match="before loading"):
        tracker.mark_consolidation_complete()
    with pytest.raises(RuntimeError, match="before consolidation"):
        tracker.mark_finalization_started()
    with pytest.raises(RuntimeError, match="before it starts"):
        tracker.mark_finalization_complete()


def test_exact_counts_verification_and_loading_guards() -> None:
    tracker = TransferProgressTracker(total_key_count=2, clock=FakeClock())
    with pytest.raises(RuntimeError, match="source count"):
        tracker.verify_key("missing")
    tracker.materialize_key("one", 2)
    with pytest.raises(RuntimeError, match="counts are unknown"):
        tracker.mark_loading_complete()
    tracker.commit_batch(
        logical_batch_id=1,
        key_id="one",
        batch_index=1,
        rows=1,
        timing=make_timing(0, 1),
    )
    with pytest.raises(RuntimeError, match="exactly"):
        tracker.verify_key("one")
    with pytest.raises(ValueError, match="exceed"):
        tracker.commit_batch(
            logical_batch_id=2,
            key_id="one",
            batch_index=2,
            rows=2,
            timing=make_timing(1, 2),
        )
    tracker.commit_batch(
        logical_batch_id=3,
        key_id="one",
        batch_index=2,
        rows=1,
        timing=make_timing(1, 2),
    )
    assert tracker.verify_key("one") is not None
    tracker.materialize_key("two", 0)
    tracker.verify_key("two")
    tracker.mark_loading_complete()
    with pytest.raises(RuntimeError, match="after loading"):
        tracker.commit_batch(
            logical_batch_id=4,
            key_id="one",
            batch_index=3,
            rows=1,
            timing=make_timing(2, 3),
        )
    assert tracker.record_consolidated_rows(logical_operation_id="copy", rows=0)
    with pytest.raises(LogicalBatchConflictError, match="different rows"):
        tracker.record_consolidated_rows(logical_operation_id="copy", rows=1)
    tracker.mark_consolidation_complete()
    with pytest.raises(RuntimeError, match="after it is complete"):
        tracker.record_consolidated_rows(logical_operation_id="new-copy", rows=1)
