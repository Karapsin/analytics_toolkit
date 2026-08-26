from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    SimpleNamespace,
    attempt_module,
    keyed_module,
    keyed_pipeline_module,
    keys_module,
    load_sql_table_module,
    make_keyed_options,
    models_module,
    pd,
    pytest,
    retry_module,
    threading,
)


def test_consolidate_keyed_worker_stage_guard_paths() -> None:
    worker = attempt_module.WorkerStageState(
        worker_index=0,
        stage_state=models_module.TransferStageState(target_exists=False),
        transfer_slices=[],
    )
    refs = models_module.TransferConnectionRefs(target={"connection": object()})

    attempt_module.consolidate_keyed_worker_stages(
        options=make_keyed_options(write_mode="upsert"),
        connection_refs=refs,
        worker_stage_states=[worker, worker],
        stage_state=models_module.TransferStageState(target_exists=False),
    )
    attempt_module.consolidate_keyed_worker_stages(
        options=make_keyed_options(),
        connection_refs=refs,
        worker_stage_states=[worker],
        stage_state=models_module.TransferStageState(target_exists=False),
    )
    with pytest.raises(RuntimeError, match="aggregate stage"):
        attempt_module.consolidate_keyed_worker_stages(
            options=make_keyed_options(),
            connection_refs=refs,
            worker_stage_states=[worker, worker],
            stage_state=models_module.TransferStageState(target_exists=False),
        )

    aggregate = models_module.TransferStageState(
        target_exists=False,
        stage_table="stage.aggregate",
    )
    with pytest.raises(RuntimeError, match="worker stage"):
        attempt_module.consolidate_keyed_worker_stages(
            options=make_keyed_options(),
            connection_refs=refs,
            worker_stage_states=[worker, worker],
            stage_state=aggregate,
        )


def test_consolidate_keyed_worker_stages_inserts_into_aggregate_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(concurrency=3)
    connection_refs = models_module.TransferConnectionRefs(
        target={"connection": FakeTransferConnection("target")},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table="stage_w00000",
        stage_tables=["stage_w00000", "stage_w00001", "stage_w00002"],
        stage_column_types={"id": "INTEGER", "event_date": "DATE"},
    )
    worker_stage_states = [
        attempt_module.WorkerStageState(
            worker_index=worker_index,
            stage_state=models_module.TransferStageState(
                target_exists=False,
                stage_table=f"stage_w{worker_index:05d}",
            ),
            transfer_slices=[],
        )
        for worker_index in range(3)
    ]
    inserted: list[tuple[str, str, dict[str, str] | None]] = []

    def fake_insert_from_table(
        _connection_type: str,
        _connection: Any,
        target_table: str,
        source_table: str,
        column_types: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> None:
        inserted.append((target_table, source_table, column_types))

    monkeypatch.setattr(attempt_module, "insert_from_table", fake_insert_from_table)
    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )

    attempt_module.consolidate_keyed_worker_stages(
        options=options,
        connection_refs=connection_refs,
        worker_stage_states=worker_stage_states,
        stage_state=stage_state,
    )

    assert inserted == [
        ("stage_w00000", "stage_w00001", {"id": "INTEGER", "event_date": "DATE"}),
        ("stage_w00000", "stage_w00002", {"id": "INTEGER", "event_date": "DATE"}),
    ]


def test_initialize_keyed_row_stages_creates_one_stage_per_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        table_schema={"id": "INTEGER", "event_date": "DATE"},
    )
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": FakeTransferConnection("source")},
        target={"connection": FakeTransferConnection("target")},
    )
    stage_state = models_module.TransferStageState(target_exists=False)
    created: list[dict[str, Any]] = []

    def fake_create_stage_table(**kwargs: Any) -> str:
        created.append(kwargs)
        return f"stage_{kwargs['random_suffix']}"

    monkeypatch.setattr(attempt_module, "create_stage_table", fake_create_stage_table)
    monkeypatch.setattr(
        attempt_module,
        "ensure_transfer_target_table",
        lambda *_args, **_kwargs: None,
    )

    attempt_module.initialize_shared_stage_for_keyed_slices(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        source_schema=[
            SimpleNamespace(name="id", native_type="integer"),
            SimpleNamespace(name="event_date", native_type="date"),
        ],
    )

    assert len(created) == 5
    assert [item["random_suffix"][-8:] for item in created] == [
        f"__w{worker_index:05d}" for worker_index in range(5)
    ]
    assert stage_state.stage_table == "stage_" + created[0]["random_suffix"]
    assert stage_state.stage_tables == ["stage_" + item["random_suffix"] for item in created]


