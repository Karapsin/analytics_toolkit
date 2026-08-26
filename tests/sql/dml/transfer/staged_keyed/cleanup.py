from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    BoundedConnectionManager,
    DropReady,
    KeyReadComplete,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    RowBatch,
    SimpleNamespace,
    _concurrency,
    _LeaseManager,
    _metadata,
    _options,
    _ready_task,
    build_ch_shard_table_name,
    load_stage,
    pytest,
    staged_keyed_pipeline,
)


def test_bounded_connection_manager_tracks_high_water_replacement_and_close() -> None:
    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        return connection

    manager = BoundedConnectionManager(
        "source",
        2,
        role="source test pool",
        open_connection=open_connection,
    )
    with manager.lease() as first_ref:
        with manager.lease() as second_ref:
            assert first_ref["connection"] is not second_ref["connection"]
            assert manager.high_water_mark == 2
        failed_connection = first_ref["connection"]
        manager.replace_connection("source", first_ref)
        assert failed_connection.close_count == 1
        assert first_ref["connection"] is opened[-1]
        assert manager.high_water_mark == 2

    manager.close()

    assert len(opened) == 3
    assert [connection.close_count for connection in opened] == [1, 1, 1]
    with pytest.raises(RuntimeError, match="manager is closed"), manager.lease():
        pass


def test_clickhouse_stage_cleanup_drops_every_policy_created_companion() -> None:
    commands: list[str] = []
    connection = SimpleNamespace(command=commands.append)
    policy = SimpleNamespace(
        create_distributed_pair=True,
        shard_on_cluster="STAGE_SHARDS",
        distributed_on_cluster="STAGE_DISTRIBUTED",
    )
    stage_table = "scratch.transfer_stage"

    load_stage.cleanup_stage_table(
        "ch",
        connection,
        stage_table,
        ch_creation_policy=policy,
    )

    shard_table = build_ch_shard_table_name(stage_table)
    assert len(commands) == 4
    assert any(f"DROP TABLE IF EXISTS {stage_table}" == sql for sql in commands)
    assert any(stage_table in sql and "STAGE_DISTRIBUTED" in sql for sql in commands)
    assert any(f"DROP TABLE IF EXISTS {shard_table}" == sql for sql in commands)
    assert any(shard_table in sql and "STAGE_SHARDS" in sql for sql in commands)


def test_drop_drain_drops_only_the_exact_acknowledged_source_stage(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    tasks = [
        _ready_task(slices[0], "source.acknowledged", 0),
        _ready_task(slices[1], "source.unverified", 0),
    ]
    for task in tasks:
        assert runtime.live_stage_credits.acquire(blocking=False)
        runtime.reserve_source_stage(task.source_stage)
        runtime.publish_source_stage(task)
    runtime.drop_ready.put_nowait(DropReady(tasks[0], None))
    dropped: list[ReadyKeyTask] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda _options, _source_ref, task: dropped.append(task),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    count = staged_keyed_pipeline._drain_drop_ready(
        options,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
        limit=None,
    )

    assert count == 1
    assert dropped == [tasks[0]]
    assert runtime.source_stage_tables == ["source.unverified"]
    assert runtime.source_stages_dropped == 1


def test_reader_processes_at_most_one_acknowledged_drop_between_batches(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=1)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 2)
    task.batch_size = 1
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    drain_limits: list[int | None] = []
    handoffs: list[Any] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_drain_drop_ready",
        lambda *_args, limit, **_kwargs: drain_limits.append(limit) or 0,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda *_args, **_kwargs: RowBatch(columns=["id"], rows=[(1,)]),
    )

    def handoff(_queue: Any, item: QueuedKeyBatch, _runtime: Any) -> None:
        handoffs.append(item)
        assert item.prefetch_slot is not None
        item.prefetch_slot.release()
        item.prefetch_slot = None

    monkeypatch.setattr(staged_keyed_pipeline, "_put_batch_with_cancellation", handoff)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_put_with_cancellation",
        lambda _queue, item, _runtime: handoffs.append(item),
    )

    staged_keyed_pipeline._stream_ready_key(
        options,
        _metadata(),
        task,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
    )

    assert drain_limits == [1, 1, 1]
    assert [item.batch_index for item in handoffs[:-1]] == [1, 2]
    assert isinstance(handoffs[-1], KeyReadComplete)
