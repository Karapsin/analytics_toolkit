from __future__ import annotations

from analytics_toolkit.sql.core.identifiers import TableIdentifier

from tests.sql._support.transfer_ordinal import (
    Any,
    RowBatch,
    TransferConcurrency,
    TransferOptions,
    TransferSlice,
    TransferStageState,
    _staged_options,
    api,
    attempt,
    dry_run,
    load_stage,
    parquet_batches,
    pd,
    pytest,
    resolve_destination_identity,
    resolve_internal_columns,
    stage_validation,
    staged_attempt,
    staged_keyed_pipeline,
    threading,
)


def test_destination_identity_preserves_quoting_and_normalizes_unquoted() -> None:
    unquoted = resolve_destination_identity("Sales.Orders", "gp")
    quoted = resolve_destination_identity('"Sales"."Orders"', "gp")

    assert unquoted.canonical == "sales.orders"
    assert quoted.canonical == '"Sales"."Orders"'
    assert unquoted.hash_prefix == unquoted.fingerprint[:16]
    assert quoted.hash_prefix != unquoted.hash_prefix


@pytest.mark.parametrize(
    ("total_rows", "batch_size", "requested", "expected"),
    [
        (0, 100, 3, 1),
        (20, 100, 3, 1),
        (100, 100, 3, 1),
        (201, 100, 5, 3),
        (1_000, 100, 3, 3),
    ],
)
def test_effective_transfer_worker_count_uses_initial_batch_count(
    total_rows: int,
    batch_size: int,
    requested: int,
    expected: int,
) -> None:
    assert (
        staged_attempt._effective_transfer_worker_count(
            requested,
            total_rows,
            batch_size,
        )
        == expected
    )


