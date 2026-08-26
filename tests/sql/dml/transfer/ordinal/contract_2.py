from __future__ import annotations

from tests.sql._support.transfer_ordinal import (
    AdaptiveRangeScheduler,
    Any,
    OrdinalRange,
    RowBatch,
    SimpleNamespace,
    SourceColumn,
    TransferConcurrency,
    TransferOptions,
    TransferStageState,
    _staged_options,
    build_snapshot_range_sql,
    build_snapshot_select_sql,
    build_source_snapshot_sql,
    load_stage,
    parquet_stage,
    pytest,
    resolve_internal_columns,
    source_count,
    staged_attempt,
    transfer_schema,
    transfer_stage,
)


def test_range_read_workers_and_consolidation(monkeypatch: Any) -> None:
    options = _staged_options()
    internal = resolve_internal_columns(["id"], "gp")
    state = TransferStageState(
        target_exists=True,
        internal_columns=internal,
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
    )
    adapter = SimpleNamespace(
        normalize_transfer_source_batch=lambda batch, _types: batch,
    )
    monkeypatch.setattr(staged_attempt, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_attempt,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(column_names=("id",), columns=([7],)),
    )
    batch = staged_attempt._read_snapshot_range(
        options,
        object(),
        "snap",
        ["id"],
        state,
        OrdinalRange(0, 1, 2),
    )
    assert batch.rows == [(7,)]

    state.internal_columns = None
    with pytest.raises(RuntimeError, match="internal columns"):
        staged_attempt._read_snapshot_range(
            options,
            object(),
            "snap",
            ["id"],
            state,
            OrdinalRange(0, 1, 2),
        )
    state.internal_columns = internal

    inserted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        staged_attempt,
        "insert_from_table",
        lambda _backend, _connection, target, source, **_kwargs: inserted.append((target, source)),
    )
    refreshed: list[str] = []
    monkeypatch.setattr(
        staged_attempt,
        "replace_connection",
        lambda connection_key, _ref: refreshed.append(connection_key),
    )
    staged_attempt._consolidate_worker_stages(
        options,
        {"connection": object()},
        state,
        ["stage_0", "stage_1", "stage_2"],
    )
    assert inserted == [("stage_0", "stage_1"), ("stage_0", "stage_2")]
    assert refreshed == ["target"]
    staged_attempt._consolidate_worker_stages(
        _staged_options(write_mode="upsert"),
        {"connection": object()},
        state,
        ["stage_0", "stage_1"],
    )
    assert len(inserted) == 2
    assert refreshed == ["target"]

    ran: list[int] = []
    monkeypatch.setattr(
        staged_attempt,
        "_range_worker",
        lambda *_args: ran.append(int(_args[-2])),
    )
    staged_attempt._run_range_workers(
        options,
        "snap",
        ["id"],
        state,
        ["stage_0", "stage_1"],
        AdaptiveRangeScheduler({}),
        insert_retry_cnt=1,
    )
    assert sorted(ran) == [0, 1]

    monkeypatch.setattr(
        staged_attempt,
        "_range_worker",
        lambda *_args: (_ for _ in ()).throw(OSError("worker failed")),
    )
    with pytest.raises(OSError, match="worker failed"):
        staged_attempt._run_range_workers(
            options,
            "snap",
            ["id"],
            state,
            ["stage_0"],
            AdaptiveRangeScheduler({}),
            insert_retry_cnt=1,
        )


def test_range_worker_coordinator_cancels_pending_future(monkeypatch: Any) -> None:
    cancelled: list[bool] = []

    class FailedFuture:
        def exception(self) -> OSError:
            return OSError("worker failed")

    class PendingFuture:
        def cancel(self) -> None:
            cancelled.append(True)

    monkeypatch.setattr(staged_attempt, "_range_worker", lambda *_args: None)
    monkeypatch.setattr(
        staged_attempt,
        "wait",
        lambda _pending, **_kwargs: ({FailedFuture()}, {PendingFuture()}),
    )
    with pytest.raises(OSError, match="worker failed"):
        staged_attempt._run_range_workers(
            _staged_options(),
            "snap",
            ["id"],
            TransferStageState(target_exists=True),
            ["stage_0"],
            AdaptiveRangeScheduler({}),
            insert_retry_cnt=1,
        )
    assert cancelled == [True]


