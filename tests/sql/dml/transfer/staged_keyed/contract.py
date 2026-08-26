from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    BoundedConnectionManager,
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
    _state,
    _thread,
    ch_lifecycle,
    common_methods,
    pytest,
    row_counts,
    sql_log_context,
    staged_attempt,
    staged_keyed_io,
    staged_keyed_pipeline,
    threading,
    time_print,
    tracked_sql_operation,
)


def test_adaptive_keyed_stream_keeps_captured_prefetch_size_for_inflight_read(
    monkeypatch: Any,
) -> None:
    options = _options(
        transfer_concurrency=_concurrency(1, 1),
        batch_size=3,
        min_batch_size=1,
        max_batch_size=6,
        adaptive_batch_size=True,
    )
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 8)
    task.batch_size = 3
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    requested_ranges: list[tuple[int, int, int]] = []
    handed_off: list[QueuedKeyBatch] = []

    def read(
        _options: Any,
        _source_ref: Any,
        current_task: ReadyKeyTask,
        _metadata_value: Any,
        start: int,
        stop: int,
        **_kwargs: Any,
    ) -> RowBatch:
        captured_size = stop - start
        requested_ranges.append((start, stop, captured_size))
        if len(requested_ranges) == 2:
            # The writer adapts while this prefetch read is already in flight.
            # This batch must keep its captured size; only later reads use 1.
            current_task.batch_size = 1
        return RowBatch(["id"], [(ordinal,) for ordinal in range(start, stop)])

    def handoff(_queue: Any, item: QueuedKeyBatch, _runtime: Any) -> None:
        handed_off.append(item)
        staged_keyed_pipeline.release_queued_batch_slot(item)

    monkeypatch.setattr(staged_keyed_pipeline, "read_key_batch", read)
    monkeypatch.setattr(staged_keyed_pipeline, "_put_batch_with_cancellation", handoff)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_drain_drop_ready",
        lambda *_args, **_kwargs: 0,
    )

    staged_keyed_pipeline._stream_ready_key(
        options,
        _metadata(),
        task,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
    )

    assert requested_ranges == [
        (1, 4, 3),
        (4, 7, 3),
        (7, 8, 1),
        (8, 9, 1),
    ]
    assert [item.batch.row_count for item in handed_off] == [3, 3, 1, 1]
    assert all(
        item.batch.row_count == item.stop_ordinal - item.start_ordinal for item in handed_off
    )
    completion = task.batch_queue.get_nowait()
    assert isinstance(completion, KeyReadComplete)
    assert completion.streamed_rows == 8


def test_capacity_one_prefetch_waits_before_read_without_source_lease(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=1)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 1)
    task.batch_size = 1
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    occupied = KeyReadComplete(task, streamed_rows=0, batch_count=0)
    assert task.batch_slot.acquire(blocking=False)
    task.batch_queue.put_nowait(occupied)
    source_connections = _LeaseManager()
    read_called = threading.Event()

    def read(*_args: Any, **_kwargs: Any) -> RowBatch:
        assert source_connections.active == 1
        read_called.set()
        return RowBatch(["id"], [(1,)])

    monkeypatch.setattr(staged_keyed_pipeline, "read_key_batch", read)
    worker, errors = _thread(
        lambda: staged_keyed_pipeline._stream_ready_key(
            options,
            _metadata(),
            task,
            runtime,
            source_connections,  # type: ignore[arg-type]
        )
    )

    assert not read_called.wait(timeout=0.1)
    assert source_connections.active == 0
    assert worker.is_alive()
    assert task.batch_queue.get(timeout=1) is occupied
    task.batch_slot.release()
    assert read_called.wait(timeout=1)
    assert source_connections.released.wait(timeout=1)
    queued_batch = task.batch_queue.get(timeout=1)
    assert isinstance(queued_batch, QueuedKeyBatch)
    assert queued_batch.prefetch_slot is task.batch_slot
    queued_batch.prefetch_slot.release()
    queued_batch.prefetch_slot = None
    completion = task.batch_queue.get(timeout=1)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert isinstance(completion, KeyReadComplete)
    assert source_connections.high_water_mark == 1


