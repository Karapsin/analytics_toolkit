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
    row_counts_module,
    transfer_stage_module,
)


def test_load_stage_batches_skips_gp_insert_page_sizer_for_non_gp_target(
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
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
    )

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
        assert kwargs["gp_insert_page_size_getter"] is None
        assert kwargs["on_gp_insert_page_success"] is None
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

    assert total_rows == 2


def test_load_stage_batches_starts_adaptive_gp_insert_pages_at_default(
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
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
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
        kwargs["on_gp_insert_page_success"](1.0, len(rows))
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

    assert total_rows == 2
    assert observed_page_sizes == [10_000]


def test_load_stage_batches_starts_adaptive_gp_insert_pages_at_explicit_size(
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
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=True,
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
        kwargs["on_gp_insert_page_success"](1.0, len(rows))
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

    assert total_rows == 2
    assert observed_page_sizes == [100_000]


def test_load_stage_batches_uses_configured_step_for_transfer_and_gp_sizers(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(180)])
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
        batch_size=100,
        adaptive_batch_size=True,
        min_batch_size=1,
        max_batch_size=200,
        adaptive_batch_size_step=0.2,
        gp_insert_chunk_size=2_000,
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
        kwargs["on_gp_insert_page_success"](1.0, 2_000)
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

    assert total_rows == 180
    assert source.cursor_obj.fetch_sizes[:2] == [100, 80]
    assert observed_page_sizes[:2] == [2_000, 1_600]


def test_load_stage_batches_uses_parquet_writer_for_trino_fast_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = RecordingSourceConnection(rows=[(1,), (2,), (3,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "BIGINT"},
    )
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="trino",
        to_db_backend="trino",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
        adaptive_batch_size=False,
        transfer_staging_schema="object_storage.sandbox",
        s3_transfer_staging_schema="hive.sandbox",
        s3_transfer_staging_location="s3://bucket/tmp/analytics_toolkit_transfer",
        transfer_staging_username="target_user",
        trino_mode="parquet",
    )
    written_batches: list[dict[str, Any]] = []

    monkeypatch.setattr(
        attempt_module,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )

    def fake_create_parquet_stage_table(
        options: Any,
        connection_refs: Any,
        stage_state: Any,
    ) -> None:
        del options, connection_refs
        stage_state.first_non_empty_batch = pd.DataFrame({"id": [1]})
        stage_state.stage_table = "object_storage.sandbox.target__stage__abcd1234"
        stage_state.stage_external_location = (
            "s3://bucket/tmp/analytics_toolkit_transfer/target/"
            "__analytics_toolkit_target_user__stage__abcd1234/"
        )
        stage_state.stage_table_created = True

    def fake_write_batch_to_parquet_stage(batch: Any, **kwargs: Any) -> int:
        written_batches.append(
            {
                "rows": list(batch.rows),
                "file_index": kwargs["file_index"],
                "location": kwargs["stage_external_location"],
                "row_group_size": kwargs["row_group_size"],
                "pa": kwargs["pa"],
                "pq": kwargs["pq"],
                "fsspec": kwargs["fsspec_module"],
            }
        )
        return len(batch.rows)

    def fail_insert_rows_batch(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("Parquet staging must not call insert_rows_batch")

    monkeypatch.setattr(
        attempt_module,
        "create_parquet_stage_table",
        fake_create_parquet_stage_table,
    )
    monkeypatch.setattr(
        attempt_module,
        "write_batch_to_parquet_stage",
        fake_write_batch_to_parquet_stage,
    )
    monkeypatch.setattr(attempt_module, "insert_rows_batch", fail_insert_rows_batch)
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
        transfer_key_label="event_date='2026-03-30'",
    )

    output = capsys.readouterr().out
    assert total_rows == 3
    assert [batch["rows"] for batch in written_batches] == [[(1,), (2,)], [(3,)]]
    assert [batch["file_index"] for batch in written_batches] == [0, 1]
    assert all(batch["row_group_size"] == 2 for batch in written_batches)
    assert source.cursor_obj.fetch_sizes == [2, 2, 2]
    assert (
        "Wrote Parquet transfer batch of 2 row(s) "
        "for event_date='2026-03-30' "
        "to s3://bucket/tmp/analytics_toolkit_transfer/target/"
    ) in output


def test_materialized_source_requires_source_staging_schema() -> None:
    options = make_progress_options(source_transfer_staging_schema=None)
    with pytest.raises(RuntimeError, match="source transfer staging schema"):
        row_counts_module._materialize_source_with_retry(
            options,
            {"connection": FakeTransferConnection("source")},
            models_module.TransferStageState(target_exists=False),
        )


def test_materialized_source_retries_create_count_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_connection = FakeTransferConnection("source")
    source_ref = {"connection": source_connection}
    options = make_progress_options(
        validate_row_count=True,
        source_transfer_staging_schema="scratch",
        source_transfer_staging_username="source_user",
        retry_cnt=2,
        timeout_increment=0,
    )
    stage_state = models_module.TransferStageState(target_exists=False)
    replacements: list[str] = []

    class RetryingAdapter:
        execute_calls = 0
        count_calls = 0
        drop_calls = 0
        cleanup_phase = False

        def build_materialize_transfer_source_sql(self, *_args: Any, **_kwargs: Any) -> str:
            return "CREATE TABLE scratch.source_result AS SELECT 1"

        def execute_command(self, _connection: Any, _sql: str) -> None:
            self.execute_calls += 1
            if self.execute_calls == 1:
                raise RuntimeError("create failed")  # noqa: EM101, TRY003

        def count_table_rows(self, *_args: Any, **_kwargs: Any) -> int:
            self.count_calls += 1
            if self.count_calls == 1:
                raise RuntimeError("count failed")  # noqa: EM101, TRY003
            return 4

        def drop_table(self, *_args: Any, **_kwargs: Any) -> None:
            self.drop_calls += 1
            if self.cleanup_phase and self.drop_calls == 2:
                raise RuntimeError("drop failed")  # noqa: EM101, TRY003

    adapter = RetryingAdapter()

    def retry_operation(*, operation: Any, **_kwargs: Any) -> Any:
        try:
            return operation(1)
        except RuntimeError:
            return operation(2)

    monkeypatch.setattr(row_counts_module, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(row_counts_module, "run_with_retry", retry_operation)
    monkeypatch.setattr(
        row_counts_module,
        "replace_connection",
        lambda connection_key, _ref: replacements.append(connection_key),
    )
    monkeypatch.setattr(
        row_counts_module,
        "build_stage_table_name",
        lambda *_args, **_kwargs: "scratch.source_result__stage__fixed",
    )

    source_sql = row_counts_module._materialize_source_with_retry(
        options,
        source_ref,
        stage_state,
    )
    assert source_sql == "SELECT * FROM scratch.source_result__stage__fixed"
    assert (
        row_counts_module._count_materialized_source_rows_with_retry(
            options,
            source_ref,
            stage_state.source_stage_tables[0],
        )
        == 4
    )

    adapter.cleanup_phase = True
    row_counts_module.cleanup_materialized_sources(options, source_ref, stage_state)

    assert adapter.execute_calls == 2
    assert adapter.count_calls == 2
    assert adapter.drop_calls == 3
    assert replacements == ["gp", "gp", "gp"]
    assert source_connection.rollback_calls == 3
    assert stage_state.source_stage_tables == []


def test_stage_early_creation_schema_choices_and_existing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(table_schema={"id": "BIGINT"})
    refs = models_module.TransferConnectionRefs(target={"connection": object()})
    existing = models_module.TransferStageState(target_exists=True)
    transfer_stage_module.ensure_transfer_target_table(options, refs, existing, [])

    adapter = SimpleNamespace(can_create_transfer_target_before_batches=lambda: True)
    monkeypatch.setattr(transfer_stage_module, "get_backend_adapter", lambda _b: adapter)
    with pytest.raises(ValueError, match="schema has no columns"):
        transfer_stage_module.ensure_transfer_target_table(
            options,
            refs,
            models_module.TransferStageState(target_exists=False),
            [],
        )

    state = models_module.TransferStageState(target_exists=False, target_existed_at_start=None)
    monkeypatch.setattr(transfer_stage_module, "_ensure_stage_target_table", lambda **_k: None)
    transfer_stage_module.ensure_transfer_target_table(options, refs, state, ["id"])
    assert state.target_existed_at_start is False
    assert state.target_created_by_operation is True
