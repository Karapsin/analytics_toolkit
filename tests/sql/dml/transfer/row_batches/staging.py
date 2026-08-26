from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    RecordingSourceConnection,
    SimpleNamespace,
    attempt_module,
    make_progress_options,
    models_module,
    parquet_batches_module,
    pd,
    pytest,
)


def test_initialize_parquet_first_batch_uses_explicit_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        to_db_backend="trino",
        table_schema={"id": "BIGINT"},
    )
    state = models_module.TransferStageState(target_exists=False)
    calls: list[str] = []
    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        lambda **_kwargs: calls.append("create"),
    )

    attempt_module._initialize_parquet_stage_for_first_batch(
        options,
        models_module.TransferConnectionRefs(target={"connection": object()}),
        state,
        models_module.RowBatch(columns=["id"], rows=[(1,)]),
    )
    assert state.stage_column_types == {"id": "BIGINT"}
    assert list(state.first_non_empty_batch["id"]) == [1]
    assert calls == ["create"]


def test_load_parquet_stage_batches_empty_estimate_and_missing_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        to_db_backend="trino",
        trino_mode="parquet",
        s3_transfer_staging_schema="hive.scratch",
        progress=True,
        estimate_total_rows=True,
    )
    progress_bar = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        attempt_module,
        "ensure_parquet_staging_dependencies",
        lambda: (object(), object(), object()),
    )
    monkeypatch.setattr(attempt_module, "parquet_row_group_size", lambda _options: 10)
    monkeypatch.setattr(attempt_module, "estimate_source_rows", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        attempt_module,
        "make_transfer_progress_bar",
        lambda *_a, **_k: progress_bar,
    )
    batches = [
        models_module.RowBatch(columns=["id"], rows=[]),
        models_module.RowBatch(columns=["id"], rows=[(1,)]),
    ]
    monkeypatch.setattr(attempt_module, "iter_source_batches", lambda *_a, **_k: iter(batches))
    state = models_module.TransferStageState(
        target_exists=False,
        first_non_empty_batch=pd.DataFrame({"id": [1]}),
    )
    with pytest.raises(RuntimeError, match="Parquet stage location"):
        attempt_module.load_parquet_stage_batches(
            options,
            models_module.TransferConnectionRefs(source={"connection": object()}),
            state,
            read_retry_cnt=1,
        )


def test_load_parquet_stage_infers_schema_from_first_row_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = RecordingSourceConnection(rows=[(1, "a")])
    source.cursor_obj.description = [
        ("id", 23, None, None, None, None),
        ("label", 25, None, None, None, None),
    ]
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(target_exists=False)
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id, label from source_table",
        target_table="sandbox.target",
        batch_size=1,
        transfer_staging_schema="object_storage.sandbox",
        s3_transfer_staging_schema="hive.sandbox",
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        transfer_staging_username="target_user",
        trino_mode="parquet",
    )

    monkeypatch.setattr(
        attempt_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )
    monkeypatch.setattr(
        attempt_module,
        "write_batch_to_parquet_stage",
        lambda batch, **kwargs: len(batch.rows),
    )

    def fake_create_parquet_stage_table(
        options: Any,
        connection_refs: Any,
        stage_state: Any,
    ) -> None:
        del options, connection_refs
        stage_state.stage_table = "object_storage.sandbox.target__stage__abcd1234"
        stage_state.stage_external_location = "s3://bucket/tmp/stage/"
        stage_state.stage_table_created = True

    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        fake_create_parquet_stage_table,
    )
    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )
    monkeypatch.setattr(
        parquet_batches_module,
        "get_sql_connection",
        FakeTransferConnection,
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 1
    assert stage_state.stage_column_types == {"id": "BIGINT", "label": "VARCHAR"}
    assert list(stage_state.first_non_empty_batch.columns) == ["id", "label"]


def test_load_stage_batches_can_adapt_to_memory_target(monkeypatch) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(10)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
        min_batch_size=1,
        max_batch_size=4,
        target_rows_per_second=False,
        target_batch_seconds=10.0,
        target_batch_memory_mb=1,
        target_batch_memory_bytes=100,
    )
    inserted_batch_sizes: list[int] = []
    memory_measurements = iter([40, 40, 300, 300])

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def fake_approx_memory_bytes(self: object) -> int:
        del self
        return next(memory_measurements)

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        del connection_type, connection_ref, table_name
        assert columns == ["id"]
        inserted_batch_sizes.append(len(rows))
        kwargs["on_success"](30.0, len(rows))
        return len(rows)

    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(
        models_module.RowBatch,
        "approx_memory_bytes",
        fake_approx_memory_bytes,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fake_insert_rows_batch)
    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 10
    assert inserted_batch_sizes == [2, 3, 4, 1]
    assert source.cursor_obj.fetch_sizes == [2, 3, 4, 1, 1]