def test_snapshot_name_counts_and_worker_stage_allocation(monkeypatch: Any) -> None:
    options = _staged_options()
    existence = iter([True, False])
    monkeypatch.setattr(staged_attempt, "table_exists", lambda *_args, **_kwargs: next(existence))
    monkeypatch.setattr(
        staged_attempt,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    allocated = staged_attempt._allocate_snapshot_name(options, {"connection": object()})
    assert allocated != f"{options.transfer_id}__source"

    monkeypatch.setattr(staged_attempt, "table_exists", lambda *_args, **_kwargs: True)
    with pytest.raises(RuntimeError, match="unique source snapshot"):
        staged_attempt._allocate_snapshot_name(options, {"connection": object()})

    adapter = SimpleNamespace(quote_identifier=lambda value: f'"{value}"')
    monkeypatch.setattr(staged_attempt, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_attempt,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(columns=([0, 2], [3, 1])),
    )
    assert staged_attempt._snapshot_slice_counts(options, object(), "snap", "slice") == {0: 3}

    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_table",
        lambda *_args, **kwargs: f"stage_{kwargs['random_suffix']}",
    )
    tables = staged_attempt._create_worker_stages(
        options,
        {"connection": object()},
        state,
        worker_count=2,
    )
    assert len(tables) == 2
    assert state.stage_table == tables[0]


def test_snapshot_sql_keeps_paging_metadata_source_local() -> None:
    columns = resolve_internal_columns(["id"], "gp")
    select_sql = build_snapshot_select_sql(
        backend="gp",
        source_sql="SELECT id FROM source_table;",
        source_columns=["id"],
        transfer_id="a" * 32,
        canonical_destination="sales.orders",
        slice_id=4,
        internal_columns=columns,
    )
    snapshot = build_source_snapshot_sql(
        backend="gp",
        snapshot_table="staging.snapshot",
        snapshot_select_sql=select_sql,
        internal_columns=columns,
    )
    range_sql = build_snapshot_range_sql(
        backend="gp",
        snapshot_table="staging.snapshot",
        source_columns=["id"],
        internal_columns=columns,
        transfer_id="a" * 32,
        canonical_destination="sales.orders",
        ordinal_range=OrdinalRange(4, 10, 20),
    )

    assert "row_number() OVER (PARTITION BY 4)" in select_sql
    assert "DISTRIBUTED RANDOMLY" in snapshot.create_sql
    assert snapshot.post_create_sqls[0].startswith("CREATE INDEX")
    assert "__analytics_toolkit_transfer_id" not in select_sql
    assert "__analytics_toolkit_destination_table" not in select_sql
    assert "__analytics_toolkit_transfer_id" not in range_sql
    assert "__analytics_toolkit_destination_table" not in range_sql
    assert range_sql.startswith('SELECT "id" FROM staging.snapshot')
    assert ">= 10" in range_sql and "< 20" in range_sql
    assert range_sql.endswith('ORDER BY "__analytics_toolkit_row_ordinal" LIMIT 10')


@pytest.mark.parametrize(
    ("target_table", "username", "stage_suffix"),
    [
        ("sales.orders", None, "abcd1234"),
        ("sales.orders", "integration_user", "a" * 32 + "__w00000"),
        ("sales." + "😀" * 40, "integration_user", "b" * 32 + "__source"),
    ],
)
def test_stage_identifiers_use_exact_gp_style_on_every_backend(
    target_table: str,
    username: str | None,
    stage_suffix: str,
) -> None:
    names = {
        backend: load_stage.build_stage_table_name(
            backend,
            target_table,
            transfer_staging_schema="staging",
            transfer_staging_username=username,
            random_suffix=stage_suffix,
            destination_hash="0123456789abcdef",
        )
        for backend in ("gp", "trino", "ch")
    }
    identifiers = {backend: name.split(".")[-1].strip('"`') for backend, name in names.items()}

    assert identifiers["trino"] == identifiers["gp"]
    assert identifiers["ch"] == identifiers["gp"]
    assert identifiers["gp"].startswith("0123456789abcdef__")
    assert identifiers["gp"].endswith(stage_suffix)
    assert len(identifiers["gp"].encode()) <= 63


