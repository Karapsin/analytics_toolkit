from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    LazyKeyedRuntime,
    TransferProgressTracker,
    TransferStageState,
    _concurrency,
    _LeaseManager,
    _metadata,
    _options,
    _ready_task,
    _state,
    _thread,
    finalize,
    load_stage,
    pd,
    pytest,
    staged_keyed_pipeline,
    threading,
)


def test_nonlazy_clickhouse_finalization_builds_its_own_bounded_pool(
    monkeypatch: Any,
) -> None:
    active = 0
    high_water = 0
    opened: list[str] = []

    class Connection:
        def __init__(self, role: str) -> None:
            nonlocal active, high_water
            self.role = role
            self.closed = False
            opened.append(role)
            active += 1
            high_water = max(high_water, active)

        def close(self) -> None:
            nonlocal active
            if not self.closed:
                self.closed = True
                active -= 1

    class Adapter:
        @staticmethod
        def needs_bounded_replace_preclear(_only_shard: bool) -> bool:
            return True

        def open_transfer_host_connection(self, _key: str, host: str) -> Connection:
            return Connection(host)

        def preclear_distributed_replace_target(self, *_args: Any, **kwargs: Any) -> bool:
            kwargs["connection_runner"]("coordinator", lambda _connection: None)
            for host in ("host-a", "host-b", "host-c"):
                kwargs["host_connection_runner"](host, lambda _connection: None)
            return True

    monkeypatch.setattr(finalize, "get_backend_adapter", lambda _backend: Adapter())
    monkeypatch.setattr(
        finalize,
        "get_sql_connection",
        lambda _key: Connection("coordinator"),
    )

    assert finalize._preclear_clickhouse_replace_target(
        _options(
            to_db_backend="ch",
            write_mode="replace",
            replace_target_table=True,
            transfer_concurrency=_concurrency(1, 1),
        ),
        TransferStageState(target_exists=True),
        target_connection_runner=None,
        target_host_connection_runner=None,
    )
    assert high_water == 1
    assert active == 0
    assert opened == ["coordinator", "host-a", "host-b", "host-c"]


def test_target_stage_candidate_precedes_partial_clickhouse_create(
    monkeypatch: Any,
) -> None:
    candidates: list[str] = []
    monkeypatch.setattr(
        load_stage,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    monkeypatch.setattr(load_stage, "table_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        load_stage,
        "_create_sql_table_with_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("distributed create failed")),
    )

    with pytest.raises(OSError, match="distributed create failed"):
        load_stage.create_stage_table(
            "ch",
            object(),
            "default.target",
            pd.DataFrame(columns=["id"]),
            random_suffix="partial",
            on_stage_candidate=candidates.append,
            ch_creation_policy=object(),
        )

    assert candidates == ["partial"]


def test_target_stage_candidates_include_ambiguous_create_outcomes(
    monkeypatch: Any,
) -> None:
    candidates: list[str] = []
    logs: list[str] = []
    existence = iter([False, True, False])
    creates = iter([OSError("ambiguous create"), None])
    monkeypatch.setattr(
        load_stage,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    monkeypatch.setattr(
        load_stage,
        "table_exists",
        lambda *_args, **_kwargs: next(existence),
    )
    monkeypatch.setattr(load_stage, "_collision_stage_suffix", lambda *_args, **_kwargs: "next")

    def create(*_args: Any, **_kwargs: Any) -> None:
        outcome = next(creates)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(load_stage, "_create_sql_table_with_connection", create)
    monkeypatch.setattr(
        load_stage,
        "time_print",
        lambda message, **_kwargs: logs.append(message),
    )

    result = load_stage.create_stage_table(
        "gp",
        object(),
        "public.target",
        pd.DataFrame(columns=["id"]),
        random_suffix="first",
        on_stage_candidate=candidates.append,
        log_prefix="[slice=1/1] ",
    )

    assert result == "next"
    assert candidates == ["first", "next"]
    assert logs
    assert all(message.startswith("[slice=1/1] ") for message in logs)


def test_writer_creates_target_stage_on_first_nonempty_key_and_reuses_it(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    tasks = [
        _ready_task(slices[0], "source.empty", 0),
        _ready_task(slices[1], "source.nonempty", 2),
        _ready_task(slices[0], "source.later", 1),
    ]
    target_connections = _LeaseManager()
    progress = TransferProgressTracker(total_key_count=3, active_writers=1)
    events: list[tuple[str, int, str | None]] = []

    def create_stage(
        _options: Any,
        _target_ref: Any,
        _metadata: Any,
        writer_index: int,
        **_kwargs: Any,
    ) -> str:
        events.append(("create", writer_index, None))
        return "target_stage.writer_0"

    def consume(*args: Any, **_kwargs: Any) -> None:
        task = args[9]
        events.append(("consume", task.transfer_slice.index, args[8]))

    monkeypatch.setattr(staged_keyed_pipeline, "create_target_writer_stage", create_stage)
    monkeypatch.setattr(staged_keyed_pipeline, "_consume_key", consume)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    runtime.ready.put(tasks[0])
    worker, errors = _thread(
        lambda: staged_keyed_pipeline._writer_worker(
            options,
            _metadata(),
            _state(),
            runtime,
            target_connections,  # type: ignore[arg-type]
            progress,
            threading.Lock(),
            0,
            1,
        )
    )
    runtime.ready.put(tasks[1], timeout=1)
    runtime.ready.put(tasks[2], timeout=1)
    runtime.ready.put(None, timeout=1)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert events == [
        ("consume", 0, None),
        ("create", 0, None),
        ("consume", 1, "target_stage.writer_0"),
        ("consume", 0, "target_stage.writer_0"),
    ]
    assert runtime.target_stages == {0: "target_stage.writer_0"}
    assert target_connections.lease_count == 1


def test_zero_only_writer_never_creates_target_stage(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    progress = TransferProgressTracker(total_key_count=2, active_writers=1)
    consumed: list[int] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda *_args: pytest.fail("zero-only writer created a target stage"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_consume_key",
        lambda *args, **_kwargs: consumed.append(args[9].transfer_slice.index),
    )

    runtime.ready.put(_ready_task(slices[0], "source.zero_0", 0))
    worker, errors = _thread(
        lambda: staged_keyed_pipeline._writer_worker(
            options,
            _metadata(),
            _state(),
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            progress,
            threading.Lock(),
            0,
            1,
        )
    )
    runtime.ready.put(_ready_task(slices[1], "source.zero_1", 0), timeout=1)
    runtime.ready.put(None, timeout=1)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert consumed == [0, 1]
    assert runtime.target_stages == {}