def test_load_stage_batches_estimator_failure_keeps_unknown_total(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(1,), (2,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        min_batch_size=1,
        max_batch_size=4,
        target_batch_seconds=10.0,
        estimate_total_rows=True,
        progress=True,
    )
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    monkeypatch.setattr(attempt_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(
        attempt_module,
        "insert_rows_batch",
        lambda *args, **kwargs: len(args[4]),
    )
    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 2
    assert progress_bars[0].kwargs["total"] is None
    assert progress_bars[0].updates == [2]
    assert source.cursor_obj.executed[0].startswith("EXPLAIN (FORMAT JSON)")
    assert source.cursor_obj.executed[-1] == "select id from source_table"


def test_load_stage_batches_fetches_row_batches_with_adaptive_sizes(monkeypatch) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(10)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
        min_batch_size=1,
        max_batch_size=4,
        target_rows_per_second=False,
        target_batch_seconds=10.0,
        gp_insert_chunk_size=50_000,
    )
    inserted_batch_sizes: list[int] = []
    insert_durations = iter([1.0, 1.0, 30.0, 30.0])

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        del connection_type, connection_ref, table_name
        assert columns == ["id"]
        assert not isinstance(rows, pd.DataFrame)
        assert kwargs["gp_insert_chunk_size"] == 50_000
        inserted_batch_sizes.append(len(rows))
        kwargs["on_success"](next(insert_durations), len(rows))
        return len(rows)

    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fake_insert_rows_batch)
    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 10
    assert inserted_batch_sizes == [2, 3, 4, 1]
    assert source.cursor_obj.fetch_sizes == [2, 3, 4, 2, 1]


def test_load_stage_batches_keeps_gp_insert_pages_fixed_when_adaptive_disabled(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(1,), (2,), (3,), (4,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = models_module.TransferOptions(
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        gp_insert_chunk_size=100_000,
    )
    observed_page_sizes: list[int] = []

    def fake_initialize_stage_for_first_batch(
        options: object,
        connection_refs: object,
        stage_state: object,
        batch: object,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = batch.to_dataframe()
        stage_state.stage_table = "sandbox.target__stage__abcd1234"

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        del connection_type, connection_ref, table_name, columns
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_gp_insert_page_success"](0.1, len(rows))
        observed_page_sizes.append(kwargs["gp_insert_page_size_getter"]())
        kwargs["on_success"](1.0, len(rows))
        return len(rows)

    monkeypatch.setattr(
        attempt_module,
        "initialize_stage_for_first_batch",
        fake_initialize_stage_for_first_batch,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fake_insert_rows_batch)
    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )

    total_rows = attempt_module.load_stage_batches(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 4
    assert observed_page_sizes == [100_000, 100_000, 100_000, 100_000]


def test_load_stage_batches_skips_empty_source_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(progress=False, estimate_total_rows=False)
    progress_bar = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        attempt_module,
        "make_transfer_progress_bar",
        lambda *_a, **_k: progress_bar,
    )
    monkeypatch.setattr(
        attempt_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(transfer_insert_page_sizing=lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        attempt_module,
        "iter_source_batches",
        lambda *_a, **_k: iter([models_module.RowBatch(columns=["id"], rows=[])]),
    )
    assert (
        attempt_module.load_stage_batches(
            options,
            models_module.TransferConnectionRefs(source={"connection": object()}),
            models_module.TransferStageState(target_exists=False),
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )
        == 0
    )