def test_staged_attempt_orchestrates_snapshot_ranges_and_finalization(
    monkeypatch: Any,
) -> None:
    options = _staged_options(
        concurrency=8,
        transfer_concurrency=TransferConcurrency(
            legacy_value=8,
            requested_read=8,
            requested_write=8,
            effective_read=2,
            effective_write=2,
            split_requested=False,
            soft_concurrency_cap=2,
            hard_concurrency_cap=5,
            soft_limited_read=2,
            soft_limited_write=2,
        ),
    )
    state = TransferStageState(target_exists=True)
    finalized: list[tuple[int, list[str]]] = []
    worker_counts: list[int] = []

    class Scheduler:
        def validate_complete(self) -> None:
            finalized.append((-1, []))

    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(staged_attempt, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(
        staged_attempt,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("id", "bigint")],
    )
    monkeypatch.setattr(
        staged_attempt, "cleanup_superseded_transfer_stages", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        staged_attempt,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(staged_attempt, "ensure_transfer_target_table", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "_materialize_snapshot", lambda *_args: ("snap", {0: 6}))

    refreshed_target = object()
    refreshes: list[str] = []

    def create_worker_stages(
        _options: TransferOptions,
        target_ref: dict[str, Any],
        _state: TransferStageState,
        **_kwargs: Any,
    ) -> list[str]:
        assert target_ref["connection"] is refreshed_target
        worker_counts.append(_kwargs["worker_count"])
        return ["worker_stage"]

    monkeypatch.setattr(
        staged_attempt,
        "_create_worker_stages",
        create_worker_stages,
    )
    monkeypatch.setattr(staged_attempt, "AdaptiveRangeScheduler", lambda _counts: Scheduler())

    def run_range_workers(*_args: Any, **kwargs: Any) -> None:
        progress = kwargs["transfer_progress"]
        completed_at = progress.now()
        progress.commit_batch(
            logical_batch_id=(0, 1, 7),
            worker_id=0,
            batch=RowBatch(["id"], [(value,) for value in range(6)]),
            read_started_at=completed_at,
            read_completed_at=completed_at,
            insert_completed_at=completed_at,
        )

    monkeypatch.setattr(staged_attempt, "_run_range_workers", run_range_workers)
    monkeypatch.setattr(staged_attempt, "_consolidate_worker_stages", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "validate_loaded_stage_row_count", lambda **_kwargs: None)
    monkeypatch.setattr(
        staged_attempt,
        "finalize_loaded_stage",
        lambda _options, _refs, stage_state, total: finalized.append(
            (total, list(stage_state.stage_tables or []))
        ),
    )
    monkeypatch.setattr(staged_attempt, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_attempt, "cleanup_stage_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(
        staged_attempt,
        "replace_connection",
        lambda key, ref: (refreshes.append(key), ref.update(connection=refreshed_target)),
    )

    assert staged_attempt.run_staged_source_transfer_attempt(options, insert_retry_cnt=1) == 6
    assert finalized == [(-1, []), (6, [])]
    assert state.slice_counts[0].expected_rows == 6
    assert worker_counts == [2]
    assert refreshes == ["target"]

    monkeypatch.setattr(
        staged_attempt,
        "cleanup_stage_table",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    with pytest.raises(OSError, match="cleanup failed"):
        staged_attempt.run_staged_source_transfer_attempt(options, insert_retry_cnt=1)

    monkeypatch.setattr(
        staged_attempt,
        "finalize_loaded_stage",
        lambda *_args: (_ for _ in ()).throw(ValueError("finalization failed")),
    )
    with pytest.raises(ValueError, match="finalization failed") as caught:
        staged_attempt.run_staged_source_transfer_attempt(options, insert_retry_cnt=1)
    summary = caught.value.analytics_toolkit_transfer_attempt_summary
    assert summary["phase"] == "destination finalization"
    assert summary["committed_rows"] == 6
    assert summary["elapsed_seconds"] >= 0


def test_transfer_parquet_filename_contains_runtime_range(monkeypatch: Any) -> None:
    uploaded: list[str] = []
    monkeypatch.setattr(
        parquet_stage,
        "row_batch_to_arrow_table",
        lambda _pa, _batch, **_kwargs: object(),
    )
    monkeypatch.setattr(
        parquet_stage,
        "write_arrow_table_to_parquet",
        lambda _pq, _table, stream, **_kwargs: stream.write(b"parquet"),
    )
    monkeypatch.setattr(
        parquet_stage,
        "upload_spooled_file",
        lambda _fsspec, _stream, uri: uploaded.append(uri),
    )
    rows = parquet_stage.write_batch_to_parquet_stage(
        RowBatch(["id"], [(1,)]),
        file_index=3,
        slice_index=None,
        stage_external_location="memory://bucket/stage/",
        pa=object(),
        pq=object(),
        fsspec_module=object(),
        row_group_size=10,
        transfer_id="a" * 32,
        worker_id=2,
        start_ordinal=10,
        stop_ordinal=20,
    )
    assert rows == 1
    assert "worker-00002-slice-00000-range-00000000000000000010-00000000000000000020" in uploaded[0]

    adapter = SimpleNamespace(
        infer_parquet_stage_column_types_from_rows=lambda _batch: {"id": "BIGINT"}
    )
    monkeypatch.setattr(parquet_stage, "get_backend_adapter", lambda _backend: adapter)
    assert parquet_stage.infer_trino_column_types_from_rows(RowBatch(["id"], [(1,)])) == {
        "id": "BIGINT"
    }
    assert list(parquet_stage.sample_dataframe_from_batch(RowBatch(["id"], [(1,)])).columns) == [
        "id"
    ]

    monkeypatch.setattr(
        transfer_schema,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(map_source_type_to_target=lambda column: column.name),
    )
    assert transfer_schema.map_source_type_to_target(SourceColumn("id"), "gp") == "id"


def test_transfer_stage_backend_helpers_cover_storage_and_identifier_edges() -> None:
    calls: list[str] = []
    adapter = SimpleNamespace(
        execute_materialization_command=lambda _connection, sql: calls.append(f"trino:{sql}"),
        execute_command=lambda _connection, sql: calls.append(f"other:{sql}"),
    )
    transfer_stage.execute_transfer_materialization(adapter, "trino", object(), "CREATE")
    transfer_stage.execute_transfer_materialization(adapter, "gp", object(), "CREATE")
    assert calls == ["trino:CREATE", "other:CREATE"]
    assert transfer_stage.normalize_unquoted_identifier("MiXeD", "gp") == "mixed"
    assert transfer_stage.normalize_unquoted_identifier("MiXeD", "ch") == "MiXeD"
    with pytest.raises(KeyError):
        transfer_stage.normalize_unquoted_identifier("x", "unknown")

    assert transfer_stage.build_transfer_stage_tail("gp", "user", "suffix") == "suffix"
    assert transfer_stage.build_transfer_stage_tail("trino", "user", "suffix") == "suffix"
    assert transfer_stage.build_transfer_stage_tail("ch", None, "suffix") == "suffix"
    with pytest.raises(KeyError):
        transfer_stage.build_transfer_stage_tail("unknown", None, "suffix")
    assert transfer_stage.collision_stage_suffix("gp", "base", "12345678") == "base1234"
    assert transfer_stage.collision_stage_suffix("ch", "base", "12345678") == "base1234"
    with pytest.raises(KeyError):
        transfer_stage.collision_stage_suffix("unknown", "base", "123")

    expected_name = transfer_stage.fit_hashed_stage_identifier("gp", "hash__", "name", "__tail")
    assert (
        transfer_stage.fit_hashed_stage_identifier("trino", "hash__", "name", "__tail")
        == expected_name
    )
    stage_tail = "a" * 32 + "__w00000"
    trino_name = transfer_stage.fit_hashed_stage_identifier(
        "trino",
        "f" * 16 + "__",
        "destination_" * 20,
        stage_tail,
    )
    assert len(trino_name.encode()) <= 63
    assert trino_name.startswith("f" * 16 + "__")
    assert trino_name.endswith(stage_tail)
    with pytest.raises(ValueError, match="too long for Greenplum"):
        transfer_stage.fit_hashed_stage_identifier(
            "trino",
            "x" * 64,
            "name",
            "tail",
        )
    gp_name = transfer_stage.fit_hashed_stage_identifier(
        "gp",
        "hash__",
        "😀" * 100,
        "__tail",
    )
    assert len(gp_name.encode()) <= 63
    with pytest.raises(ValueError, match="too long"):
        transfer_stage.fit_hashed_stage_identifier("gp", "x" * 64, "name", "tail")
    with pytest.raises(KeyError):
        transfer_stage.fit_hashed_stage_identifier("unknown", "hash__", "name", "tail")
    with pytest.raises(KeyError):
        load_stage._stage_base_identifier("unknown", "name", None, "suffix")

    gp_sql, gp_post = transfer_stage.build_source_snapshot_sqls(
        "gp", "snap", "SELECT 1", "slice", "ordinal"
    )
    ch_sql, ch_post = transfer_stage.build_source_snapshot_sqls(
        "ch", "snap", "SELECT 1", "slice", "ordinal"
    )
    trino_sql, trino_post = transfer_stage.build_source_snapshot_sqls(
        "trino", "snap", "SELECT 1", "slice", "ordinal"
    )
    assert "DISTRIBUTED RANDOMLY" in gp_sql and len(gp_post) == 2
    other_gp_sql, other_gp_post = transfer_stage.build_source_snapshot_sqls(
        "gp", "snap_other", "SELECT 1", "slice", "ordinal"
    )
    assert other_gp_sql != gp_sql
    assert gp_post[0].split()[2] != other_gp_post[0].split()[2]
    assert len(gp_post[0].split()[2].encode()) <= 63
    assert "MergeTree" in ch_sql and not ch_post
    assert trino_sql == "CREATE TABLE snap AS SELECT 1" and not trino_post
    assert source_count._apply_query_label("SELECT 1", None) == "SELECT 1"