def test_initialize_shared_keyed_stage_dispatches_parquet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(
        table_schema={"id": "INTEGER", "event_date": "DATE"},
        trino_mode="parquet",
        to_db_backend="trino",
        transfer_staging_schema="scratch",
        s3_transfer_staging_location="s3://bucket/stage",
    )
    state = models_module.TransferStageState(target_exists=False)
    calls: list[str] = []
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *_a, **_k: None)
    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        lambda **_kwargs: calls.append("parquet"),
    )

    attempt_module.initialize_shared_stage_for_keyed_slices(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(target={"connection": object()}),
        stage_state=state,
        source_schema=[],
    )
    assert calls == ["parquet"]


def test_initialize_shared_keyed_stage_maps_inspected_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(table_schema=None, concurrency=1)
    state = models_module.TransferStageState(target_exists=False)
    schema = [SimpleNamespace(name="id", native_type="integer")]
    monkeypatch.setattr(
        attempt_module,
        "map_source_schema_to_target",
        lambda *_a, **_k: {"id": "BIGINT"},
    )
    monkeypatch.setattr(
        attempt_module,
        "get_backend_adapter",
        lambda _b: SimpleNamespace(validate_ch_columns_in_columns=lambda *_a, **_k: None),
    )
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *_a, **_k: None)
    monkeypatch.setattr(attempt_module, "create_stage_table", lambda **_k: "stage.shared")
    attempt_module.initialize_shared_stage_for_keyed_slices(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(target={"connection": object()}),
        stage_state=state,
        source_schema=schema,
    )
    assert state.stage_column_types == {"id": "BIGINT"}


def test_initialize_shared_keyed_stage_requires_resolvable_nonempty_schema() -> None:
    state = models_module.TransferStageState(target_exists=False)
    with pytest.raises(ValueError, match="inspectable source query schema"):
        attempt_module.initialize_shared_stage_for_keyed_slices(
            options=make_keyed_options(table_schema=None),
            connection_refs=models_module.TransferConnectionRefs(target={"connection": object()}),
            stage_state=state,
            source_schema=[],
        )
    with pytest.raises(ValueError, match="at least one column"):
        attempt_module.initialize_shared_stage_for_keyed_slices(
            options=make_keyed_options(table_schema={}),
            connection_refs=models_module.TransferConnectionRefs(target={"connection": object()}),
            stage_state=state,
            source_schema=[],
        )


def test_initialize_shared_keyed_stage_uses_explicit_schema_without_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(
        concurrency=1,
        table_schema={"id": "INTEGER", "event_date": "DATE"},
    )
    state = models_module.TransferStageState(target_exists=False)
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *_a, **_k: None)
    monkeypatch.setattr(
        attempt_module,
        "create_stage_table",
        lambda **kwargs: created.append(kwargs) or "stage.shared",
    )

    attempt_module.initialize_shared_stage_for_keyed_slices(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(target={"connection": object()}),
        stage_state=state,
        source_schema=[],
    )

    assert state.stage_column_types == {"id": "INTEGER", "event_date": "DATE"}
    assert state.stage_table == "stage.shared"
    assert state.stage_table_created is True
    assert list(state.first_non_empty_batch.columns) == ["id", "event_date"]
    assert created[0]["column_types"] == state.stage_column_types


def test_keyed_attempt_cleanup_error_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(validate_row_count=False)
    source = FakeTransferConnection("source")
    monkeypatch.setattr(attempt_module, "get_sql_connection", lambda _key: source)
    monkeypatch.setattr(
        attempt_module,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(
        attempt_module,
        "create_stage_state",
        lambda *_a: models_module.TransferStageState(target_exists=False),
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("keyed transfer")),
    )
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stage",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("keyed cleanup")),
    )
    messages: list[str] = []
    monkeypatch.setattr(attempt_module, "time_print", messages.append)
    with pytest.raises(RuntimeError, match="keyed transfer"):
        attempt_module.run_keyed_transfer_attempt(options, 1, 1)
    assert messages
    assert "Cleanup failed" in messages[0]


def test_keyed_attempt_cleanup_only_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options(validate_row_count=False)
    source = FakeTransferConnection("source")
    state = models_module.TransferStageState(
        target_exists=False, stage_table="stage.shared", stage_tables=["stage.shared"]
    )
    worker = keyed_module.WorkerStageState(
        worker_index=0, stage_state=state, transfer_slices=options.transfer_slices or []
    )
    monkeypatch.setattr(attempt_module, "get_sql_connection", lambda _key: source)
    monkeypatch.setattr(
        attempt_module,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(attempt_module, "create_stage_state", lambda *_a: state)
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_a: [SimpleNamespace(name="id", native_type="int")],
    )
    monkeypatch.setattr(
        attempt_module,
        "initialize_shared_stage_for_keyed_slices",
        lambda **_k: None,
    )
    monkeypatch.setattr(attempt_module, "build_keyed_worker_stage_states", lambda **_k: [worker])
    monkeypatch.setattr(attempt_module, "load_keyed_stage_slices", lambda **_k: 0)
    monkeypatch.setattr(attempt_module, "consolidate_keyed_worker_stages", lambda **_k: None)
    monkeypatch.setattr(attempt_module, "validate_loaded_stage_row_count", lambda **_k: None)
    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", lambda **_k: None)
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stage",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("keyed cleanup only")),
    )
    with pytest.raises(RuntimeError, match="keyed cleanup only"):
        attempt_module.run_keyed_transfer_attempt(options, 1, 1)


