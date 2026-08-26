from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    SimpleNamespace,
    attempt_module,
    keys_module,
    make_gp_config,
    make_keyed_options,
    make_progress_options,
    models_module,
    pd,
    pytest,
    row_counts_module,
    threading,
    transfer_api_module,
)


def test_keyed_transfer_workers_run_concurrently(
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
    barrier = threading.Barrier(2)
    started: list[int] = []

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        return FakeTransferConnection(connection_key)

    def fake_load_stage_batches(**kwargs: Any) -> int:
        started.append(kwargs["slice_index"])
        barrier.wait(timeout=2)
        return 1

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        attempt_module,
        "load_stage_batches",
        fake_load_stage_batches,
    )

    total_rows = attempt_module.load_keyed_stage_slices(
        options=options,
        worker_stage_states=worker_stage_states,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 2
    assert sorted(started) == [0, 1]


def test_keyed_worker_failure_skips_finalize_and_still_cleans_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    options = make_keyed_options()

    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )
    monkeypatch.setattr(
        attempt_module,
        "create_stage_state",
        lambda *_args, **_kwargs: models_module.TransferStageState(target_exists=False),
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [SimpleNamespace(name="id", native_type="integer")],
    )

    def fake_initialize_shared_stage_for_keyed_slices(**kwargs: Any) -> None:
        stage_state = kwargs["stage_state"]
        stage_state.stage_table = "sandbox.target__stage__abcd1234"
        stage_state.stage_table_created = True
        stage_state.stage_column_types = {"id": "INTEGER"}
        stage_state.first_non_empty_batch = pd.DataFrame(columns=["id"])

    def fake_load_keyed_stage_slices(**_kwargs: Any) -> int:
        events.append("load_keyed_stage_slices")
        raise RuntimeError("slice failed")

    def fail_finalize(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("target must not be finalized after slice failure")

    def fake_cleanup_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup_stage")

    monkeypatch.setattr(
        attempt_module,
        "initialize_shared_stage_for_keyed_slices",
        fake_initialize_shared_stage_for_keyed_slices,
    )
    monkeypatch.setattr(
        attempt_module,
        "load_keyed_stage_slices",
        fake_load_keyed_stage_slices,
    )
    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", fail_finalize)
    monkeypatch.setattr(attempt_module, "cleanup_stage", fake_cleanup_stage)

    with pytest.raises(RuntimeError, match="slice failed"):
        attempt_module.run_transfer_attempt(
            options=options,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )

    assert events == ["load_keyed_stage_slices", "cleanup_stage"]


def test_keyed_worker_stage_groups_assign_slices_round_robin() -> None:
    _keys, expressions, values, slices, concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id, event_date from source_table where {event_date}",
        transfer_keys="event_date",
        transfer_key_values=[f"2025-01-{day:02d}" for day in range(1, 80)],
        concurrency=5,
    )
    options = make_keyed_options(
        transfer_key_expressions=expressions,
        transfer_key_values=values,
        transfer_slices=slices,
        concurrency=concurrency,
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table_created=True,
        first_non_empty_batch=pd.DataFrame(columns=["id", "event_date"]),
        stage_column_types={"id": "INTEGER", "event_date": "DATE"},
        stage_table="stage_w00000",
        stage_tables=[f"stage_w{worker_index:05d}" for worker_index in range(5)],
    )

    worker_stage_states = attempt_module.build_keyed_worker_stage_states(
        options=options,
        stage_state=stage_state,
    )

    assert len(worker_stage_states) == 5
    assert [
        [transfer_slice.index for transfer_slice in worker.transfer_slices]
        for worker in worker_stage_states
    ] == [list(range(worker_index, 79, 5)) for worker_index in range(5)]
    assert [worker.stage_state.stage_table for worker in worker_stage_states] == [
        f"stage_w{worker_index:05d}" for worker_index in range(5)
    ]


