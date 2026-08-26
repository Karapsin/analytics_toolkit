from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    SimpleNamespace,
    attempt_module,
    capture_rendering_progress_bars,
    make_progress_options,
    models_module,
    progress_module,
    pytest,
    row_counts_module,
)


def test_row_count_disabled_mismatch_missing_workers_retry_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = make_progress_options(validate_row_count=False)
    state = models_module.TransferStageState(target_exists=False)
    state.stage_table = "stage"
    monkeypatch.setattr(row_counts_module, "count_table_rows", lambda *_args, **_kwargs: 2)
    row_counts_module.validate_slice_row_count(
        options=disabled,
        stage_state=state,
        slice_index=0,
        transfer_key_label=None,
        streamed_rows=2,
    )
    row_counts_module.validate_streamed_row_count(
        options=disabled,
        stage_state=state,
        total_rows=2,
    )
    row_counts_module.validate_loaded_stage_row_count(
        options=disabled,
        connection_refs=models_module.TransferConnectionRefs(),
        stage_state=state,
        total_rows=2,
        open_connection=lambda _key: object(),
    )
    monkeypatch.setattr(row_counts_module, "count_table_rows", lambda *_args, **_kwargs: 1)
    with pytest.raises(row_counts_module.TransferRowCountMismatchError):
        row_counts_module.validate_loaded_stage_row_count(
            options=disabled,
            connection_refs=models_module.TransferConnectionRefs(),
            stage_state=state,
            total_rows=2,
            open_connection=lambda _key: object(),
        )

    enabled = make_progress_options(validate_row_count=True, retry_cnt=2, timeout_increment=0)
    with pytest.raises(RuntimeError, match="worker stage states"):
        row_counts_module.validate_streamed_row_count(
            options=enabled,
            stage_state=state,
            total_rows=1,
        )
    state.expected_source_rows = 2
    with pytest.raises(row_counts_module.TransferRowCountMismatchError):
        row_counts_module.validate_loaded_stage_row_count(
            options=enabled,
            connection_refs=models_module.TransferConnectionRefs(),
            stage_state=state,
            total_rows=1,
            open_connection=lambda _key: object(),
        )

    source_ref = {"connection": SimpleNamespace(name="old")}
    calls: list[str] = []
    monkeypatch.setattr(
        row_counts_module,
        "count_source_rows",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("count")),
    )
    monkeypatch.setattr(row_counts_module, "rollback_quietly", lambda conn: calls.append(conn.name))
    monkeypatch.setattr(
        row_counts_module,
        "replace_connection",
        lambda _key, ref: ref.update(connection=SimpleNamespace(name="new")),
    )
    with pytest.raises(RuntimeError, match="count"):
        row_counts_module._count_source_rows_with_retry(enabled, source_ref, "select 1")
    assert calls == ["old", "new"]

    messages: list[str] = []
    monkeypatch.setattr(
        row_counts_module,
        "time_print",
        lambda message, **_k: messages.append(message),
    )
    row_counts_module._log_expected_rows(enabled, 12, None, None)
    row_counts_module._log_expected_rows(enabled, 3, 1, "")
    assert messages == [
        "Expecting 12 source row(s)",
        "Expecting 3 source row(s) for for",
    ]


def test_row_count_validation_materializes_source_once_when_schema_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    source_connection = FakeTransferConnection("source")
    options = make_progress_options(
        validate_row_count=True,
        source_transfer_staging_schema="scratch",
        source_transfer_staging_username="source_user",
    )
    stage_state = models_module.TransferStageState(target_exists=False)

    class MaterializingAdapter:
        def build_materialize_transfer_source_sql(
            self,
            table_name: str,
            source_sql: str,
            *,
            query_label: str | None = None,
        ) -> str:
            events.append(("build_materialize", table_name, source_sql, query_label))
            return f"CREATE TABLE {table_name} AS {source_sql}"

        def execute_command(self, connection: Any, sql: str) -> None:
            events.append(("materialize", connection, sql))

        def count_table_rows(
            self,
            connection: Any,
            table_name: str,
            *,
            query_label: str | None = None,
        ) -> int:
            events.append(("count", connection, table_name, query_label))
            return 7

        def source_sql_for_count_limited_read(self, **kwargs: Any) -> str:
            return str(kwargs["source_sql"])

        def drop_table(self, connection: Any, table_name: str, **kwargs: Any) -> None:
            events.append(("drop", connection, table_name, kwargs))

    adapter = MaterializingAdapter()
    monkeypatch.setattr(row_counts_module, "get_backend_adapter", lambda _backend: adapter)

    prepared = row_counts_module.prepare_row_count_validated_options(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(
            source={"connection": source_connection}
        ),
        stage_state=stage_state,
    )

    assert prepared.source_sql.startswith(
        "SELECT * FROM scratch.source_result__analytics_toolkit_source_user__stage__"
    )
    assert stage_state.expected_source_rows == 7
    assert [event[0] for event in events] == [
        "build_materialize",
        "materialize",
        "count",
    ]
    assert events[0][2] == options.source_sql

    row_counts_module.cleanup_materialized_sources(
        options=options,
        connection_ref={"connection": source_connection},
        stage_state=stage_state,
    )

    assert [event[0] for event in events] == [
        "build_materialize",
        "materialize",
        "count",
        "drop",
    ]
    assert stage_state.source_stage_tables == []


def test_transfer_progress_bar_formats_estimated_total_counts(monkeypatch) -> None:
    progress_bars = capture_rendering_progress_bars(monkeypatch)

    options = make_progress_options()
    progress_bar = progress_module.make_transfer_progress_bar(
        options,
        total=2_000_000,
        base_tqdm=attempt_module.tqdm,
    )
    progress_bar.update(1_722_355)

    assert progress_bars[0].rendered == [
        "transfer_table gp_sandbox.sandbox.target:  86%|########| "
        "1_722_355/2_000_000 [00:00<00:02, 14087.46row/s]"
    ]


def test_transfer_progress_bar_formats_unknown_total_counts(monkeypatch) -> None:
    progress_bars = capture_rendering_progress_bars(monkeypatch)

    options = make_progress_options()
    progress_bar = progress_module.make_transfer_progress_bar(
        options,
        total=None,
        base_tqdm=attempt_module.tqdm,
    )
    progress_bar.update(1_722_355)

    assert progress_bars[0].rendered == [
        "transfer_table gp_sandbox.sandbox.target: 1_722_355row [00:00, 14087.46row/s]"
    ]


def test_transfer_progress_bar_progress_false_disables_output(monkeypatch) -> None:
    progress_bars = capture_rendering_progress_bars(monkeypatch)

    options = make_progress_options(progress=False)
    progress_bar = progress_module.make_transfer_progress_bar(
        options,
        total=None,
        base_tqdm=attempt_module.tqdm,
    )
    progress_bar.update(1_722_355)

    assert progress_bars[0].kwargs["disable"] is True
    assert progress_bars[0].rendered == []