def test_clickhouse_per_host_fallback_stays_inside_target_pool(monkeypatch: Any) -> None:
    active = 0
    high_water = 0
    opened_roles: list[str] = []
    state_lock = threading.Lock()

    class Connection:
        def __init__(self, role: str) -> None:
            nonlocal active, high_water
            self.role = role
            self.closed = False
            opened_roles.append(role)
            with state_lock:
                active += 1
                high_water = max(high_water, active)

        def command(self, _sql: str) -> None:
            return None

        def close(self) -> None:
            nonlocal active
            if self.closed:
                return
            self.closed = True
            with state_lock:
                active -= 1

    waits = 0

    def wait_for_absence(*_args: Any, **_kwargs: Any) -> None:
        nonlocal waits
        waits += 1
        if waits == 1:
            raise TimeoutError("cluster DDL remained visible")

    monkeypatch.setattr(
        ch_lifecycle,
        "_wait_for_ch_distributed_table_pair_absence",
        wait_for_absence,
    )
    monkeypatch.setattr(
        ch_lifecycle,
        "_query_ch_configured_cluster_hosts",
        lambda *_args, **_kwargs: ["host-a", "host-b", "host-c"],
    )
    monkeypatch.setattr(
        ch_lifecycle,
        "_select_ch_hosts_for_local_drop",
        lambda _connection, _pair, **kwargs: kwargs["configured_hosts"],
    )
    manager = BoundedConnectionManager(
        "target",
        1,
        role="target ClickHouse pool",
        open_connection=lambda _key: Connection("coordinator"),
    )

    ch_lifecycle.drop_ch_distributed_table_pair_bounded(
        "sandbox.target",
        "CORE",
        query_label=None,
        ch_retry_per_host_drops=True,
        connection_runner=lambda role, operation: manager.run(
            role,
            lambda ref: operation(ref["connection"]),
        ),
        host_connection_runner=lambda host, operation: manager.run_with_connection(
            "host cleanup",
            lambda: Connection(host),
            operation,
        ),
    )
    manager.close()

    assert high_water == 1
    assert active == 0
    assert {"host-a", "host-b", "host-c"}.issubset(opened_roles)
    assert manager.high_water_mark == 1


def test_consolidation_replaces_connections_through_bounded_target_pool(
    monkeypatch: Any,
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []
    inserted_with: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        staged_attempt,
        "insert_from_table",
        lambda _backend, connection, *_args, **_kwargs: inserted_with.append(connection),
    )
    manager = BoundedConnectionManager(
        "target",
        1,
        role="target consolidation pool",
        open_connection=open_connection,
    )
    with manager.lease() as target_ref:
        original = target_ref["connection"]
        staged_attempt._consolidate_worker_stages(
            _options(transfer_concurrency=_concurrency(1, 1)),
            target_ref,
            _state(),
            ["target.primary", "target.secondary"],
        )
        replacement = target_ref["connection"]

    assert original.close_count == 1
    assert replacement is not original
    assert inserted_with == [replacement]
    assert manager.high_water_mark == 1
    manager.close()
    assert [connection.close_count for connection in opened] == [1, 1]


def test_final_target_metadata_count_uses_bounded_target_runner(
    monkeypatch: Any,
) -> None:
    options = _options(collect_final_target_count=True)
    manager = _LeaseManager()
    counted_connections: list[Any] = []

    monkeypatch.setattr(
        row_counts,
        "count_table_rows",
        lambda _backend, connection, _table, **_kwargs: (
            counted_connections.append(connection) or 17
        ),
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "best_effort_transfer_target_count",
        lambda current_options, **kwargs: row_counts.best_effort_transfer_target_count(
            current_options,
            open_connection=lambda _key: pytest.fail("opened an unbudgeted connection"),
            count_rows=row_counts.count_table_rows,
            **kwargs,
        ),
    )

    staged_keyed_io.capture_final_target_count(
        options,
        manager,  # type: ignore[arg-type]
    )

    assert options.final_target_rows == 17
    assert counted_connections
    assert manager.lease_count == 1
    assert manager.high_water_mark == 1


def test_keyed_sql_log_context_prefixes_messages_and_suppresses_raw_sql(
    monkeypatch: Any,
) -> None:
    logs: list[str] = []
    secret_sql = "SELECT password FROM customer_secret"

    def fail(*_args: Any) -> None:
        raise RuntimeError("driver included row contents")

    adapter = SimpleNamespace(
        backend="gp",
        _read_columns_impl=fail,
    )
    monkeypatch.setattr(
        "analytics_toolkit.general.time_print",
        lambda message, **_kwargs: logs.append(message),
    )

    with pytest.raises(RuntimeError), sql_log_context(
        "[slice=1/1] ",
        suppress_sql=True,
    ):
        common_methods.read_columns(
            adapter,
            object(),
            secret_sql,
            print_queries=False,
            print_query=lambda *_args: None,
            read_dbapi_columns=object(),
        )

    assert logs
    assert all(message.startswith("[slice=1/1] ") for message in logs)
    assert logs[-1] == "[slice=1/1] Failed SQL (details suppressed)"
    assert secret_sql not in "\n".join(logs)