def test_keyed_worker_validates_each_slice_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_conn = FakeTransferConnection("source")
    count_values = iter([2, 3])
    streamed_by_sql = {
        "select id from source where id = 1": 2,
        "select id from source where id = 2": 3,
    }
    options = make_progress_options(
        from_db_key="source_db",
        from_db_backend="gp",
        validate_row_count=True,
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table="sandbox.target__stage__abcd1234",
    )
    worker_stage_state = attempt_module.WorkerStageState(
        worker_index=0,
        stage_state=stage_state,
        transfer_slices=[
            models_module.TransferSlice(
                index=0,
                values=(1,),
                predicate_sql="id = 1",
                source_sql="select id from source where id = 1",
                label="id=1",
            ),
            models_module.TransferSlice(
                index=1,
                values=(2,),
                predicate_sql="id = 2",
                source_sql="select id from source where id = 2",
                label="id=2",
            ),
        ],
    )

    monkeypatch.setattr(attempt_module, "get_sql_connection", lambda _key: source_conn)
    monkeypatch.setattr(attempt_module, "close_connection_ref", lambda *a, **k: None)
    monkeypatch.setattr(
        row_counts_module,
        "count_source_rows",
        lambda *_args, **_kwargs: next(count_values),
    )
    monkeypatch.setattr(
        attempt_module,
        "load_stage_batches",
        lambda **kwargs: streamed_by_sql[kwargs["options"].source_sql],
    )

    total_rows = attempt_module.load_keyed_stage_worker(
        options=options,
        worker_stage_state=worker_stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 5
    assert [row_count.as_dict() for row_count in stage_state.slice_counts] == [
        {
            "index": 0,
            "label": None,
            "expected_rows": 2,
            "streamed_rows": 2,
        },
        {
            "index": 1,
            "label": None,
            "expected_rows": 3,
            "streamed_rows": 3,
        },
    ]


def test_lazy_keyed_upsert_dry_run_has_no_consolidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source", transfer_staging_schema="source_stage"),
        "target": make_gp_config("target", transfer_staging_schema="target_stage"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_sql="select id from source_table where {event_date}",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER"},
        transfer_keys="event_date",
        transfer_key_values=[1, 2, 3],
        key_columns="id",
        write_mode="upsert",
        read_concurrency=3,
        write_concurrency=2,
        dry_run=True,
    )

    phases = [step.phase for step in plan.statements]
    assert "consolidate_stage" not in phases
    assert "consolidate_stage_if_created" not in phases
    assert phases.count("upsert_target") >= 1


def test_run_keyed_transfer_attempt_requires_slices() -> None:
    with pytest.raises(ValueError, match="requires transfer_slices"):
        attempt_module.run_keyed_transfer_attempt(
            make_progress_options(),
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )


def test_run_keyed_transfer_attempt_uses_one_stage_finalize_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source_conn = FakeTransferConnection("main-source")
    target_conn = FakeTransferConnection("main-target")
    options = make_keyed_options()

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        if connection_key == "source_db":
            return source_conn
        if connection_key == "target_db":
            return target_conn
        raise AssertionError(f"unexpected connection key: {connection_key}")

    def fake_create_stage_state(*_args: Any, **_kwargs: Any) -> Any:
        events.append("create_stage_state")
        return models_module.TransferStageState(target_exists=False)

    def fake_inspect_source_query_schema(*_args: Any, **_kwargs: Any) -> list[Any]:
        events.append("inspect_source_query_schema")
        return [SimpleNamespace(name="id", native_type="integer")]

    def fake_initialize_shared_stage_for_keyed_slices(**kwargs: Any) -> None:
        events.append("initialize_shared_stage")
        stage_state = kwargs["stage_state"]
        stage_state.stage_table = "sandbox.target__stage__abcd1234"
        stage_state.stage_table_created = True
        stage_state.stage_column_types = {"id": "INTEGER"}
        stage_state.first_non_empty_batch = pd.DataFrame(columns=["id"])

    def fake_load_keyed_stage_slices(**_kwargs: Any) -> int:
        events.append("load_keyed_stage_slices")
        return 5

    def fake_finalize_loaded_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("finalize_loaded_stage")

    def fake_cleanup_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup_stage")

    def fake_close_connection_ref(
        _connection_ref: dict[str, Any],
        _connection_type: str,
        role: str,
    ) -> None:
        events.append(f"close:{role}")

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(attempt_module, "create_stage_state", fake_create_stage_state)
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        fake_inspect_source_query_schema,
    )
    monkeypatch.setattr(
        attempt_module,
        "initialize_shared_stage_for_keyed_slices",
        fake_initialize_shared_stage_for_keyed_slices,
    )
    monkeypatch.setattr(
        attempt_module,
        "load_keyed_stage_slices",
        fake_load_keyed_stage_slices,
    )
    monkeypatch.setattr(
        attempt_module,
        "validate_loaded_stage_row_count",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        attempt_module,
        "finalize_loaded_stage",
        fake_finalize_loaded_stage,
    )
    monkeypatch.setattr(attempt_module, "cleanup_stage", fake_cleanup_stage)
    monkeypatch.setattr(attempt_module, "close_connection_ref", fake_close_connection_ref)

    total_rows = attempt_module.run_transfer_attempt(
        options=options,
        read_retry_cnt=3,
        insert_retry_cnt=2,
    )

    assert total_rows == 5
    assert events == [
        "create_stage_state",
        "inspect_source_query_schema",
        "initialize_shared_stage",
        "close:source coordinator",
        "load_keyed_stage_slices",
        "finalize_loaded_stage",
        "cleanup_stage",
        "close:source",
    ]