def test_explicit_stage_suffix_collision_allocates_new_name(monkeypatch: Any) -> None:
    existence_checks = 0
    created: list[tuple[str, dict[str, Any]]] = []

    def fake_exists(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal existence_checks
        existence_checks += 1
        return existence_checks == 1

    monkeypatch.setattr(load_stage, "table_exists", fake_exists)
    monkeypatch.setattr(
        load_stage,
        "_create_sql_table_with_connection",
        lambda _backend, _connection, table, *_args, **kwargs: created.append((table, kwargs)),
    )
    actual = load_stage.create_stage_table(
        "trino",
        object(),
        "sales.orders",
        pd.DataFrame({"id": [1]}),
        random_suffix="transferid__w00000",
        destination_hash="0123456789abcdef",
        ddl_properties={"compression_codec": "'ZSTD'"},
    )

    assert actual == created[0][0]
    assert created[0][1]["ddl_properties"] == {"compression_codec": "'ZSTD'"}
    relation = actual.split(".")[-1].strip('"')
    assert relation.startswith("0123456789abcdef__orders")
    assert relation[:-4].endswith("transferid__w00000")
    assert len(relation[-5:]) == 5
    assert len(relation.encode()) <= 63


def test_hashed_stage_name_keeps_prefix_and_gp_byte_limit() -> None:
    identity = resolve_destination_identity("sales.orders", "gp")
    name = load_stage.build_stage_table_name(
        "gp",
        "sales.orders",
        transfer_staging_schema="staging",
        random_suffix="a" * 32,
        destination_hash=identity.hash_prefix,
    )
    relation = name.split(".")[-1].strip('"')

    assert relation.startswith(f"{identity.hash_prefix}__")
    assert "a" * 32 in relation
    assert len(relation.encode()) <= 62
    assert len(f"_{relation}".encode()) <= 63


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_numeric_hashed_stage_identifier_is_quoted_and_parseable(backend: str) -> None:
    schema = "iceberg.staging" if backend == "trino" else "staging"
    name = load_stage.build_stage_table_name(
        backend,
        "sales.orders",
        transfer_staging_schema=schema,
        random_suffix="a" * 32,
        destination_hash="0123456789abcdef",
    )

    assert '"0123456789abcdef__' in name or "`0123456789abcdef__" in name
    assert TableIdentifier.parse(name, backend).relation.startswith("0123456789abcdef__")


def test_internal_columns_resolve_case_and_suffix_collisions() -> None:
    columns = resolve_internal_columns(
        [
            "__ANALYTICS_TOOLKIT_TRANSFER_ID",
            "__analytics_toolkit_destination_table",
            "__analytics_toolkit_row_ordinal",
            "__analytics_toolkit_row_ordinal_1",
        ],
        "gp",
    )

    assert columns.transfer_id == "__analytics_toolkit_transfer_id_1"
    assert columns.destination_table == "__analytics_toolkit_destination_table_1"
    assert columns.row_ordinal == "__analytics_toolkit_row_ordinal_2"
    assert len(set(columns.names())) == 4


def test_keyed_source_staging_pipelines_ready_key_before_later_ctas_finishes(
    monkeypatch: Any,
) -> None:
    slices = [
        TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1"),
        TransferSlice(1, (2,), "", "SELECT 2 AS id", "key=2"),
    ]
    options = _staged_options(
        transfer_slices=slices,
        transfer_keys=["key"],
        transfer_concurrency=TransferConcurrency(None, 2, 1, 2, 1, True),
    )
    internal_columns = resolve_internal_columns(["id"], "gp")
    metadata = staged_keyed_pipeline.freeze_attempt_metadata(
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
        internal_columns=internal_columns,
    )
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        internal_columns=internal_columns,
    )
    runtime = staged_keyed_pipeline.LazyKeyedRuntime(
        slices,
        read_workers=2,
        write_workers=1,
    )
    progress = staged_keyed_pipeline.TransferProgressTracker(
        total_key_count=2,
        active_writers=1,
    )
    source_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "source",
        2,
        role="test source pool",
        open_connection=lambda _key: object(),
    )
    target_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "target",
        1,
        role="test target pool",
        open_connection=lambda _key: object(),
    )
    events: list[str] = []
    later_ctas_started = threading.Event()
    release_later_ctas = threading.Event()
    later_ctas_finished = threading.Event()

    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda _options, _ref, slice_index: f"source_{slice_index}",
    )

    def materialize(_options: Any, _ref: Any, _metadata: Any, item: Any, _stage: str) -> int:
        events.append(f"ctas-start:{item.index}")
        if item.index == 1:
            later_ctas_started.set()
            assert release_later_ctas.wait(timeout=5)
            later_ctas_finished.set()
        events.append(f"ctas-complete:{item.index}")
        return 1

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        materialize,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda *_args, **_kwargs: events.append("target-stage-created") or "target_0",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda _options, _ref, task, _metadata, start, stop, **_kwargs: RowBatch(
            ["id"],
            [(task.transfer_slice.index,) for _ in range(start, stop)],
        ),
    )

    def insert(
        _options: Any,
        _ref: Any,
        _stage: str,
        batch: Any,
        _metadata: Any,
        **_kwargs: Any,
    ) -> int:
        if batch.task.transfer_slice.index == 0:
            assert later_ctas_started.wait(timeout=5)
            assert not later_ctas_finished.is_set()
            events.append("insert:0")
            release_later_ctas.set()
        else:
            events.append("insert:1")
        return batch.batch.row_count

    monkeypatch.setattr(staged_keyed_pipeline, "insert_target_batch", insert)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda _options, _ref, _metadata, task, _stage, _rows: events.append(
            f"validate:{task.transfer_slice.index}"
        ),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda _options, _ref, task: events.append(f"drop:{task.transfer_slice.index}"),
    )

    staged_keyed_pipeline._run_lazy_workers(
        options,
        metadata,
        state,
        runtime,
        source_connections,
        target_connections,
        progress,
        insert_retry_cnt=1,
    )

    assert events.index("insert:0") < events.index("ctas-complete:1")
    assert events.count("target-stage-created") == 1
    assert events.index("validate:0") < events.index("drop:0")
    assert events.index("validate:1") < events.index("drop:1")
    assert runtime.source_stage_tables == []
    assert set(runtime.verified) == {0, 1}


def test_lazy_keyed_metadata_reuses_bounded_final_target_count(monkeypatch: Any) -> None:
    build_calls: list[bool] = []
    transfer_slice = TransferSlice(
        index=0,
        values=(1,),
        predicate_sql="id = 1",
        source_sql="SELECT 1 AS id",
        label="id:1",
    )

    def build_options(**kwargs: Any) -> TransferOptions:
        collect_count = bool(kwargs["collect_final_target_count"])
        build_calls.append(collect_count)
        return _staged_options(
            transfer_keys=["id"],
            transfer_slices=[transfer_slice],
            collect_final_target_count=collect_count,
        )

    def transfer_attempt(options: TransferOptions, **_kwargs: Any) -> int:
        assert options.collect_final_target_count is True
        object.__setattr__(options, "final_target_rows", 17)
        return 1

    monkeypatch.setattr(api, "build_transfer_options", build_options)
    monkeypatch.setattr(api, "run_transfer_attempt", transfer_attempt)
    monkeypatch.setattr(
        api,
        "best_effort_transfer_target_count",
        lambda *_args, **_kwargs: pytest.fail("opened an unbudgeted metadata-count connection"),
    )

    result = api.transfer_table("source", "target", return_metadata=True)

    assert build_calls == [True]
    assert result.metadata.final_target_rows == 17


def test_lazy_source_staged_writers_keep_each_key_on_one_target_stage(
    monkeypatch: Any,
) -> None:
    slices = [
        TransferSlice(index, (index,), "", f"SELECT {index}", f"key={index}") for index in range(4)
    ]
    options = _staged_options(
        transfer_slices=slices,
        transfer_keys=["key"],
        batch_size=2,
        transfer_concurrency=TransferConcurrency(None, 2, 3, 2, 3, True),
    )
    internal_columns = resolve_internal_columns(["id"], "gp")
    metadata = staged_keyed_pipeline.freeze_attempt_metadata(
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
        internal_columns=internal_columns,
    )
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        internal_columns=internal_columns,
    )
    runtime = staged_keyed_pipeline.LazyKeyedRuntime(
        slices,
        read_workers=2,
        write_workers=3,
    )
    progress = staged_keyed_pipeline.TransferProgressTracker(
        total_key_count=4,
        active_writers=3,
    )
    source_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "source",
        2,
        role="test source pool",
        open_connection=lambda _key: object(),
    )
    target_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "target",
        3,
        role="test target pool",
        open_connection=lambda _key: object(),
    )
    expected_rows = {0: 3, 1: 0, 2: 3, 3: 0}
    stages_by_key: dict[int, set[str]] = {}
    keys_by_writer: dict[int, list[int]] = {}
    created_stages: dict[int, str] = {}

    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda _options, _ref, slice_index: f"source_{slice_index}",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        lambda _options, _ref, _metadata, item, _stage: expected_rows[item.index],
    )

    def create_stage(
        _options: Any,
        _ref: Any,
        _metadata: Any,
        writer_index: int,
        **_kwargs: Any,
    ) -> str:
        stage = f"target_{writer_index}"
        assert writer_index not in created_stages
        created_stages[writer_index] = stage
        return stage

    monkeypatch.setattr(staged_keyed_pipeline, "create_target_writer_stage", create_stage)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda _options, _ref, task, _metadata, start, stop, **_kwargs: RowBatch(
            ["id"],
            [(task.transfer_slice.index,) for _ in range(start, stop)],
        ),
    )

    def insert(
        _options: Any,
        _ref: Any,
        stage_table: str,
        batch: Any,
        _metadata: Any,
        **_kwargs: Any,
    ) -> int:
        key = batch.task.transfer_slice.index
        stages_by_key.setdefault(key, set()).add(stage_table)
        return batch.batch.row_count

    def validate(
        _options: Any,
        _ref: Any,
        _metadata: Any,
        task: Any,
        stage_table: str | None,
        streamed_rows: int,
    ) -> None:
        key = task.transfer_slice.index
        writer = task.writer_index
        assert writer is not None
        assert streamed_rows == expected_rows[key]
        keys_by_writer.setdefault(writer, []).append(key)
        if expected_rows[key]:
            assert stage_table == f"target_{writer}"

    monkeypatch.setattr(staged_keyed_pipeline, "insert_target_batch", insert)
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", validate)
    monkeypatch.setattr(staged_keyed_pipeline, "drop_source_stage", lambda *_args: None)

    staged_keyed_pipeline._run_lazy_workers(
        options,
        metadata,
        state,
        runtime,
        source_connections,
        target_connections,
        progress,
        insert_retry_cnt=1,
    )

    assert set(runtime.verified) == set(expected_rows)
    assert set(stages_by_key) == {0, 2}
    assert all(len(stage_names) == 1 for stage_names in stages_by_key.values())
    assert created_stages == {
        writer: f"target_{writer}"
        for writer, keys in keys_by_writer.items()
        if any(expected_rows[key] for key in keys)
    }
    assert runtime.target_stages == created_stages


def test_legacy_stage_identifiers_use_gp_fitting_on_every_backend() -> None:
    names = {
        backend: load_stage.build_stage_table_name(
            backend,
            "sales." + "long_destination_name_" * 8,
            transfer_staging_schema="staging",
            transfer_staging_username="integration_user",
            random_suffix="abcd1234",
        )
        for backend in ("gp", "trino", "ch")
    }
    identifiers = {backend: name.split(".")[-1].strip('"`') for backend, name in names.items()}

    assert identifiers["trino"] == identifiers["gp"]
    assert identifiers["ch"] == identifiers["gp"]
    assert len(identifiers["gp"].encode()) <= 63


def test_new_transfer_dispatch_and_identity_guard_branches(monkeypatch: Any) -> None:
    options = _staged_options(concurrency=4)
    monkeypatch.setattr(
        attempt,
        "run_staged_source_transfer_attempt",
        lambda _options, **_kwargs: 7,
    )
    assert attempt.run_transfer_attempt(options, 1, 1) == 7
    assert dry_run.dry_run_worker_stage_count(options) == 4

    state = TransferStageState(target_exists=True)
    assert parquet_batches.append_transfer_identity_columns(
        RowBatch(["id"], [(1,)]),
        options=_staged_options(transfer_id=None),
        stage_state=state,
        slice_id=0,
        start_ordinal=1,
    ).rows == [(1,)]
    unchanged = parquet_batches.append_transfer_identity_columns(
        RowBatch(["id"], [(1,)]),
        options=options,
        stage_state=state,
        slice_id=0,
        start_ordinal=1,
    )
    assert unchanged.columns == ["id"]
    assert unchanged.rows == [(1,)]

    attempt._cleanup_target_superseded_stages(options, state)


def test_nonempty_stage_slice_accepts_exact_in_memory_count() -> None:
    stage_validation.validate_transfer_stage_slice(
        options=_staged_options(),
        connection=object(),
        stage_table="target_stage.writer_0",
        internal_columns=resolve_internal_columns(["id"], "gp"),
        slice_id=3,
        expected_count=2,
        streamed_count=2,
    )


def test_public_transfer_reuses_one_runtime_id_and_returns_it(monkeypatch: Any) -> None:
    template = TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="SELECT id FROM source",
        target_table="public.target",
        replace_target_table=False,
        canonical_destination_identity="public.target",
        destination_hash="0123456789abcdef",
    )
    seen: list[str | None] = []
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: template)
    monkeypatch.setattr(
        api,
        "run_transfer_attempt",
        lambda options, **_kwargs: seen.append(options.transfer_id) or 3,
    )
    monkeypatch.setattr(
        api,
        "best_effort_transfer_target_count",
        lambda _options, **_kwargs: 3,
    )

    first = api.transfer_table("source", "target", return_metadata=True)
    second = api.transfer_table("source", "target", return_metadata=True)

    assert first.metadata.transfer_id == seen[0]
    assert second.metadata.transfer_id == seen[1]
    assert len(first.metadata.transfer_id or "") == 32
    assert first.metadata.transfer_id != second.metadata.transfer_id
