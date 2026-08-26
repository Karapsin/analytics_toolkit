from __future__ import annotations

from tests.sql._support.transfer_ordinal import (
    AdaptiveRangeScheduler,
    Any,
    OrdinalRange,
    SimpleNamespace,
    TransferSlice,
    TransferStageState,
    _staged_options,
    api,
    pytest,
    resolve_internal_columns,
    staged_attempt,
)


def test_adaptive_ranges_cover_slices_once_after_split_retry() -> None:
    scheduler = AdaptiveRangeScheduler({0: 5, 1: 2, 2: 0})
    failed = scheduler.claim(0, 5)
    assert failed == OrdinalRange(0, 1, 6)
    scheduler.requeue_failed(0, failed, reduced_batch_size=2)

    while True:
        claimed = scheduler.claim(1, 10)
        if claimed is None:
            break
        scheduler.complete(1, claimed)

    scheduler.validate_complete()
    assert sum(item.row_count for item in scheduler.completed_ranges()) == 7


def test_materialize_snapshot_builds_all_slices_and_drops_partial_on_failure(
    monkeypatch: Any,
) -> None:
    slices = [
        TransferSlice(0, (1,), "id = 1", "SELECT 1 AS id", "one"),
        TransferSlice(1, (2,), "id = 2", "SELECT 2 AS id", "two"),
    ]
    options = _staged_options(transfer_slices=slices)
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    commands: list[str] = []
    dropped: list[str] = []
    adapter = SimpleNamespace(
        execute_command=lambda _connection, sql: commands.append(sql),
        drop_table=lambda _connection, table, **_kwargs: dropped.append(table),
    )
    monkeypatch.setattr(staged_attempt, "_allocate_snapshot_name", lambda *_args: "snap")
    monkeypatch.setattr(staged_attempt, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_attempt,
        "execute_transfer_materialization",
        lambda _adapter, _backend, _connection, sql: commands.append(sql),
    )
    monkeypatch.setattr(
        staged_attempt,
        "_snapshot_slice_counts",
        lambda *_args: {0: 1, 1: 1},
    )

    assert staged_attempt._materialize_snapshot(options, {"connection": object()}, state) == (
        "snap",
        {0: 1, 1: 1},
    )
    assert any(sql.startswith("INSERT INTO snap") for sql in commands)
    assert any(sql.startswith("CREATE INDEX") for sql in commands)

    calls = 0

    def fail_second(*_args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("slice insert failed")

    monkeypatch.setattr(staged_attempt, "execute_transfer_materialization", fail_second)
    with pytest.raises(OSError, match="slice insert failed"):
        staged_attempt._materialize_snapshot(options, {"connection": object()}, state)
    assert dropped == ["snap"]

    monkeypatch.setattr(
        staged_attempt,
        "execute_transfer_materialization",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        staged_attempt,
        "_snapshot_slice_counts",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt("count cancelled")),
    )

    def fail_drop(_connection: Any, table: str, **_kwargs: Any) -> None:
        dropped.append(table)
        raise RuntimeError("snapshot cleanup failed")

    adapter.drop_table = fail_drop
    dropped.clear()
    with pytest.raises(KeyboardInterrupt, match="count cancelled"):
        staged_attempt._materialize_snapshot(options, {"connection": object()}, state)
    assert dropped == ["snap"]

    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        staged_attempt._materialize_snapshot(
            _staged_options(source_transfer_staging_schema=None),
            {"connection": object()},
            TransferStageState(target_exists=True),
        )

    monkeypatch.setattr(
        staged_attempt,
        "build_snapshot_select_sql",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("render failed")),
    )
    dropped.clear()
    with pytest.raises(ValueError, match="render failed"):
        staged_attempt._materialize_snapshot(options, {"connection": object()}, state)
    assert not dropped


def test_transfer_does_not_full_retry_nonretryable_post_finalization_close(
    monkeypatch: Any,
) -> None:
    class FinalizedCloseError(RuntimeError):
        analytics_toolkit_sql_retry_safe = False

    options = _staged_options(
        transfer_slices=[TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")],
        transfer_keys=["key"],
        full_retry_cnt=5,
        full_timeout_increment=0,
    )
    attempts: list[int] = []
    error = FinalizedCloseError("target connection remains live")
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: options)

    def fail_attempt(**_kwargs: Any) -> int:
        attempts.append(1)
        raise error

    monkeypatch.setattr(api, "run_transfer_attempt", fail_attempt)

    with pytest.raises(FinalizedCloseError) as exc_info:
        api.transfer_table("source", "target")

    assert exc_info.value is error
    assert attempts == [1]


def test_unkeyed_target_open_failure_closes_partial_source_lease(monkeypatch: Any) -> None:
    active = {"source": 0, "target": 0}
    high_water = {"source": 0, "target": 0}
    closed: list[str] = []

    class Connection:
        def __init__(self, key: str) -> None:
            self.key = key

        def close(self) -> None:
            active[self.key] -= 1
            closed.append(self.key)

    def open_connection(key: str) -> Connection:
        if key == "target":
            raise OSError("target open failed")
        active[key] += 1
        high_water[key] = max(high_water[key], active[key])
        return Connection(key)

    monkeypatch.setattr(staged_attempt, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_state",
        lambda *_args: pytest.fail("state creation must not run after target open failure"),
    )

    with pytest.raises(OSError, match="target open failed"):
        staged_attempt.run_staged_source_transfer_attempt(
            _staged_options(),
            insert_retry_cnt=1,
        )

    assert active == {"source": 0, "target": 0}
    assert high_water == {"source": 1, "target": 0}
    assert closed == ["source"]

    with pytest.raises(OSError, match="target open failed"):
        staged_attempt._range_worker(
            _staged_options(),
            "source.snapshot",
            ["id"],
            TransferStageState(target_exists=True),
            "target.stage",
            SimpleNamespace(),
            0,
            1,
        )
    assert active == {"source": 0, "target": 0}
    assert high_water == {"source": 1, "target": 0}
    assert closed == ["source", "source"]
