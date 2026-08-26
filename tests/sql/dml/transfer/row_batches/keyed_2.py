from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    SimpleNamespace,
    attempt_module,
    keyed_module,
    keyed_pipeline_module,
    make_keyed_options,
    make_progress_options,
    models_module,
    pd,
    pytest,
    replace,
    threading,
    transfer_logging_module,
)


def test_keyed_pipeline_internal_guards_and_stage_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = models_module.TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="source",
        target_table="sandbox.target",
        validate_row_count=True,
    )
    state = keyed_pipeline_module.PipelineState()
    first_error = RuntimeError("first")
    state.fail(first_error)
    state.fail(RuntimeError("second"))
    assert state.first_error is first_error

    controller = keyed_pipeline_module.AdaptiveBatchController(options)
    assert controller.current_size() == options.batch_size
    controller.update(1.0, 10, None)

    with pytest.raises(RuntimeError, match="cancelled while enqueueing"):
        keyed_pipeline_module._put_until_accepted(keyed_pipeline_module.Queue(), object(), state)

    class FullOnceQueue:
        def __init__(self) -> None:
            self.calls = 0

        def put(self, _item: object, timeout: float) -> None:
            assert timeout == 0.1
            self.calls += 1
            if self.calls == 1:
                raise keyed_pipeline_module.Full

    active_state = keyed_pipeline_module.PipelineState()
    queue = FullOnceQueue()
    keyed_pipeline_module._put_until_accepted(queue, object(), active_state)
    assert queue.calls == 2

    mismatch_state = keyed_pipeline_module.PipelineState(expected_rows={0: 2}, staged_rows={0: 1})
    with pytest.raises(ValueError, match="row-count mismatch"):
        keyed_pipeline_module._publish_row_counts(
            options, models_module.TransferStageState(target_exists=False), mismatch_state
        )

    item = models_module.QueuedTransferBatch(
        0, 1, 1, None, 1, 1, models_module.RowBatch(["id"], [(1,)])
    )
    parquet_state = models_module.TransferStageState(
        target_exists=False,
        stage_external_location="memory://stage",
    )
    monkeypatch.setattr(keyed_pipeline_module, "parquet_row_group_size", lambda _o: 10)
    monkeypatch.setattr(keyed_pipeline_module, "write_batch_to_parquet_stage", lambda *_a, **_k: 1)
    assert (
        keyed_pipeline_module._stage_batch(
            options,
            parquet_state,
            {},
            item,
            0,
            1,
            (object(), object(), object()),
            None,
        )
        == 1
    )
    parquet_state.stage_external_location = None
    with pytest.raises(RuntimeError, match="external location"):
        keyed_pipeline_module._stage_batch(
            options,
            parquet_state,
            {},
            item,
            0,
            1,
            (object(), object(), object()),
            None,
        )
    with pytest.raises(RuntimeError, match="writer stage table"):
        keyed_pipeline_module._stage_batch(
            options,
            models_module.TransferStageState(target_exists=False),
            {},
            item,
            0,
            1,
            None,
            None,
        )

    sizing = SimpleNamespace(initial_size=10, min_size=1, max_size=20)
    monkeypatch.setattr(
        keyed_pipeline_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(transfer_insert_page_sizing=lambda **_kwargs: sizing),
    )
    assert keyed_pipeline_module._build_gp_insert_sizer(options).current_size == 10

    matching_state = keyed_pipeline_module.PipelineState(expected_rows={0: 1}, staged_rows={0: 1})
    matching_stage = models_module.TransferStageState(target_exists=False)
    keyed_pipeline_module._publish_row_counts(options, matching_stage, matching_state)
    assert matching_stage.expected_source_rows == 1
    keyed_pipeline_module._log_slice_complete_locked(
        keyed_pipeline_module.PipelineState(), 0, None, 1, 1
    )

    parquet_options = replace(options, trino_mode="parquet")
    parquet_queue = keyed_pipeline_module.Queue()
    parquet_queue.put(keyed_pipeline_module._STOP)
    monkeypatch.setattr(
        keyed_pipeline_module,
        "ensure_parquet_staging_dependencies",
        lambda: (object(), object(), object()),
    )
    monkeypatch.setattr(
        keyed_pipeline_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(transfer_insert_page_sizing=lambda **_kwargs: None),
    )
    keyed_pipeline_module._writer_impl(
        0,
        1,
        parquet_options,
        parquet_state,
        parquet_queue,
        keyed_pipeline_module.PipelineState(),
        controller,
        1,
    )

    drain_queue = keyed_pipeline_module.Queue()
    drain_queue.put(object())
    drain_queue.put(keyed_pipeline_module._STOP)
    monkeypatch.setattr(
        keyed_pipeline_module,
        "_writer_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("startup")),
    )
    with pytest.raises(RuntimeError, match="startup"):
        keyed_pipeline_module._writer(
            0,
            1,
            options,
            parquet_state,
            drain_queue,
            keyed_pipeline_module.PipelineState(),
            controller,
            1,
        )


def test_keyed_pipeline_runs_independent_reader_and_writer_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slices = [
        models_module.TransferSlice(index, (index,), "", f"slice-{index}", f"key={index}")
        for index in range(2)
    ]
    options = models_module.TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="source",
        target_table="sandbox.target",
        transfer_slices=slices,
        transfer_keys=["key"],
        validate_row_count=False,
        adaptive_batch_size=False,
        transfer_concurrency=models_module.TransferConcurrency(None, 2, 2, 2, 2, True),
    )
    shared_state = models_module.TransferStageState(
        target_exists=False,
        source_columns=["id"],
        source_column_types={"id": "integer"},
    )
    writer_states = [
        SimpleNamespace(
            stage_state=models_module.TransferStageState(
                target_exists=False,
                stage_table=f"stage_{index}",
                stage_column_types={"id": "INTEGER"},
            )
        )
        for index in range(2)
    ]
    read_barrier = threading.Barrier(2)
    write_barrier = threading.Barrier(2)
    staged: list[tuple[str, int]] = []

    class Connection:
        def close(self) -> None:
            return None

    def source_batches(*_args: Any, **_kwargs: Any) -> Any:
        read_barrier.wait(timeout=2)
        yield models_module.RowBatch(["id"], [(1,), (2,)])

    def insert_batch(
        _backend: str,
        _target_ref: dict[str, Any],
        stage_table: str,
        _columns: list[str],
        rows: list[tuple[Any, ...]],
        **_kwargs: Any,
    ) -> int:
        write_barrier.wait(timeout=2)
        staged.append((stage_table, len(rows)))
        return len(rows)

    monkeypatch.setattr(keyed_pipeline_module, "get_sql_connection", lambda _key: Connection())
    monkeypatch.setattr(keyed_pipeline_module, "iter_source_batches", source_batches)
    monkeypatch.setattr(keyed_pipeline_module, "insert_rows_batch", insert_batch)
    monkeypatch.setattr(
        keyed_pipeline_module,
        "cleanup_sources_and_close",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        keyed_pipeline_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            normalize_transfer_source_batch=lambda batch, _types: batch,
            transfer_insert_page_sizing=lambda **_kwargs: None,
        ),
    )
    monkeypatch.setattr(keyed_pipeline_module, "time_print", lambda *_args, **_kwargs: None)

    total = keyed_pipeline_module.run_keyed_transfer_pipeline(
        options=options,
        stage_state=shared_state,
        writer_stage_states=writer_states,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total == 4
    assert sorted(staged) == [("stage_0", 2), ("stage_1", 2)]
    assert [item.streamed_rows for item in shared_state.slice_counts] == [2, 2]

    monkeypatch.setattr(attempt_module, "run_keyed_transfer_pipeline", lambda **_kwargs: 9)
    assert (
        attempt_module.load_keyed_stage_slices(
            options=options,
            stage_state=shared_state,
            worker_stage_states=writer_states,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )
        == 9
    )


def test_keyed_pipeline_worker_guards_and_coordinator_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_slice = models_module.TransferSlice(0, (0,), "", "slice-0", "key=0")
    options = models_module.TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="source",
        target_table="sandbox.target",
        transfer_slices=[transfer_slice],
        transfer_keys=["key"],
        validate_row_count=False,
        adaptive_batch_size=False,
    )
    stage = models_module.TransferStageState(
        target_exists=False,
        stage_table="stage_0",
        source_columns=["id"],
        source_column_types={"id": "integer"},
    )
    writer = SimpleNamespace(stage_state=stage)

    class Connection:
        def close(self) -> None:
            return None

    monkeypatch.setattr(keyed_pipeline_module, "get_sql_connection", lambda _key: Connection())
    monkeypatch.setattr(
        keyed_pipeline_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            normalize_transfer_source_batch=lambda batch, _types: batch,
            transfer_insert_page_sizing=lambda **_kwargs: None,
        ),
    )
    monkeypatch.setattr(
        keyed_pipeline_module,
        "cleanup_sources_and_close",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(keyed_pipeline_module, "time_print", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="stage count"):
        keyed_pipeline_module.run_keyed_transfer_pipeline(
            options=options,
            stage_state=stage,
            writer_stage_states=[],
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )

    queue = keyed_pipeline_module.Queue()
    queue.put(object())
    queue.put(keyed_pipeline_module._STOP)
    state = keyed_pipeline_module.PipelineState()
    controller = keyed_pipeline_module.AdaptiveBatchController(options)
    keyed_pipeline_module._writer_impl(0, 1, options, stage, queue, state, controller, 1)
    assert state.failed_batches == 1

    queue = keyed_pipeline_module.Queue()
    queue.put(
        models_module.QueuedTransferBatch(
            0, 1, 1, None, 1, 1, models_module.RowBatch(["id"], [(1,)])
        )
    )
    queue.put(keyed_pipeline_module._STOP)
    cancelled = keyed_pipeline_module.PipelineState()
    cancelled.cancellation.set()
    keyed_pipeline_module._writer_impl(0, 1, options, stage, queue, cancelled, controller, 1)

    monkeypatch.setattr(
        keyed_pipeline_module,
        "iter_source_batches",
        lambda *_args, **_kwargs: iter([models_module.RowBatch(["id"], [(1,)])]),
    )
    cancelled = keyed_pipeline_module.PipelineState()
    cancelled.cancellation.set()
    keyed_pipeline_module._reader(
        0,
        1,
        options,
        [transfer_slice],
        keyed_pipeline_module.Queue(),
        cancelled,
        controller,
        stage,
        1,
    )

    during_read = keyed_pipeline_module.PipelineState()

    def cancel_then_yield(*_args: Any, **_kwargs: Any) -> Any:
        during_read.cancellation.set()
        yield models_module.RowBatch(["id"], [(1,)])

    monkeypatch.setattr(keyed_pipeline_module, "iter_source_batches", cancel_then_yield)
    keyed_pipeline_module._reader(
        0,
        1,
        options,
        [transfer_slice],
        keyed_pipeline_module.Queue(),
        during_read,
        controller,
        stage,
        1,
    )

    monkeypatch.setattr(
        keyed_pipeline_module,
        "iter_source_batches",
        lambda *_args, **_kwargs: iter([models_module.RowBatch(["id"], [(1,)])]),
    )
    monkeypatch.setattr(keyed_pipeline_module, "insert_rows_batch", lambda *_a, **_k: 1)

    def stage_without_ack(state: Any, item: models_module.QueuedTransferBatch, rows: int) -> None:
        state.staged_rows[item.slice_index] = rows

    monkeypatch.setattr(keyed_pipeline_module, "_acknowledge", stage_without_ack)
    with pytest.raises(RuntimeError, match="acknowledge every queued batch"):
        keyed_pipeline_module.run_keyed_transfer_pipeline(
            options=options,
            stage_state=stage,
            writer_stage_states=[writer],
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )


def test_keyed_pipeline_writer_start_failure_drains_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_slice = models_module.TransferSlice(0, (0,), "", "slice-0", "key=0")
    options = models_module.TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="source",
        target_table="sandbox.target",
        transfer_slices=[transfer_slice],
        transfer_keys=["key"],
        validate_row_count=False,
        adaptive_batch_size=False,
    )
    stage = models_module.TransferStageState(
        target_exists=False, stage_table="stage_0", source_columns=["id"]
    )

    class Connection:
        def close(self) -> None:
            return None

    def connection(key: str) -> Connection:
        if key == "target":
            message = "writer connection failed"
            raise RuntimeError(message)
        return Connection()

    monkeypatch.setattr(keyed_pipeline_module, "get_sql_connection", connection)
    monkeypatch.setattr(
        keyed_pipeline_module,
        "cleanup_sources_and_close",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(keyed_pipeline_module, "time_print", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="writer connection failed"):
        keyed_pipeline_module.run_keyed_transfer_pipeline(
            options=options,
            stage_state=stage,
            writer_stage_states=[SimpleNamespace(stage_state=stage)],
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )


def test_keyed_slice_workers_use_filtered_sql_and_own_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(concurrency=2)
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table_created=True,
        first_non_empty_batch=pd.DataFrame(columns=["id", "event_date"]),
        stage_column_types={"id": "INTEGER", "event_date": "DATE"},
        stage_table="sandbox.target__stage__abcd1234__w00000",
        stage_tables=[
            "sandbox.target__stage__abcd1234__w00000",
            "sandbox.target__stage__abcd1234__w00001",
        ],
    )
    worker_stage_states = attempt_module.build_keyed_worker_stage_states(
        options=options,
        stage_state=stage_state,
    )
    opened_connections: list[tuple[str, str]] = []
    loaded: list[dict[str, Any]] = []

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        connection = FakeTransferConnection(f"{connection_key}-{len(opened_connections)}")
        opened_connections.append((connection_key, connection.name))
        return connection

    def fake_load_stage_batches(**kwargs: Any) -> int:
        loaded.append(
            {
                "source_sql": kwargs["options"].source_sql,
                "source_conn": kwargs["connection_refs"].source["connection"].name,
                "slice_index": kwargs["slice_index"],
                "transfer_key_label": kwargs["transfer_key_label"],
                "stage_table": kwargs["stage_state"].stage_table,
            }
        )
        return kwargs["slice_index"] + 1

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)

    total_rows = attempt_module.load_keyed_stage_slices(
        options=options,
        worker_stage_states=worker_stage_states,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 3
    loaded_by_slice = sorted(loaded, key=lambda item: item["slice_index"])
    assert [item["source_sql"] for item in loaded_by_slice] == [
        transfer_slice.source_sql for transfer_slice in options.transfer_slices
    ]
    assert [item["slice_index"] for item in loaded_by_slice] == [0, 1]
    assert [item["transfer_key_label"] for item in loaded_by_slice] == [
        "event_date='2025-01-01'",
        "event_date='2025-01-02'",
    ]
    assert [item["stage_table"] for item in loaded_by_slice] == [
        "sandbox.target__stage__abcd1234__w00000",
        "sandbox.target__stage__abcd1234__w00001",
    ]
    assert opened_connections == [
        ("source_db", "source_db-0"),
        ("source_db", "source_db-1"),
    ]
    assert loaded_by_slice[0]["source_conn"] != loaded_by_slice[1]["source_conn"]


def test_keyed_state_requires_stage_and_logging_handles_empty_keys() -> None:
    state = models_module.TransferStageState(target_exists=False)
    state.transfer_slices = []
    with pytest.raises(RuntimeError, match="stage table"):
        keyed_module.build_keyed_worker_stage_states(stage_state=state)
    options = make_progress_options(transfer_keys=[])
    transfer_slice = models_module.TransferSlice(0, (), "", "select 1", "slice-00000")
    assert transfer_logging_module.format_transfer_slice_log_label(options, transfer_slice) is None
    options = make_progress_options(transfer_keys=["id"])
    transfer_slice = models_module.TransferSlice(
        0, (None,), "id IS NULL", "select 1", "slice-00000"
    )
    assert (
        transfer_logging_module.format_transfer_slice_log_label(options, transfer_slice)
        == "id=NULL"
    )
