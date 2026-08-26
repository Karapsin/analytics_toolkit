from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    BoundedConnectionCloseError,
    BoundedConnectionManager,
    LazyKeyedRuntime,
    SimpleNamespace,
    SqlTableReadinessError,
    TransferProgressTracker,
    TransferSlice,
    VerifiedKey,
    _concurrency,
    _LeaseManager,
    _metadata,
    _options,
    _ProgressBar,
    _ready_task,
    _state,
    load_stage,
    pd,
    pytest,
    staged_keyed_io,
    staged_keyed_pipeline,
)


def test_staged_key_batch_recovers_after_transient_replacement_open_failure(
    monkeypatch: Any,
) -> None:
    options = _options(retry_cnt=2, timeout_increment=0)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    opened: list[Any] = []
    open_attempts = 0
    reads = 0

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    def open_connection(_key: str) -> Connection:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 2:
            raise OSError("temporary connection open failure")
        connection = Connection()
        opened.append(connection)
        return connection

    def read(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("connection lost")
        return SimpleNamespace(column_names=["id"], columns=[[1]])

    monkeypatch.setattr(staged_keyed_io, "_read_backend", read)
    manager = BoundedConnectionManager(
        "source",
        1,
        role="source replacement-open retry pool",
        open_connection=open_connection,
    )
    with manager.lease() as source_ref:
        batch = staged_keyed_io.read_key_batch(
            options,
            source_ref,
            task,
            _metadata(),
            1,
            2,
            batch_index=1,
        )

    assert batch.rows == [(1,)]
    assert open_attempts == 3
    assert reads == 2
    assert manager.high_water_mark == 1
    manager.close()
    assert [connection.close_count for connection in opened] == [1, 1]


def test_staged_key_batch_replaces_failed_source_connection(monkeypatch: Any) -> None:
    options = _options(retry_cnt=2, timeout_increment=0)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    opened: list[Any] = []
    reads = 0

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        return connection

    def read(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("connection lost")
        return SimpleNamespace(column_names=["id"], columns=[[1]])

    monkeypatch.setattr(staged_keyed_io, "_read_backend", read)
    manager = BoundedConnectionManager(
        "source",
        1,
        role="source batch retry pool",
        open_connection=open_connection,
    )
    with manager.lease() as source_ref:
        batch = staged_keyed_io.read_key_batch(
            options,
            source_ref,
            task,
            _metadata(),
            1,
            2,
            batch_index=1,
        )

    assert batch.rows == [(1,)]
    assert reads == 2
    assert len(opened) == 2
    assert opened[0].close_count == 1
    assert manager.high_water_mark == 1
    manager.close()
    assert opened[1].close_count == 1


def test_target_pool_close_failure_after_finalization_is_nonretryable(
    monkeypatch: Any,
) -> None:
    transfer_slice = TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")
    options = _options(
        transfer_slices=[transfer_slice],
        transfer_concurrency=_concurrency(1, 1),
    )
    finalized: list[int] = []

    class CloseFailingManager(_LeaseManager):
        def __init__(
            self,
            _connection_key: str,
            _capacity: int,
            *,
            role: str,
            **_kwargs: Any,
        ) -> None:
            super().__init__()
            self.role = role

        def close(self) -> None:
            if self.role == "target transfer pool":
                raise BoundedConnectionCloseError("target connection remains live")

    def run_workers(
        _options: Any,
        _metadata_value: Any,
        _stage_state: Any,
        runtime: LazyKeyedRuntime,
        _source_connections: Any,
        _target_connections: Any,
        progress: TransferProgressTracker,
        **_kwargs: Any,
    ) -> None:
        runtime.mark_verified(VerifiedKey(0, 0, 0, None))
        progress.start_key(0)
        progress.materialize_key(0, 0)
        progress.assign_key(0, 0)
        progress.verify_key(0)

    monkeypatch.setattr(staged_keyed_pipeline, "BoundedConnectionManager", CloseFailingManager)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: _state())
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", lambda *_args: _metadata())
    monkeypatch.setattr(staged_keyed_pipeline, "_run_lazy_workers", run_workers)
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "_consolidate_created_stages", lambda *_args: 0)
    monkeypatch.setattr(
        staged_keyed_pipeline, "validate_loaded_stage_row_count", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda *_args, **_kwargs: finalized.append(1),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_loading_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    with pytest.raises(BoundedConnectionCloseError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert finalized == [1]
    assert exc_info.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_target_stage_collision_retry_fits_hashed_writer_name(
    monkeypatch: Any,
    backend: str,
) -> None:
    preferred = f"{'a' * 32}__w00000"
    existence = iter([False, True, False])
    creates = iter([OSError("ambiguous create"), None])

    monkeypatch.setattr(
        load_stage,
        "table_exists",
        lambda *_args, **_kwargs: next(existence),
    )
    monkeypatch.setattr(
        load_stage.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="1234567890abcdef"),
    )

    def create(*_args: Any, **_kwargs: Any) -> None:
        outcome = next(creates)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(load_stage, "_create_sql_table_with_connection", create)
    monkeypatch.setattr(load_stage, "time_print", lambda *_args, **_kwargs: None)

    result = load_stage.create_stage_table(
        backend,
        object(),
        "public.target",
        pd.DataFrame(columns=["id"]),
        random_suffix=preferred,
        destination_hash="0123456789abcdef",
    )
    identifier = result.split(".")[-1].strip('"`')

    assert len(identifier.encode()) <= 62
    assert identifier.startswith("0123456789abcdef__")
    assert identifier.endswith(f"{preferred}1234")


def test_target_stage_readiness_failure_is_not_retried_as_collision(
    monkeypatch: Any,
) -> None:
    candidates: list[str] = []
    logs: list[str] = []
    existence_checks = 0

    monkeypatch.setattr(
        load_stage,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )

    def table_exists(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal existence_checks
        existence_checks += 1
        return False

    monkeypatch.setattr(load_stage, "table_exists", table_exists)
    monkeypatch.setattr(
        load_stage,
        "_create_sql_table_with_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SqlTableReadinessError("visible on 20/22 hosts")
        ),
    )
    monkeypatch.setattr(
        load_stage,
        "time_print",
        lambda message, **_kwargs: logs.append(message),
    )

    with pytest.raises(SqlTableReadinessError, match="20/22"):
        load_stage.create_stage_table(
            "ch",
            object(),
            "default.target",
            pd.DataFrame(columns=["id"]),
            random_suffix="first",
            on_stage_candidate=candidates.append,
        )

    assert existence_checks == 1
    assert candidates == ["first"]
    assert not logs
