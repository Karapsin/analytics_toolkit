from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pytest
from analytics_toolkit.sql.dml.transfer.flow import staged_unkeyed_progress
from analytics_toolkit.sql.dml.transfer.flow.logging import (
    TransferAttemptLogState,
    build_transfer_operation_context,
    format_transfer_slice_log_label,
    staged_pipeline_start_message,
    staged_reader_slice_message,
    staged_writer_key_message,
    transfer_retry_message,
)
from analytics_toolkit.sql.dml.transfer.flow.staged_unkeyed_progress import (
    UnkeyedStagedProgress,
)
from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import (
    BatchTiming,
    TransferProgressTracker,
    format_slice_tag,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferOptions,
    TransferSlice,
)


@dataclass
class MutableClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


class RecordingProgressBar:
    def __init__(self) -> None:
        self.reset_calls = 0
        self.close_calls = 0

    def update(self, _rows: int) -> None:
        return

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
        "source_sql": "SELECT secret_column FROM source_table",
        "target_table": "public.target_table",
        "source_transfer_staging_schema": "source_stage",
        "transfer_staging_schema": "target_stage",
        "write_mode": "replace",
        "attempt_number": 1,
    }
    values.update(overrides)
    return TransferOptions(**values)


def _timing(
    started_at: float,
    completed_at: float,
    *,
    memory_bytes: int = 10,
) -> BatchTiming:
    midpoint = started_at + (completed_at - started_at) / 2
    return BatchTiming(
        read_started_at=started_at,
        read_completed_at=midpoint,
        queued_at=midpoint,
        insert_completed_at=completed_at,
        approximate_memory_bytes=memory_bytes,
    )


def _batch(rows: int) -> RowBatch:
    return RowBatch(["id"], [(value,) for value in range(rows)])


def test_slice_tag_formats_float_edges_tokens_and_control_characters() -> None:
    tag = format_slice_tag(
        1,
        1,
        [
            ("finite", 1.25),
            ("nan", math.nan),
            ("positive", math.inf),
            ("negative", -math.inf),
            ("jwt", "abcdefgh.abcdefgh.abcdefgh"),
            ("text", "slash\\cr\rtab\tctrl\x01"),
        ],
    )

    assert "finite:1.25" in tag
    assert "nan:NaN" in tag
    assert "positive:Infinity" in tag
    assert "negative:-Infinity" in tag
    assert "jwt:<redacted>" in tag
    assert "text:'slash\\\\cr\\rtab\\tctrl\\u0001'" in tag


def test_batch_timing_and_tracker_constructor_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        BatchTiming(math.nan, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="consolidation_enabled"):
        TransferProgressTracker(total_key_count=1, consolidation_enabled=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="allow_split_key_writers"):
        TransferProgressTracker(total_key_count=1, allow_split_key_writers=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="primary_writer_id must be hashable"):
        TransferProgressTracker(total_key_count=1, primary_writer_id=[])  # type: ignore[arg-type]


def test_tracker_reset_and_timestamp_validation_guards() -> None:
    clock = MutableClock(1)
    progress_bar = RecordingProgressBar()
    tracker = TransferProgressTracker(
        total_key_count=1,
        clock=clock,
        progress_bar=progress_bar,
    )

    tracker.reset()
    assert tracker.snapshot(at=1).attempt_number == 2
    assert progress_bar.reset_calls == 1
    with pytest.raises(ValueError, match="started_at must be finite"):
        tracker.reset(started_at=math.nan)

    tracker = TransferProgressTracker(total_key_count=1, clock=clock)
    with pytest.raises(ValueError, match="started_at must be finite"):
        tracker.start_key("key", started_at=math.inf)
    with pytest.raises(ValueError, match="started_at must be finite"):
        tracker.materialize_key("key", 0, started_at=-math.inf)


def test_materialization_and_assignment_limits_and_preassignment_accounting() -> None:
    tracker = TransferProgressTracker(total_key_count=2, active_writers=2)
    assert tracker.assign_key("primary", 0)
    assert tracker.assign_key("secondary", 1)
    assert tracker.materialize_key("primary", 2)
    assert tracker.materialize_key("secondary", 4)

    snapshot = tracker.snapshot()
    assert snapshot.remaining_consolidation_rows == 4
    tracker.set_primary_writer(1)
    assert tracker.snapshot().remaining_consolidation_rows == 2

    with pytest.raises(ValueError, match="Materialized key count exceeds"):
        tracker.materialize_key("extra", 0)
    with pytest.raises(ValueError, match="Assigned key count exceeds"):
        tracker.assign_key("extra", 0)