def test_keyed_gp_worker_retry_refreshes_only_failed_worker_connection(
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
    replaced_connections: list[tuple[str, str]] = []
    insert_calls: list[tuple[str, str]] = []
    failed_stage_tables: set[str] = set()
    lock = threading.Lock()

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        with lock:
            connection = FakeTransferConnection(f"{connection_key}-{len(opened_connections)}")
            opened_connections.append((connection_key, connection.name))
            return connection

    def fake_replace_connection(
        connection_key: str,
        connection_ref: dict[str, Any],
    ) -> None:
        with lock:
            old_connection = connection_ref["connection"]
            replacement = FakeTransferConnection(
                f"{connection_key}-replacement-{len(replaced_connections)}"
            )
            replaced_connections.append((connection_key, old_connection.name))
            old_connection.close()
            connection_ref["connection"] = replacement

    def fake_insert_rows_backend(
        _backend: str,
        connection: FakeTransferConnection,
        table_name: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        with lock:
            insert_calls.append((table_name, connection.name))
            if table_name.endswith("w00000") and table_name not in failed_stage_tables:
                failed_stage_tables.add(table_name)
                raise RuntimeError("connection already closed")

    def fake_load_stage_batches(**kwargs: Any) -> int:
        return retry_module.run_with_fresh_connection(
            kwargs["options"].to_db_key,
            "insert_stage",
            lambda connection_ref: load_sql_table_module.insert_rows_batch(
                "gp",
                connection_ref,
                kwargs["stage_state"].stage_table,
                ["id"],
                [(kwargs["slice_index"],)],
                retry_fn=retry_module.run_with_retry,
                retry_cnt=2,
                timeout_increment=0,
                connection_key=kwargs["options"].to_db_key,
                rollback_fn=retry_module.rollback_quietly,
                replace_connection_fn=fake_replace_connection,
            ),
            open_connection=fake_get_sql_connection,
        )

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(
        load_sql_table_module,
        "_insert_rows_backend",
        fake_insert_rows_backend,
    )

    total_rows = attempt_module.load_keyed_stage_slices(
        options=options,
        worker_stage_states=worker_stage_states,
        read_retry_cnt=1,
        insert_retry_cnt=2,
    )

    assert total_rows == 2
    assert len(replaced_connections) == 1
    assert replaced_connections[0][0] == "target_db"
    assert [
        connection_name
        for table_name, connection_name in insert_calls
        if table_name.endswith("w00000")
    ] == [replaced_connections[0][1], "target_db-replacement-0"]
    assert (
        len(
            {
                connection_name
                for table_name, connection_name in insert_calls
                if table_name.endswith("w00001")
            }
        )
        == 1
    )


def test_keyed_pipeline_failure_paths_cancel_without_final_ack(
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
    shared_state = models_module.TransferStageState(
        target_exists=False,
        source_columns=["id"],
        source_column_types={"id": "integer"},
    )
    writers = [
        SimpleNamespace(
            stage_state=models_module.TransferStageState(
                target_exists=False,
                stage_table="stage_0",
                stage_column_types={"id": "INTEGER"},
            )
        )
    ]

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
    monkeypatch.setattr(
        keyed_pipeline_module,
        "iter_source_batches",
        lambda *_args, **_kwargs: iter(
            [models_module.RowBatch(["id"], [(1,)]), models_module.RowBatch(["id"], [])]
        ),
    )
    monkeypatch.setattr(
        keyed_pipeline_module,
        "insert_rows_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("write failed")),
    )

    with pytest.raises(RuntimeError, match="write failed"):
        keyed_pipeline_module.run_keyed_transfer_pipeline(
            options=options,
            stage_state=shared_state,
            writer_stage_states=writers,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )

    monkeypatch.setattr(
        keyed_pipeline_module,
        "iter_source_batches",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("read failed")),
    )
    monkeypatch.setattr(keyed_pipeline_module, "insert_rows_batch", lambda *_a, **_k: 1)
    with pytest.raises(RuntimeError, match="read failed"):
        keyed_pipeline_module.run_keyed_transfer_pipeline(
            options=options,
            stage_state=shared_state,
            writer_stage_states=writers,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )
