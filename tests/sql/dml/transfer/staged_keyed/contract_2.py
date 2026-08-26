from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    BoundedConnectionManager,
    Callable,
    Iterator,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    RowBatch,
    SimpleNamespace,
    SourceColumn,
    TransferConnectionRefs,
    TransferOptions,
    TransferProgressTracker,
    TransferSlice,
    TransferStageState,
    VerifiedKey,
    _concurrency,
    _LeaseManager,
    _metadata,
    _options,
    _ProgressBar,
    _ready_task,
    _state,
    finalize,
    pd,
    pytest,
    row_counts,
    staged_keyed_io,
    staged_keyed_logging,
    staged_keyed_pipeline,
)


def test_mid_attempt_schema_drift_fails_cached_contract_without_refresh(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    state = TransferStageState(target_exists=False)
    inspections: list[str] = []
    metadata_uses: list[Any] = []
    materialized: list[int] = []
    validated: list[int] = []
    dropped: list[int] = []
    post_load_phases: list[str] = []

    def inspect(_backend: str, _connection: Any, sql: str) -> list[SourceColumn]:
        inspections.append(sql)
        return [SourceColumn("id", "bigint")]

    def materialize(
        _options: TransferOptions,
        _source_ref: Any,
        metadata: Any,
        transfer_slice: TransferSlice,
        _source_stage: str,
    ) -> int:
        metadata_uses.append(metadata)
        materialized.append(transfer_slice.index)
        return 1

    def read(
        _options: TransferOptions,
        _source_ref: Any,
        task: ReadyKeyTask,
        metadata: Any,
        _start: int,
        _stop: int,
        **_kwargs: Any,
    ) -> RowBatch:
        metadata_uses.append(metadata)
        columns = ["id"] if task.transfer_slice.index == 0 else ["renamed_id"]
        return RowBatch(columns, [(task.transfer_slice.index,)])

    def insert(
        _options: TransferOptions,
        _target_ref: Any,
        _stage: str,
        batch: QueuedKeyBatch,
        metadata: Any,
        **_kwargs: Any,
    ) -> int:
        metadata_uses.append(metadata)
        if tuple(batch.batch.columns) != metadata.source_columns:
            raise RuntimeError("later key is incompatible with cached schema contract")
        return batch.batch.row_count

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "BoundedConnectionManager",
        lambda *_args, **_kwargs: _LeaseManager(),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(staged_keyed_pipeline, "inspect_source_query_schema", inspect)
    monkeypatch.setattr(
        staged_keyed_pipeline, "cleanup_superseded_transfer_stages", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(staged_keyed_pipeline, "ensure_transfer_target_table", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda _options, _ref, slice_index: f"source.slice_{slice_index}",
    )
    monkeypatch.setattr(staged_keyed_pipeline, "materialize_source_key", materialize)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda *_args, **_kwargs: "target.writer_0",
    )
    monkeypatch.setattr(staged_keyed_pipeline, "read_key_batch", read)
    monkeypatch.setattr(staged_keyed_pipeline, "insert_target_batch", insert)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda _options, _ref, _metadata, task, *_args: validated.append(task.transfer_slice.index),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda _options, _ref, task: dropped.append(task.transfer_slice.index),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_validate_target_stages",
        lambda *_args: post_load_phases.append("aggregate validation"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda *_args, **_kwargs: post_load_phases.append("destination mutation"),
    )

    with pytest.raises(RuntimeError, match="incompatible with cached schema contract"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert inspections == ["SELECT 1 AS id"]
    assert len({id(metadata) for metadata in metadata_uses}) == 1
    assert materialized == [0, 1]
    assert validated == [0]
    assert 1 not in dropped
    assert "source.slice_1" in (state.source_stage_tables or [])
    assert post_load_phases == []


def test_prepare_keyed_attempt_caches_one_immutable_schema_contract(monkeypatch: Any) -> None:
    options = _options()
    refs = TransferConnectionRefs(
        source={"connection": object()},
        target={"connection": object()},
    )
    state = TransferStageState(target_exists=True)
    inspections: list[str] = []
    cleanups: list[str | None] = []
    targets: list[list[str]] = []

    def inspect(_backend: str, _connection: Any, sql: str) -> list[SourceColumn]:
        inspections.append(sql)
        return [SourceColumn("id", "bigint")]

    monkeypatch.setattr(staged_keyed_pipeline, "inspect_source_query_schema", inspect)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_superseded_transfer_stages",
        lambda **kwargs: cleanups.append(kwargs["staging_schema"]),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_with_internal_column_types",
        lambda types, *_args: {**types, "internal": "TEXT"},
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "ensure_transfer_target_table",
        lambda _options, _refs, _state, columns: targets.append(columns),
    )

    metadata = staged_keyed_pipeline._prepare_attempt(options, refs, state)

    assert inspections == ["SELECT 1 AS id"]
    assert metadata.source_columns == ("id",)
    assert dict(metadata.source_column_types) == {"id": "bigint"}
    assert dict(metadata.stage_column_types or {}) == {"id": "BIGINT", "internal": "TEXT"}
    assert state.source_columns == ["id"]
    assert cleanups == ["source_stage", "target_stage"]
    assert targets == [["id"]]
    with pytest.raises(TypeError):
        metadata.source_column_types["id"] = "changed"  # type: ignore[index]

    monkeypatch.setattr(staged_keyed_pipeline, "inspect_source_query_schema", lambda *_: [])
    with pytest.raises(ValueError, match="inspectable source schema"):
        staged_keyed_pipeline._prepare_attempt(options, refs, TransferStageState(True))


def test_prepare_keyed_attempt_honors_explicit_schema(monkeypatch: Any) -> None:
    options = _options(table_schema={"id": "INTEGER"})
    state = TransferStageState(target_exists=True)
    refs = TransferConnectionRefs(source={"connection": object()}, target={"connection": object()})
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("id", "bigint")],
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "cleanup_superseded_transfer_stages", lambda **_: None
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_table_schema_columns",
        lambda schema, columns: {columns[0]: schema[columns[0]]},
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "_with_internal_column_types", lambda value, *_: value
    )
    monkeypatch.setattr(staged_keyed_pipeline, "ensure_transfer_target_table", lambda *_: None)

    metadata = staged_keyed_pipeline._prepare_attempt(options, refs, state)

    assert dict(metadata.stage_column_types or {}) == {"id": "INTEGER"}