def test_materialization_rejects_rows_below_internal_committed_invariant() -> None:
    tracker = TransferProgressTracker(total_key_count=1)
    tracker._key_committed_rows["key"] = 2

    with pytest.raises(ValueError, match="below rows already committed"):
        tracker.materialize_key("key", 1)


def test_primary_writer_recalculation_handles_known_and_unknown_assignments() -> None:
    tracker = TransferProgressTracker(total_key_count=2, active_writers=2)
    tracker.assign_key("unknown", 0)
    tracker.materialize_key("known", 10)
    tracker.assign_key("known", 1)

    tracker.set_primary_writer(1)
    assert tracker.snapshot().remaining_consolidation_rows == 0
    tracker.set_primary_writer(1)


def test_commit_guards_unknown_keys_and_automatic_assignment_limits() -> None:
    tracker = TransferProgressTracker(total_key_count=1)
    with pytest.raises(RuntimeError, match="source key count is known"):
        tracker.commit_batch(
            logical_batch_id="unknown",
            key_id="unknown",
            batch_index=1,
            rows=1,
            timing=_timing(0, 1),
        )

    tracker.materialize_key("actual", 1)
    tracker.assign_key("decoy", 0)
    with pytest.raises(ValueError, match="Assigned key count exceeds"):
        tracker.commit_batch(
            logical_batch_id="actual",
            key_id="actual",
            batch_index=1,
            rows=1,
            timing=_timing(0, 1),
            writer_id=1,
        )


def test_non_primary_writer_is_assigned_on_first_commit() -> None:
    tracker = TransferProgressTracker(total_key_count=1, active_writers=2)
    tracker.materialize_key("key", 1)

    progress = tracker.commit_batch(
        logical_batch_id="batch",
        key_id="key",
        batch_index=1,
        rows=1,
        timing=_timing(0, 1),
        writer_id=1,
    )

    assert progress is not None
    assert progress.snapshot.remaining_consolidation_rows == 1


def test_tracker_rejects_nonfinite_verification_snapshot_and_clock() -> None:
    tracker = TransferProgressTracker(total_key_count=1)
    tracker.materialize_key("key", 0)
    with pytest.raises(ValueError, match="verified_at must be finite"):
        tracker.verify_key("key", verified_at=math.nan)
    with pytest.raises(ValueError, match="Snapshot time must be finite"):
        tracker.snapshot(at=math.inf)

    clock = MutableClock(0)
    clock_tracker = TransferProgressTracker(total_key_count=0, clock=clock)
    clock.value = math.nan
    with pytest.raises(ValueError, match="Clock value must be finite"):
        clock_tracker.mark_loading_complete()


def test_loading_requires_all_materialized_rows_to_be_committed() -> None:
    tracker = TransferProgressTracker(total_key_count=1)
    tracker.materialize_key("key", 2)

    with pytest.raises(RuntimeError, match="every source row"):
        tracker.mark_loading_complete()


def test_equal_completion_timestamps_leave_rolling_rate_unavailable() -> None:
    tracker = TransferProgressTracker(total_key_count=1)
    tracker.materialize_key("key", 2)
    tracker.commit_batch(
        logical_batch_id=1,
        key_id="key",
        batch_index=1,
        rows=1,
        timing=_timing(0, 0),
    )
    second = tracker.commit_batch(
        logical_batch_id=2,
        key_id="key",
        batch_index=2,
        rows=1,
        timing=_timing(0, 0),
    )

    assert second is not None
    assert second.snapshot.rolling_rows_per_second is None
    assert second.snapshot.eta_rows_per_second is None


def test_split_writer_fraction_ignores_zero_only_stage_accounting() -> None:
    tracker = TransferProgressTracker(
        total_key_count=1,
        active_writers=2,
        allow_split_key_writers=True,
    )
    tracker.materialize_key("key", 10)
    tracker._writer_stage_rows[0] = 0

    assert tracker.snapshot().remaining_consolidation_rows == 5


def test_unkeyed_progress_validation_duplicate_and_snapshot_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="attempt_started_at must be finite"):
        UnkeyedStagedProgress(
            _options(),
            total_rows=0,
            worker_count=1,
            attempt_started_at=math.inf,
            progress_bar=RecordingProgressBar(),
        )

    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=0,
        worker_count=1,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=MutableClock(),
    )
    assert progress.snapshot().committed_rows == 0
    monkeypatch.setattr(progress._tracker, "commit_batch", lambda **_kwargs: None)
    with pytest.raises(RuntimeError, match="committed twice"):
        progress.commit_batch(
            logical_batch_id=(0, 1, 2),
            worker_id=0,
            batch=_batch(1),
            read_started_at=0,
            read_completed_at=0,
            insert_completed_at=0,
        )


