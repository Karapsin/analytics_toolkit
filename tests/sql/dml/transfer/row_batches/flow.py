from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    ProtocolError,
    SimpleNamespace,
    attempt_module,
    make_progress_options,
    models_module,
    pytest,
    row_counts_module,
    source_module,
    transfer_api_module,
)


def test_run_transfer_attempt_aborts_stream_failure_before_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="ch",
        validate_row_count=False,
    )
    stream_error = source_module.TransferSourceStreamReadError(
        connection_key="source_db",
        backend="ch",
        query="select id from source_table",
        original_exception=ProtocolError("unexpected failure to read next chunk"),
    )

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
        lambda *_args, **_kwargs: [
            SimpleNamespace(name="id", native_type="integer", precision=None, scale=None)
        ],
    )
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *a, **k: None)
    monkeypatch.setattr(
        attempt_module,
        "load_stage_batches",
        lambda *_args, **_kwargs: events.append("load") or (_ for _ in ()).throw(stream_error),
    )
    monkeypatch.setattr(
        attempt_module,
        "finalize_loaded_stage",
        lambda *_args, **_kwargs: events.append("finalize"),
    )
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stage",
        lambda *_args, **_kwargs: events.append("cleanup"),
    )
    monkeypatch.setattr(attempt_module, "close_connection_ref", lambda *a, **k: None)

    with pytest.raises(source_module.TransferSourceStreamReadError):
        attempt_module.run_transfer_attempt(
            options=options,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )

    assert events == ["load", "cleanup"]


def test_run_transfer_attempt_cleans_only_current_stage_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")

    options = make_progress_options(
        transfer_staging_schema="transfer_schema",
        transfer_staging_username="target_user",
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="gp",
    )

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        if connection_key == "source_db":
            return source_conn
        if connection_key == "target_db":
            return target_conn
        raise AssertionError(f"unexpected connection key: {connection_key}")

    def fake_create_stage_state(*_args: Any, **_kwargs: Any) -> models_module.TransferStageState:
        events.append("create_stage_state")
        return models_module.TransferStageState(target_exists=False)

    def fake_inspect_source_query_schema(*_args: Any, **_kwargs: Any) -> list[Any]:
        events.append("inspect_source_query_schema")
        return [
            SimpleNamespace(
                name="id",
                native_type="integer",
                precision=None,
                scale=None,
            )
        ]

    def fake_ensure_transfer_target_table(*_args: Any, **_kwargs: Any) -> None:
        events.append("ensure_transfer_target_table")

    def fake_load_stage_batches(*_args: Any, **_kwargs: Any) -> int:
        events.append("load_stage_batches")
        return 7

    def fake_finalize_loaded_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("finalize_loaded_stage")

    def fake_cleanup_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup_stage")

    def fake_cleanup_stale_stage_tables_with_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("transfer must not run stale stage discovery cleanup")

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
        "cleanup_stale_stage_tables_with_connection",
        fake_cleanup_stale_stage_tables_with_connection,
        raising=False,
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        fake_inspect_source_query_schema,
    )
    monkeypatch.setattr(
        attempt_module,
        "ensure_transfer_target_table",
        fake_ensure_transfer_target_table,
    )
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(attempt_module, "validate_loaded_stage_row_count", lambda **_kwargs: None)
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

    assert total_rows == 7
    assert events == [
        "create_stage_state",
        "inspect_source_query_schema",
        "ensure_transfer_target_table",
        "load_stage_batches",
        "finalize_loaded_stage",
        "cleanup_stage",
        "close:source",
    ]