def test_row_count_and_finalization_accept_bounded_target_runner(monkeypatch: Any) -> None:
    options = _options(write_mode="append", validate_row_count=True)
    state = _state()
    state.expected_source_rows = 1
    state.stage_table = "target_stage.writer_0"
    state.stage_tables = [state.stage_table]
    state.first_non_empty_batch = pd.DataFrame({"id": [1]})
    state.insert_column_types = {"id": "BIGINT"}
    roles: list[str] = []
    connection = object()

    def run(role: str, operation: Callable[[dict[str, Any]], Any]) -> Any:
        roles.append(role)
        return operation({"connection": connection})

    monkeypatch.setattr(
        row_counts,
        "count_table_rows",
        lambda _backend, current, _table, **_kwargs: 1 if current is connection else -1,
    )
    row_counts.validate_loaded_stage_row_count(
        options=options,
        connection_refs=TransferConnectionRefs(),
        stage_state=state,
        total_rows=1,
        open_connection=lambda _key: pytest.fail("opened an unbudgeted row-count connection"),
        target_connection_runner=run,
    )

    monkeypatch.setattr(finalize, "validate_stage_uniqueness", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "validate_stage_target_key_overlap", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "finalize_stage_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(finalize, "analyze_table", lambda **_kwargs: None)
    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda *_args, **_kwargs: pytest.fail("opened an unbudgeted finalization connection"),
    )
    finalize.finalize_loaded_stage(
        options,
        TransferConnectionRefs(),
        state,
        1,
        target_connection_runner=run,
    )

    assert roles == [
        "validate_stage_row_count",
        "validate_stage",
        "validate_stage",
        "finalize_target",
        "analyze_target",
    ]


def test_runtime_bounds_capacity_one_prefetch_and_live_source_stages() -> None:
    slices = [
        TransferSlice(index, (index,), "", f"SELECT {index}", f"key={index}") for index in range(6)
    ]
    runtime = LazyKeyedRuntime(slices, read_workers=2, write_workers=3)

    assert runtime.ready.maxsize == 3
    assert [batch_queue.maxsize for batch_queue in runtime.writer_queues] == [1, 1, 1]
    assert runtime.live_stage_limit == 5
    assert [runtime.live_stage_credits.acquire(blocking=False) for _ in range(5)] == [
        True,
        True,
        True,
        True,
        True,
    ]
    assert runtime.live_stage_credits.acquire(blocking=False) is False

    task = _ready_task(slices[0], "source.exact", 0)
    runtime.reserve_source_stage(task.source_stage)
    runtime.publish_source_stage(task)
    runtime.mark_source_stage_dropped(task.source_stage)

    assert runtime.live_stage_credits.acquire(blocking=False) is True
    assert runtime.live_source_stage_count == 0


def test_slice_tag_uses_normalized_index_without_scanning_all_keys() -> None:
    class NonIterableSlices(list):
        def __iter__(self) -> Iterator[TransferSlice]:
            raise AssertionError("slice tag must not linearly scan transfer slices")

    options = _options()
    slices = list(options.transfer_slices or [])
    object.__setattr__(options, "transfer_slices", NonIterableSlices(slices))

    assert staged_keyed_logging.slice_tag(options, slices[1]).startswith("[slice=2/2 ")