def test_unkeyed_progress_rejects_duplicate_verification() -> None:
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=0,
        worker_count=1,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=MutableClock(),
    )

    progress.mark_loading_complete()
    with pytest.raises(RuntimeError, match="verified twice"):
        progress.mark_loading_complete()


def test_unkeyed_finalization_requires_wrapper_start_timestamp() -> None:
    progress = UnkeyedStagedProgress(
        _options(),
        total_rows=0,
        worker_count=1,
        attempt_started_at=0,
        progress_bar=RecordingProgressBar(),
        clock=MutableClock(),
    )
    progress.mark_loading_complete()
    progress.mark_consolidation_complete(stage_count=0, copied_rows=0, elapsed_seconds=0)
    progress._tracker.mark_finalization_started()

    with pytest.raises(RuntimeError, match="did not record its start time"):
        progress.mark_finalization_complete()


def test_unkeyed_private_eta_and_exception_attachment_fallbacks() -> None:
    assert staged_unkeyed_progress._eta_seconds(None, 1) is None
    assert staged_unkeyed_progress._eta_seconds(1, None) is None
    assert staged_unkeyed_progress._eta_seconds(0, None) == 0

    staged_unkeyed_progress._attach_attempt_summary(  # type: ignore[arg-type]
        object(),
        phase="source-stage loading",
        committed_rows=1,
        elapsed_seconds=2,
    )


def test_retry_logging_sanitizes_nonfinite_and_negative_summary_fields() -> None:
    for elapsed in (math.nan, math.inf, -1, True, "secret-duration"):
        message = transfer_retry_message(
            2,
            5,
            {
                "phase": "password=hunter2",
                "committed_rows": True,
                "elapsed_seconds": elapsed,
            },
        )
        assert "hunter2" not in message
        assert "after transfer failure" in message
        assert "staged 0 rows in 0.0 seconds" in message


def test_attempt_log_state_keeps_only_dictionary_summaries() -> None:
    state = TransferAttemptLogState()
    invalid = RuntimeError("secret failure")
    invalid.analytics_toolkit_transfer_attempt_summary = "secret summary"
    with pytest.raises(RuntimeError, match="secret failure"):
        state.run(lambda: (_ for _ in ()).throw(invalid))
    assert state.previous_summary is None

    valid = RuntimeError("failure")
    valid.analytics_toolkit_transfer_attempt_summary = {
        "phase": "destination finalization",
        "committed_rows": 3,
        "elapsed_seconds": 2,
    }
    with pytest.raises(RuntimeError, match="failure"):
        state.run(lambda: (_ for _ in ()).throw(valid))
    assert "after destination finalization" in state.retry_message(2, 5)


def test_operation_context_redacts_keyed_staged_source_sql() -> None:
    staged = _options(transfer_slices=[])
    assert build_transfer_operation_context(staged, 2).sql_preview is None

    unstaged = _options(source_transfer_staging_schema=None, transfer_slices=None)
    context = build_transfer_operation_context(unstaged, 2)
    assert context.sql_preview == "SELECT secret_column FROM source_table"


def test_logging_helpers_cover_unkeyed_and_keyed_message_shapes() -> None:
    transfer_slice = TransferSlice(0, (), "", "SELECT 1", "slice-00000")
    assert format_transfer_slice_log_label(_options(transfer_keys=None), transfer_slice) is None
    assert (
        staged_pipeline_start_message(2, 1, 2)
        == "Starting keyed source-staging pipeline: 2 slice(s), 1 source-stage reader(s), "
        "2 target writer(s); ready keys flow directly to target writers with no global stage "
        "barrier"
    )
    assert staged_reader_slice_message(0, 2, 1, 3, None, "created", "stage_a") == (
        "Source-stage reader 1/2 created slice 1/3 stage_a"
    )
    assert staged_reader_slice_message(1, 2, 2, 3, "id=2", "dropped", "stage_b") == (
        "Source-stage reader 2/2 dropped slice 2/3 for id=2 stage_b"
    )
    assert staged_writer_key_message(0, 1, 0, 3, None, "claimed", "stage_a") == (
        "Target writer 1/1 claimed whole slice 1/3 stage_a"
    )
    assert staged_writer_key_message(0, 1, 1, 3, "id=2", "verified", "stage_a") == (
        "Target writer 1/1 verified whole slice 2/3 for id=2 stage_a"
    )
