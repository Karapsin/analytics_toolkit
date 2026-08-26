from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    RecordingSourceConnection,
    SimpleNamespace,
    StaticClickHouseClient,
    StaticDbapiConnection,
    attempt_module,
    estimate_module,
    make_progress_options,
    models_module,
    progress_module,
    pytest,
    row_counts_module,
)


def test_count_loaded_stage_rows_empty_missing_and_format_fallback() -> None:
    options = make_progress_options(validate_row_count=True)
    state = models_module.TransferStageState(target_exists=False)
    assert (
        row_counts_module._count_loaded_stage_rows(
            options,
            state,
            0,
            open_connection=lambda _key: object(),
        )
        == 0
    )
    with pytest.raises(RuntimeError, match="stage table"):
        row_counts_module._count_loaded_stage_rows(
            options,
            state,
            1,
            open_connection=lambda _key: object(),
        )
    assert row_counts_module._format_row_count("unknown") == "unknown"


@pytest.mark.parametrize(
    ("backend", "connection", "expected_total", "expected_sql_prefix"),
    [
        (
            "gp",
            StaticDbapiConnection([('[{"Plan": {"Plan Rows": 123}}]',)]),
            123,
            "EXPLAIN (FORMAT JSON)",
        ),
        (
            "trino",
            StaticDbapiConnection([('{"outputRowCount": 456}',)]),
            456,
            "EXPLAIN (TYPE DISTRIBUTED, FORMAT JSON)",
        ),
        (
            "ch",
            StaticClickHouseClient([("default", "source_table", 1, 789, 1)]),
            789,
            "EXPLAIN ESTIMATE",
        ),
    ],
)
def test_estimate_source_rows_uses_backend_planner_estimates(
    backend: str,
    connection: Any,
    expected_total: int,
    expected_sql_prefix: str,
) -> None:
    options = models_module.TransferOptions(
        from_db_key=backend,
        from_db_backend=backend,
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        estimate_total_rows=True,
        progress=True,
    )

    estimated_total = estimate_module.estimate_source_rows(options, connection)

    assert estimated_total == expected_total
    executed = getattr(connection, "executed", getattr(connection, "queries", []))
    assert executed[0].startswith(expected_sql_prefix)


def test_load_stage_batches_estimated_total_sets_progress_bar_total(
    monkeypatch,
) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(3)])
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
    monkeypatch.setattr(attempt_module, "estimate_source_rows", lambda *_args: 3)
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

    assert total_rows == 3
    assert progress_bars[0].kwargs["total"] == 3
    assert progress_bars[0].kwargs["bar_format"] == progress_module._TRANSFER_PROGRESS_TOTAL_FORMAT
    assert progress_bars[0].updates == [2, 1]
    assert progress_bars[0].closed is True


def test_load_stage_batches_formats_transferred_row_count(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = RecordingSourceConnection(rows=[(1,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = make_progress_options(progress=False)

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
        batch_size = len(rows)
        del connection_type, connection_ref, table_name, columns, rows
        kwargs["on_success"](1.0, batch_size)
        return 1_000_000

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

    output = capsys.readouterr().out
    assert total_rows == 1_000_000
    assert (
        "[gp_sandbox/gp] "
        "Transferred batch of 1_000_000 row(s) "
        "to sandbox.target__stage__abcd1234 in 1 second "
        "(1,000,000.00 row/s); total transferred 1_000_000 row(s)"
    ) in output


def test_load_stage_batches_logs_transfer_key_label(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = RecordingSourceConnection(rows=[(1,)])
    connection_refs = models_module.TransferConnectionRefs(
        source={"connection": source},
        target={"connection": object()},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "INTEGER"},
    )
    options = make_progress_options(progress=False)

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
        transfer_key_label="event_date='2026-03-30', user_id_suffix='0'",
    )

    output = capsys.readouterr().out
    assert total_rows == 1
    assert (
        "Transferred batch of 1 row(s) "
        "for event_date='2026-03-30', user_id_suffix='0' "
        "to sandbox.target__stage__abcd1234"
    ) in output


def test_load_stage_batches_progress_false_disables_bar(monkeypatch) -> None:
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
        progress=False,
        estimate_total_rows=True,
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

    def unexpected_estimate(*_args: object) -> int:
        raise AssertionError("unexpected estimate")

    monkeypatch.setattr(attempt_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(attempt_module, "estimate_source_rows", unexpected_estimate)
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
    assert len(progress_bars) == 1
    assert progress_bars[0].kwargs["disable"] is True
    assert progress_bars[0].updates == [2]
    assert progress_bars[0].closed is True


def test_load_stage_batches_updates_progress_bar(monkeypatch) -> None:
    source = RecordingSourceConnection(rows=[(row_id,) for row_id in range(3)])
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

    def fake_insert_rows_batch(
        connection_type: str,
        connection_ref: dict[str, Any],
        table_name: str,
        columns: list[str],
        rows: list[tuple[int]],
        **kwargs: Any,
    ) -> int:
        del connection_type, connection_ref, table_name, columns
        kwargs["on_progress"](len(rows))
        kwargs["on_success"](1.0, len(rows))
        return len(rows)

    monkeypatch.setattr(attempt_module, "tqdm", FakeTqdm)
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

    assert total_rows == 3
    assert len(progress_bars) == 1
    assert progress_bars[0].kwargs == {
        "total": None,
        "desc": "transfer_table gp_sandbox.sandbox.target",
        "unit": "row",
        "disable": False,
        "bar_format": progress_module._TRANSFER_PROGRESS_UNKNOWN_TOTAL_FORMAT,
    }
    assert progress_bars[0].updates == [2, 1]
    assert progress_bars[0].closed is True


def test_row_count_direct_failure_and_worker_paths() -> None:
    disabled = make_progress_options(validate_row_count=False)
    state = models_module.TransferStageState(target_exists=False)
    assert (
        row_counts_module.prepare_row_count_validated_options(
            options=disabled,
            connection_refs=models_module.TransferConnectionRefs(),
            stage_state=state,
        )
        is disabled
    )

    enabled = make_progress_options(validate_row_count=True)
    with pytest.raises(RuntimeError, match="slice source row count"):
        row_counts_module.validate_slice_row_count(
            options=enabled,
            stage_state=state,
            slice_index=2,
            transfer_key_label="id=2",
            streamed_rows=1,
        )
    state.current_expected_source_rows = 2
    with pytest.raises(
        row_counts_module.TransferRowCountMismatchError,
        match="slice_index=2; slice=id=2",
    ):
        row_counts_module.validate_slice_row_count(
            options=enabled,
            stage_state=state,
            slice_index=2,
            transfer_key_label="id=2",
            streamed_rows=1,
        )

    worker_one = models_module.TransferStageState(target_exists=False)
    worker_one.expected_source_rows = 2
    worker_one.slice_counts = list(state.slice_counts)
    worker_two = models_module.TransferStageState(target_exists=False)
    worker_two.expected_source_rows = 3
    state.worker_stage_states = [
        SimpleNamespace(stage_state=worker_one),
        SimpleNamespace(stage_state=worker_two),
    ]
    row_counts_module.validate_streamed_row_count(
        options=enabled,
        stage_state=state,
        total_rows=5,
    )
    assert state.expected_source_rows == 5
    assert len(state.slice_counts) == 1