def test_source_stage_name_allocation_handles_collisions(monkeypatch: Any) -> None:
    options = _options()
    monkeypatch.setattr(
        staged_keyed_io,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    existence = iter([True, False])
    monkeypatch.setattr(
        staged_keyed_io,
        "table_exists",
        lambda *_args, **_kwargs: next(existence),
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "collision_stage_suffix",
        lambda *_args: "collision",
    )

    assert (
        staged_keyed_io.allocate_source_stage_name(options, {"connection": object()}, 1)
        == "collision"
    )

    monkeypatch.setattr(staged_keyed_io, "table_exists", lambda *_args, **_: True)
    with pytest.raises(RuntimeError, match="unique source stage"):
        staged_keyed_io.allocate_source_stage_name(options, {"connection": object()}, 0)


def test_staged_key_batch_enforces_sql_and_rowbatch_row_limit(monkeypatch: Any) -> None:
    options = _options(retry_cnt=1, timeout_increment=0)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 3)
    queries: list[str] = []

    def over_return(_backend: str, _connection: Any, sql: str, **_kwargs: Any) -> Any:
        queries.append(sql)
        return SimpleNamespace(column_names=["id"], columns=[[1, 2, 3]])

    monkeypatch.setattr(staged_keyed_io, "_read_backend", over_return)

    with pytest.raises(RuntimeError, match="scheduled limit is 2"):
        staged_keyed_io.read_key_batch(
            options,
            {"connection": object()},
            task,
            _metadata(),
            1,
            3,
            batch_index=1,
        )

    assert len(queries) == 1
    assert queries[0].endswith('ORDER BY "__analytics_toolkit_row_ordinal" LIMIT 2')


def test_target_pool_bounds_every_keyed_target_phase(  # noqa: C901, PLR0915
    monkeypatch: Any,
) -> None:
    transfer_slice = TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")
    options = _options(
        transfer_slices=[transfer_slice],
        transfer_concurrency=_concurrency(1, 2),
    )
    state = _state()
    managers: dict[str, BoundedConnectionManager] = {}
    opened: list[Any] = []
    phases: list[str] = []

    class Connection:
        def __init__(self, key: str) -> None:
            self.key = key
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    def open_connection(key: str) -> Connection:
        connection = Connection(key)
        opened.append(connection)
        return connection

    class RecordingManager(BoundedConnectionManager):
        def __init__(self, key: str, capacity: int, *, role: str, **_kwargs: Any) -> None:
            super().__init__(
                key,
                capacity,
                role=role,
                open_connection=open_connection,
            )
            managers[role] = self

    def observe(phase: str, target_ref: dict[str, Any]) -> None:
        connection = target_ref["connection"]
        assert connection.key == "target"
        assert connection.close_count == 0
        phases.append(phase)

    def prepare(
        _options: Any,
        refs: TransferConnectionRefs,
        _stage_state: TransferStageState,
    ) -> Any:
        observe("metadata", refs.target)
        return _metadata()

    def run_workers(
        _options: Any,
        _metadata_value: Any,
        _stage_state: Any,
        runtime: LazyKeyedRuntime,
        _source_connections: Any,
        target_connections: BoundedConnectionManager,
        progress: TransferProgressTracker,
        **_kwargs: Any,
    ) -> None:
        target_connections.run(
            "per-key validation",
            lambda target_ref: observe("per-key validation", target_ref),
        )
        runtime.register_target_stage(0, "target_stage.writer_0")
        runtime.mark_verified(VerifiedKey(0, 0, 0, "target_stage.writer_0"))
        progress.start_key(0)
        progress.materialize_key(0, 0)
        progress.assign_key(0, 0)
        progress.verify_key(0)

    def validate_aggregate(
        _options: Any,
        target_ref: dict[str, Any],
        *_args: Any,
    ) -> None:
        observe("aggregate validation", target_ref)

    def validate_row_count(**kwargs: Any) -> None:
        kwargs["target_connection_runner"](
            "row-count validation",
            lambda target_ref: observe("row-count validation", target_ref),
        )

    def finalize(*_args: Any, **kwargs: Any) -> None:
        kwargs["target_connection_runner"](
            "finalization",
            lambda target_ref: observe("finalization", target_ref),
        )

    def cleanup(*_args: Any, **kwargs: Any) -> None:
        assert kwargs["safe_exception_logging"] is True
        kwargs["target_connection_runner"](
            "cleanup",
            lambda target_ref: observe("cleanup", target_ref),
        )

    monkeypatch.setattr(staged_keyed_pipeline, "BoundedConnectionManager", RecordingManager)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", prepare)
    monkeypatch.setattr(staged_keyed_pipeline, "_run_lazy_workers", run_workers)
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", validate_aggregate)
    monkeypatch.setattr(
        staged_keyed_pipeline, "validate_loaded_stage_row_count", validate_row_count
    )
    monkeypatch.setattr(staged_keyed_pipeline, "finalize_loaded_stage", finalize)
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", cleanup)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_loading_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_transfer_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    assert (
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )
        == 0
    )

    target_manager = managers["target transfer pool"]
    assert phases == [
        "metadata",
        "per-key validation",
        "aggregate validation",
        "row-count validation",
        "finalization",
        "cleanup",
    ]
    assert target_manager.capacity == 2
    assert target_manager.high_water_mark == 1
    target_connections = [connection for connection in opened if connection.key == "target"]
    assert len(target_connections) == 1
    assert target_connections[0].close_count == 1
