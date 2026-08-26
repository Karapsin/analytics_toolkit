from __future__ import annotations

from tests.sql._support.transfer_ordinal import (
    AdaptiveRangeScheduler,
    Any,
    OrdinalRange,
    RowBatch,
    TransferOptions,
    TransferStageState,
    _staged_options,
    assert_transfer_identity,
    pytest,
    resolve_internal_columns,
    stage_validation,
    staged_attempt,
)


def test_empty_slice_requires_no_target_stage_query() -> None:
    stage_validation.validate_transfer_stage_slice(
        options=_staged_options(),
        connection=object(),
        stage_table=[],
        internal_columns=resolve_internal_columns(["id"], "gp"),
        slice_id=3,
        expected_count=0,
        streamed_count=0,
    )


def test_internal_identity_quotes_and_rejects_mixed_runtime_values() -> None:
    internal = resolve_internal_columns([], "trino")
    assert all(value.startswith('"') for value in internal.quoted("trino"))
    assert_transfer_identity(
        expected_transfer_id="a",
        actual_transfer_id="a",
        expected_destination="target",
        actual_destination="target",
        resource="stage",
    )
    with pytest.raises(RuntimeError, match="transfer ID"):
        assert_transfer_identity(
            expected_transfer_id="a",
            actual_transfer_id="b",
            expected_destination="target",
            actual_destination="target",
            resource="stage",
        )
    with pytest.raises(RuntimeError, match="destination"):
        assert_transfer_identity(
            expected_transfer_id="a",
            actual_transfer_id="a",
            expected_destination="target",
            actual_destination="other",
            resource="stage",
        )


def test_range_scheduler_rejects_invalid_ownership_and_incomplete_coverage() -> None:
    for values in [(-1, 1, 2), (0, 0, 2), (0, 2, 2)]:
        with pytest.raises(ValueError):
            OrdinalRange(*values)
    with pytest.raises(ValueError, match="non-negative"):
        AdaptiveRangeScheduler({-1: 2})

    scheduler = AdaptiveRangeScheduler({0: 3})
    with pytest.raises(ValueError, match="worker_id"):
        scheduler.claim(-1, 1)
    with pytest.raises(ValueError, match="batch_size"):
        scheduler.claim(0, 0)
    first = scheduler.claim(0, 1)
    assert first is not None and not scheduler.finished
    with pytest.raises(RuntimeError, match="not claimed"):
        scheduler.complete(1, first)
    with pytest.raises(ValueError, match="reduced_batch_size"):
        scheduler.requeue_failed(0, first, reduced_batch_size=0)
    with pytest.raises(RuntimeError, match="not claimed"):
        scheduler.requeue_failed(1, first, reduced_batch_size=1)
    with pytest.raises(RuntimeError, match="incomplete ranges"):
        scheduler.validate_complete()
    scheduler.complete(0, first)
    second = scheduler.claim(0, 3)
    assert second is not None
    scheduler.complete(0, second)
    assert scheduler.finished

    gap = AdaptiveRangeScheduler({0: 2})
    gap._pending.clear()
    gap._completed.add(OrdinalRange(0, 2, 3))
    with pytest.raises(RuntimeError, match="gap or overlap"):
        gap.validate_complete()
    incomplete = AdaptiveRangeScheduler({0: 2})
    incomplete._pending.clear()
    with pytest.raises(RuntimeError, match="incomplete"):
        incomplete.validate_complete()


def test_range_worker_retries_only_failed_interval_and_validates_size(monkeypatch: Any) -> None:
    options = _staged_options()
    state = TransferStageState(
        target_exists=True,
        stage_column_types={"id": "BIGINT"},
    )
    connections = [object(), object(), object(), object()]
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: connections.pop(0))
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "rollback_quietly", lambda _connection: None)
    monkeypatch.setattr(staged_attempt, "replace_connection", lambda *_args: None)
    inserted: list[int] = []
    monkeypatch.setattr(
        staged_attempt,
        "insert_rows_batch",
        lambda *_args, **_kwargs: inserted.append(len(_args[4])),
    )
    attempts = 0

    def flaky_read(
        _options: TransferOptions,
        _connection: Any,
        _snapshot: str,
        _columns: list[str],
        _state: TransferStageState,
        claimed: OrdinalRange,
    ) -> RowBatch:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary source failure")
        return RowBatch(["id"], [(value,) for value in range(claimed.row_count)])

    monkeypatch.setattr(staged_attempt, "_read_snapshot_range", flaky_read)
    scheduler = AdaptiveRangeScheduler({0: 2})
    staged_attempt._range_worker(options, "snap", ["id"], state, "stage", scheduler, 0, 1)
    scheduler.validate_complete()
    assert inserted == [1, 1]

    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(
        staged_attempt,
        "_read_snapshot_range",
        lambda *_args: RowBatch(["id"], []),
    )
    with pytest.raises(RuntimeError, match="returned 0 row"):
        staged_attempt._range_worker(
            options,
            "snap",
            ["id"],
            state,
            "stage",
            AdaptiveRangeScheduler({0: 1}),
            0,
            1,
        )

    monkeypatch.setattr(
        staged_attempt,
        "_read_snapshot_range",
        lambda *_args: (_ for _ in ()).throw(OSError("terminal")),
    )
    with pytest.raises(OSError, match="terminal"):
        staged_attempt._range_worker(
            options,
            "snap",
            ["id"],
            state,
            "stage",
            AdaptiveRangeScheduler({0: 1}),
            0,
            1,
        )


def test_stage_slice_validation_rejects_in_memory_count_mismatch() -> None:
    with pytest.raises(RuntimeError, match="streamed"):
        stage_validation.validate_transfer_stage_slice(
            options=_staged_options(),
            connection=object(),
            stage_table="target_stage.writer_0",
            internal_columns=resolve_internal_columns(["id"], "gp"),
            slice_id=3,
            expected_count=2,
            streamed_count=1,
        )


def test_stage_validation_checks_user_payload_count(monkeypatch: Any) -> None:
    options = _staged_options()
    internal = resolve_internal_columns(["id"], "gp")
    monkeypatch.setattr(stage_validation, "count_table_rows", lambda *_args, **_kwargs: 2)
    stage_validation.validate_transfer_stage_identity(
        options=options,
        connection=object(),
        stage_tables=["stage"],
        internal_columns=internal,
        expected_slice_counts={0: 2, 1: 0},
    )
    monkeypatch.setattr(stage_validation, "count_table_rows", lambda *_args, **_kwargs: 1)
    with pytest.raises(RuntimeError, match="payload count"):
        stage_validation.validate_transfer_stage_identity(
            options=options,
            connection=object(),
            stage_tables=["stage"],
            internal_columns=internal,
            expected_slice_counts={0: 2},
        )


def test_staged_attempt_rejects_missing_identity_and_empty_schema(monkeypatch: Any) -> None:
    with pytest.raises(RuntimeError, match="runtime identity"):
        staged_attempt.run_staged_source_transfer_attempt(
            _staged_options(transfer_id=None),
            insert_retry_cnt=1,
        )

    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_state",
        lambda *_args: TransferStageState(target_exists=True),
    )
    monkeypatch.setattr(staged_attempt, "inspect_source_query_schema", lambda *_args: [])
    monkeypatch.setattr(staged_attempt, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)

    with pytest.raises(ValueError, match="inspectable source schema"):
        staged_attempt.run_staged_source_transfer_attempt(_staged_options(), insert_retry_cnt=1)