def test_run_transfer_attempt_fails_before_finalize_when_stage_count_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")
    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="gp",
        validate_row_count=True,
    )

    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda connection_key: source_conn if connection_key == "source_db" else target_conn,
    )
    monkeypatch.setattr(
        attempt_module,
        "create_stage_state",
        lambda *_args, **_kwargs: models_module.TransferStageState(target_exists=False),
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [
            SimpleNamespace(name="id", native_type="integer", precision=None, scale=None)
        ],
    )
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *a, **k: None)

    def fake_load_stage_batches(**kwargs: Any) -> int:
        kwargs["stage_state"].stage_table = "sandbox.target__stage__abcd1234"
        return 7

    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(
        attempt_module,
        "finalize_loaded_stage",
        lambda *a, **k: events.append("finalize"),
    )
    monkeypatch.setattr(attempt_module, "cleanup_stage", lambda *a, **k: events.append("cleanup"))
    monkeypatch.setattr(
        attempt_module, "close_connection_ref", lambda *a, **k: events.append("close")
    )
    monkeypatch.setattr(row_counts_module, "count_source_rows", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(row_counts_module, "count_table_rows", lambda *_args, **_kwargs: 6)

    with pytest.raises(
        row_counts_module.TransferRowCountMismatchError,
        match="expected_source_rows=7; streamed_rows=7; stage_rows=6",
    ):
        attempt_module.run_transfer_attempt(
            options=options,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )

    assert events == ["cleanup", "close"]


def test_run_transfer_attempt_skips_stale_cleanup_when_staging_schema_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")

    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="gp",
        transfer_staging_schema=None,
    )

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        if connection_key == "source_db":
            return source_conn
        if connection_key == "target_db":
            return target_conn
        raise AssertionError(f"unexpected connection key: {connection_key}")

    def fake_create_stage_state(*_args: Any, **_kwargs: Any) -> models_module.TransferStageState:
        events.append("create_stage_state")
        return models_module.TransferStageState(target_exists=False)

    def fake_inspect_source_query_schema(*_args: Any, **_kwargs: Any) -> list[Any]:
        events.append("inspect_source_query_schema")
        return [
            SimpleNamespace(
                name="id",
                native_type="integer",
                precision=None,
                scale=None,
            )
        ]

    def fake_ensure_transfer_target_table(*_args: Any, **_kwargs: Any) -> None:
        events.append("ensure_transfer_target_table")

    def fake_load_stage_batches(*_args: Any, **_kwargs: Any) -> int:
        events.append("load_stage_batches")
        return 7

    def fake_finalize_loaded_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("finalize_loaded_stage")

    def fake_cleanup_stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup_stage")

    def fake_cleanup_stale_stage_tables_with_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("transfer must not run stale stage discovery cleanup")

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
        "ensure_transfer_target_table",
        fake_ensure_transfer_target_table,
    )
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(attempt_module, "validate_loaded_stage_row_count", lambda **_kwargs: None)
    monkeypatch.setattr(
        attempt_module,
        "finalize_loaded_stage",
        fake_finalize_loaded_stage,
    )
    monkeypatch.setattr(attempt_module, "validate_loaded_stage_row_count", lambda **_kwargs: None)
    monkeypatch.setattr(attempt_module, "cleanup_stage", fake_cleanup_stage)
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stale_stage_tables_with_connection",
        fake_cleanup_stale_stage_tables_with_connection,
        raising=False,
    )
    monkeypatch.setattr(attempt_module, "close_connection_ref", fake_close_connection_ref)

    total_rows = attempt_module.run_transfer_attempt(
        options=options,
        read_retry_cnt=3,
        insert_retry_cnt=2,
    )

    assert total_rows == 7
    assert events == [
        "create_stage_state",
        "inspect_source_query_schema",
        "ensure_transfer_target_table",
        "load_stage_batches",
        "finalize_loaded_stage",
        "cleanup_stage",
        "close:source",
    ]