def test_keyed_sql_log_context_prefixes_nested_logs_and_hides_tracked_preview(
    capsys: Any,
) -> None:
    tag = "[slice=1/1]"
    secret_sql = "SELECT credential_secret FROM private_rows"

    with sql_log_context(f"{tag} ", suppress_sql=True):
        time_print("Nested stage message")
        with tracked_sql_operation(
            operation_name="keyed_stage",
            alias="target",
            backend="gp",
            phase="create_stage",
            preview_sql=secret_sql,
        ):
            pass

    output = capsys.readouterr().out
    relevant_lines = [
        line for line in output.splitlines() if "Nested stage message" in line or "SQL" in line
    ]
    assert relevant_lines
    assert all(tag in line for line in relevant_lines)
    assert secret_sql not in output
    assert "Finished SQL statement" not in output


def test_keyed_staged_attempt_guards_and_staged_attempt_delegation(monkeypatch: Any) -> None:
    with pytest.raises(ValueError, match="requires transfer slices"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            _options(transfer_slices=[]),
            insert_retry_cnt=1,
        )
    with pytest.raises(RuntimeError, match="runtime identity"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            _options(transfer_id=None),
            insert_retry_cnt=1,
        )

    delegated: list[int] = []

    def delegate(_options: Any, *, insert_retry_cnt: int) -> int:
        delegated.append(insert_retry_cnt)
        return 7

    monkeypatch.setattr(
        staged_attempt,
        "run_keyed_staged_source_transfer_attempt",
        delegate,
    )

    assert staged_attempt.run_staged_source_transfer_attempt(_options(), insert_retry_cnt=3) == 7
    assert delegated == [3]


def test_lazy_memory_target_is_shared_across_active_and_prefetched_batches() -> None:
    options = _options(
        transfer_concurrency=_concurrency(2, 2),
        target_batch_memory_bytes=4_000,
        min_batch_memory_bytes=2_000,
        max_batch_memory_bytes=8_000,
    )

    sizer = staged_keyed_pipeline._make_batch_sizer(options)

    assert sizer.target_memory_bytes == 1_000
    assert sizer.min_target_memory_bytes == 500
    assert sizer.max_target_memory_bytes == 2_000


def test_lazy_target_stage_uses_cached_staging_ddl_contract(monkeypatch: Any) -> None:
    policy = SimpleNamespace(create_distributed_pair=True)
    options = _options(
        staging_ddl_properties={"fillfactor": "80"},
        staging_ch_policy=policy,
    )
    captured: list[dict[str, Any]] = []

    def create(*_args: Any, **kwargs: Any) -> str:
        captured.append(kwargs)
        return "target_stage.writer_0"

    monkeypatch.setattr(staged_keyed_io, "create_stage_table", create)
    assert (
        staged_keyed_io.create_target_writer_stage(
            options,
            {"connection": object()},
            _metadata(),
            0,
        )
        == "target_stage.writer_0"
    )
    assert captured[0]["ddl_properties"] is options.staging_ddl_properties
    assert captured[0]["ch_creation_policy"] is policy


def test_materialize_source_key_runs_one_ctas_per_key_without_append(monkeypatch: Any) -> None:
    options = _options()
    metadata = _metadata()
    events: list[tuple[str, str]] = []
    counts = {0: 3, 1: 5}
    adapter = SimpleNamespace(
        execute_command=lambda _connection, sql: events.append(("post", sql)),
    )
    monkeypatch.setattr(staged_keyed_io, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_keyed_io,
        "build_snapshot_select_sql",
        lambda **kwargs: f"SELECT slice_{kwargs['slice_id']}",
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "build_source_snapshot_sql",
        lambda **kwargs: SimpleNamespace(
            create_sql=(
                f"CREATE TABLE {kwargs['snapshot_table']} AS {kwargs['snapshot_select_sql']}"
            ),
            post_create_sqls=(f"POST CREATE {kwargs['snapshot_table']}",),
        ),
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "execute_transfer_materialization",
        lambda _adapter, _backend, _connection, sql: events.append(("ctas", sql)),
    )

    def count(
        _options: Any,
        _connection: Any,
        table: str,
        slice_index: int,
        _metadata: Any,
    ) -> int:
        events.append(("count", table))
        return counts[slice_index]

    monkeypatch.setattr(staged_keyed_io, "count_source_slice", count)
    slices = options.transfer_slices or []

    results = [
        staged_keyed_io.materialize_source_key(
            options,
            {"connection": object()},
            metadata,
            transfer_slice,
            f"source_stage.key_{transfer_slice.index}",
        )
        for transfer_slice in slices
    ]

    assert results == [3, 5]
    ctas = [sql for kind, sql in events if kind == "ctas"]
    assert ctas == [
        "CREATE TABLE source_stage.key_0 AS SELECT slice_0",
        "CREATE TABLE source_stage.key_1 AS SELECT slice_1",
    ]
    assert all("INSERT INTO" not in sql for sql in ctas)
    assert [kind for kind, _value in events] == [
        "ctas",
        "post",
        "count",
        "ctas",
        "post",
        "count",
    ]