def test_run_transfer_attempt_stops_when_early_target_create_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="gp",
    )

    monkeypatch.setattr(
        attempt_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )
    monkeypatch.setattr(
        attempt_module,
        "create_stage_state",
        lambda *_args, **_kwargs: models_module.TransferStageState(
            target_exists=False,
            target_existed_at_start=False,
        ),
    )
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                name="id",
                native_type="integer",
                precision=None,
                scale=None,
            )
        ],
    )

    def fail_ensure_transfer_target_table(*_args: Any, **_kwargs: Any) -> None:
        events.append("ensure_transfer_target_table")
        raise RuntimeError("schema missing")

    def fail_load_stage_batches(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("stage batches must not start")

    def fake_cleanup_stage(*_args: Any, **kwargs: Any) -> None:
        events.append(f"cleanup:{kwargs['drop_created_target']}")

    monkeypatch.setattr(
        attempt_module,
        "ensure_transfer_target_table",
        fail_ensure_transfer_target_table,
    )
    monkeypatch.setattr(attempt_module, "load_stage_batches", fail_load_stage_batches)
    monkeypatch.setattr(attempt_module, "cleanup_stage", fake_cleanup_stage)

    with pytest.raises(RuntimeError, match="schema missing"):
        attempt_module.run_transfer_attempt(
            options=options,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )

    assert events == ["ensure_transfer_target_table", "cleanup:True"]


def test_run_transfer_attempt_validates_expected_streamed_and_stage_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source_conn = FakeTransferConnection("source")
    target_conn = FakeTransferConnection("target")
    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="gp",
        from_db_key="source_db",
        from_db_backend="gp",
        validate_row_count=True,
    )

    def fake_get_sql_connection(connection_key: str) -> FakeTransferConnection:
        return source_conn if connection_key == "source_db" else target_conn

    def fake_create_stage_state(*_args: Any, **_kwargs: Any) -> models_module.TransferStageState:
        events.append("create_stage_state")
        return models_module.TransferStageState(target_exists=False)

    def fake_load_stage_batches(**kwargs: Any) -> int:
        events.append("load_stage_batches")
        kwargs["stage_state"].stage_table = "sandbox.target__stage__abcd1234"
        return 7

    monkeypatch.setattr(attempt_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(attempt_module, "create_stage_state", fake_create_stage_state)
    monkeypatch.setattr(
        attempt_module,
        "inspect_source_query_schema",
        lambda *_args, **_kwargs: [
            SimpleNamespace(name="id", native_type="integer", precision=None, scale=None)
        ],
    )
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *a, **k: None)
    monkeypatch.setattr(attempt_module, "load_stage_batches", fake_load_stage_batches)
    monkeypatch.setattr(
        attempt_module, "finalize_loaded_stage", lambda *a, **k: events.append("finalize")
    )
    monkeypatch.setattr(attempt_module, "cleanup_stage", lambda *a, **k: events.append("cleanup"))
    monkeypatch.setattr(
        attempt_module, "close_connection_ref", lambda *a, **k: events.append("close")
    )
    monkeypatch.setattr(
        row_counts_module,
        "count_source_rows",
        lambda *_args, **_kwargs: events.append("count_source") or 7,
    )
    monkeypatch.setattr(
        row_counts_module,
        "count_table_rows",
        lambda *_args, **_kwargs: events.append("count_stage") or 7,
    )

    total_rows = attempt_module.run_transfer_attempt(
        options=options,
        read_retry_cnt=1,
        insert_retry_cnt=1,
    )

    assert total_rows == 7
    assert options.row_count_result is not None
    assert options.row_count_result.expected_source_rows == 7
    assert options.row_count_result.streamed_rows == 7
    assert options.row_count_result.stage_rows == 7
    assert options.row_count_result.row_count_validated is True
    assert events == [
        "create_stage_state",
        "count_source",
        "load_stage_batches",
        "count_stage",
        "finalize",
        "cleanup",
        "close",
    ]


def test_transfer_append_runs_once_and_metadata_target_count_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(replace_target_table=False, write_mode="append")
    closed: list[str] = []
    option_inputs: list[dict[str, Any]] = []

    def build_options(**kwargs: Any) -> Any:
        option_inputs.append(kwargs)
        return options

    monkeypatch.setattr(transfer_api_module, "build_transfer_options", build_options)
    monkeypatch.setattr(
        transfer_api_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            transfer_attempt_policy=lambda _retry_cnt: SimpleNamespace(
                retry_ambiguous_stage_load=False,
                insert_retry_cnt=1,
            )
        ),
    )
    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", lambda **_k: 4)
    monkeypatch.setattr(
        transfer_api_module,
        "run_annotated_once",
        lambda *, operation, context: operation(),
    )

    class Target:
        def close(self) -> None:
            closed.append("close")
            message = "ignored close"
            raise RuntimeError(message)

    monkeypatch.setattr(transfer_api_module, "get_sql_connection", lambda _key: Target())
    monkeypatch.setattr(
        transfer_api_module,
        "count_table_rows",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("count failed")),
    )

    result = transfer_api_module.transfer_table("source", "target", return_metadata=True)

    assert result.rows == 4
    assert option_inputs[0]["write_mode"] == "append"
    assert result.metadata.final_target_rows is None
    assert closed == ["close"]
