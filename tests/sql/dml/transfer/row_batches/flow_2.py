from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    DatabaseError,
    FakeTransferConnection,
    ProtocolError,
    SimpleNamespace,
    attempt_module,
    finalize_module,
    make_progress_options,
    models_module,
    pytest,
    source_module,
    stage_identity_module,
    transfer_api_module,
    transfer_stage_module,
)


@pytest.mark.parametrize("target_cleanup_fails", [False, True])
def test_transfer_attempt_cleanup_captures_source_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
    target_cleanup_fails: bool,
) -> None:
    source_error = RuntimeError("source cleanup failed")
    target_error = RuntimeError("target cleanup failed")
    monkeypatch.setattr(
        finalize_module,
        "cleanup_materialized_sources",
        lambda *_args: (_ for _ in ()).throw(source_error),
    )

    def cleanup_target(**_kwargs: Any) -> None:
        if target_cleanup_fails:
            raise target_error

    result = finalize_module.cleanup_transfer_attempt_stages(
        make_progress_options(),
        models_module.TransferConnectionRefs(source={"connection": object()}),
        models_module.TransferStageState(target_exists=False),
        1,
        None,
        cleanup_target,
    )

    assert result is (target_error if target_cleanup_fails else source_error)


def test_transfer_attempt_cleanup_error_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(validate_row_count=False)
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
        lambda *_a: (_ for _ in ()).throw(RuntimeError("transfer")),
    )
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stage",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("cleanup")),
    )
    messages: list[str] = []
    monkeypatch.setattr(attempt_module, "time_print", messages.append)
    with pytest.raises(RuntimeError, match="transfer"):
        attempt_module.run_transfer_attempt(options, 1, 1)
    assert messages
    assert "Cleanup failed" in messages[0]


def test_transfer_attempt_cleanup_only_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(validate_row_count=False)
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
    monkeypatch.setattr(attempt_module, "inspect_source_query_schema", lambda *_a: [])
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *_a, **_k: None)
    monkeypatch.setattr(attempt_module, "load_stage_batches", lambda **_k: 0)
    monkeypatch.setattr(attempt_module, "validate_loaded_stage_row_count", lambda **_k: None)
    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", lambda **_k: None)
    monkeypatch.setattr(
        attempt_module,
        "cleanup_stage",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("cleanup only")),
    )
    with pytest.raises(RuntimeError, match="cleanup only"):
        attempt_module.run_transfer_attempt(options, 1, 1)


@pytest.mark.parametrize(
    ("table_schema", "source_schema", "expected_types"),
    [
        ({"id": "BIGINT"}, [SimpleNamespace(name="id", native_type="int")], {"id": "BIGINT"}),
        ({"id": "BIGINT"}, [], {"id": "BIGINT"}),
        (None, [SimpleNamespace(name="id", native_type="int")], {"id": "MAPPED"}),
    ],
)
def test_transfer_attempt_schema_selection_matrix(
    monkeypatch: pytest.MonkeyPatch,
    table_schema: dict[str, str] | None,
    source_schema: list[Any],
    expected_types: dict[str, str],
) -> None:
    options = make_progress_options(table_schema=table_schema, validate_row_count=False)
    source = FakeTransferConnection("source")
    state = models_module.TransferStageState(target_exists=False)
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
        lambda *_a: source_schema,
    )
    monkeypatch.setattr(
        attempt_module,
        "validate_table_schema_columns",
        lambda schema, _cols: schema,
    )
    monkeypatch.setattr(
        attempt_module,
        "map_source_schema_to_target",
        lambda *_a, **_k: {"id": "MAPPED"},
    )
    monkeypatch.setattr(attempt_module, "ensure_transfer_target_table", lambda *_a, **_k: None)
    monkeypatch.setattr(attempt_module, "load_stage_batches", lambda **_k: 0)
    monkeypatch.setattr(attempt_module, "validate_loaded_stage_row_count", lambda **_k: None)
    monkeypatch.setattr(attempt_module, "finalize_loaded_stage", lambda **_k: None)
    monkeypatch.setattr(attempt_module, "cleanup_stage", lambda **_k: None)

    assert attempt_module.run_transfer_attempt(options, 1, 1) == 0
    assert state.stage_column_types == expected_types


def test_transfer_creates_missing_target_before_stage_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    options = make_progress_options(
        to_db_key="target_db",
        to_db_backend="gp",
        target_table="sandbox.target",
        gp_distributed_by_key=["id"],
    )
    source_columns = ["id", "__analytics_toolkit_transfer_id"]
    internal_columns = stage_identity_module.resolve_internal_columns(source_columns, "gp")
    stage_state = models_module.TransferStageState(
        target_exists=False,
        target_existed_at_start=False,
        source_columns=source_columns,
        stage_column_types={
            "id": "INTEGER",
            "__analytics_toolkit_transfer_id": "VARCHAR",
            internal_columns.transfer_id: "TEXT",
            internal_columns.destination_table: "TEXT",
            internal_columns.slice_id: "BIGINT",
            internal_columns.row_ordinal: "BIGINT",
        },
        internal_columns=internal_columns,
    )

    def fake_ensure_stage_target_table(**kwargs: Any) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        transfer_stage_module,
        "_ensure_stage_target_table",
        fake_ensure_stage_target_table,
    )

    transfer_stage_module.ensure_transfer_target_table(
        options,
        models_module.TransferConnectionRefs(
            target={"connection": FakeTransferConnection("target")}
        ),
        stage_state,
        source_columns,
    )

    assert stage_state.target_exists is True
    assert stage_state.target_created_by_operation is True
    assert calls[0]["target_table"] == "sandbox.target"
    assert calls[0]["target_column_types"] == {
        "id": "INTEGER",
        "__analytics_toolkit_transfer_id": "VARCHAR",
    }
    assert list(calls[0]["sample_batch"].columns) == source_columns


@pytest.mark.parametrize(
    "message",
    [
        (
            "Received ClickHouse exception, code: 32, server response: Code: 32. "
            "DB::Exception: Attempt to read after eof: Cannot parse Int64 from String, "
            "because value is too short: while executing 'FUNCTION CAST(customer_id, "
            "Int64)'. (ATTEMPT_TO_READ_AFTER_EOF)"
        ),
        (
            "Received ClickHouse exception, code: 36, server response: Code: 36. "
            "DB::Exception: Macro 'uuid' in engine arguments requires an explicit UUID. "
            "(BAD_ARGUMENTS)"
        ),
        (
            "Received ClickHouse exception, code: 53, server response: Code: 53. "
            "DB::Exception: Sharding expression has type Float64, but should be one of "
            "integer type. (TYPE_MISMATCH)"
        ),
    ],
)
def test_transfer_does_not_full_retry_clickhouse_deterministic_error(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    options = make_progress_options(
        from_db_key="ch_source",
        from_db_backend="ch",
        to_db_key="gp_target",
        to_db_backend="gp",
        target_table="sandbox.target",
        replace_target_table=True,
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=5,
        full_timeout_increment=0,
    )
    error = DatabaseError(message)
    attempts: list[int] = []
    monkeypatch.setattr(transfer_api_module, "build_transfer_options", lambda **_k: options)

    def fail_attempt(**_kwargs: Any) -> int:
        attempts.append(1)
        raise error

    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", fail_attempt)

    with pytest.raises(DatabaseError) as caught:
        transfer_api_module.transfer_table("ch_source", "gp_target")

    assert caught.value is error
    assert attempts == [1]


def test_transfer_does_not_full_retry_missing_trino_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        to_db_key="trino",
        to_db_backend="trino",
        target_table="pa_core_sandbox.karapsin_temp_users_filter",
        replace_target_table=True,
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=5,
        full_timeout_increment=0,
    )
    error = ValueError(
        "Trino table operations for schema-qualified names require .connections['trino'].catalog."
    )
    attempts: list[int] = []
    monkeypatch.setattr(transfer_api_module, "build_transfer_options", lambda **_k: options)

    def fail_attempt(**_kwargs: Any) -> int:
        attempts.append(1)
        raise error

    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", fail_attempt)

    with pytest.raises(ValueError, match="schema-qualified names require") as caught:
        transfer_api_module.transfer_table("gp", "trino")

    assert caught.value is error
    assert attempts == [1]


def test_transfer_exhausted_clickhouse_stream_failure_reports_retry_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        from_db_key="ch_source",
        from_db_backend="ch",
        to_db_key="gp_target",
        to_db_backend="gp",
        target_table="sandbox.target",
        batch_size=100,
        min_batch_size=10,
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        progress=False,
        validate_row_count=False,
    )
    monkeypatch.setattr(
        transfer_api_module,
        "build_transfer_options",
        lambda **_kwargs: options,
    )
    monkeypatch.setattr(
        transfer_api_module,
        "run_transfer_attempt",
        lambda **kwargs: (_ for _ in ()).throw(
            source_module.TransferSourceStreamReadError(
                connection_key="ch_source",
                backend="ch",
                query=kwargs["options"].source_sql,
                original_exception=ProtocolError("unexpected failure to read next chunk"),
            )
        ),
    )

    with pytest.raises(
        source_module.TransferSourceStreamReadError,
        match=("target_table=sandbox.target; full_retry_attempt=1; retry_batch_size=100"),
    ):
        transfer_api_module.transfer_table(
            from_db="ch_source",
            to_db="gp_target",
            from_sql="select id from source_table",
            to_table="sandbox.target",
        )


def test_transfer_failure_cleanup_drops_only_target_absent_at_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped: list[str] = []
    options = make_progress_options(to_db_key="target_db", to_db_backend="gp")

    monkeypatch.setattr(
        finalize_module,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation(
            {"connection": FakeTransferConnection("target")}
        ),
    )
    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda _backend, _key, _ref, table_name, **_kwargs: dropped.append(table_name),
    )

    finalize_module.cleanup_stage(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(),
        stage_state=models_module.TransferStageState(
            target_exists=True,
            target_existed_at_start=False,
            target_created_by_operation=True,
        ),
        read_retry_cnt=1,
        drop_created_target=True,
    )
    finalize_module.cleanup_stage(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(),
        stage_state=models_module.TransferStageState(
            target_exists=True,
            target_existed_at_start=True,
            target_created_by_operation=True,
        ),
        read_retry_cnt=1,
        drop_created_target=True,
    )

    assert dropped == [options.target_table]


def test_transfer_restarts_ambiguous_stage_load_and_uses_policy_retry_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        replace_target_table=True,
        retry_cnt=2,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
    )
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(transfer_api_module, "build_transfer_options", lambda **_k: options)
    monkeypatch.setattr(
        transfer_api_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            transfer_attempt_policy=lambda _retry_cnt: SimpleNamespace(
                retry_ambiguous_stage_load=True,
                insert_retry_cnt=0,
            )
        ),
    )

    def attempt(**kwargs: Any) -> int:
        calls.append((kwargs["read_retry_cnt"], kwargs["insert_retry_cnt"]))
        if len(calls) == 1:
            message = "unknown commit"
            raise transfer_api_module.AmbiguousTableLoadError(message)
        return 3

    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", attempt)
    monkeypatch.setattr(
        transfer_api_module,
        "run_retrying_operation",
        lambda **kwargs: kwargs["operation"](1),
    )

    assert transfer_api_module.transfer_table("source", "target") == 3
    assert calls == [(2, 0), (2, 0)]


def test_transfer_retries_clickhouse_stream_failure_with_smaller_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        from_db_key="ch_source",
        from_db_backend="ch",
        to_db_key="gp_target",
        to_db_backend="gp",
        target_table="sandbox.target",
        batch_size=100,
        min_batch_size=10,
        max_batch_size=500,
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=2,
        full_timeout_increment=0,
        progress=False,
        validate_row_count=False,
    )
    attempts: list[tuple[int, int | None]] = []

    monkeypatch.setattr(
        transfer_api_module,
        "build_transfer_options",
        lambda **_kwargs: options,
    )

    def fake_run_transfer_attempt(
        *,
        options: Any,
        read_retry_cnt: int,
        insert_retry_cnt: int,
    ) -> int:
        del read_retry_cnt, insert_retry_cnt
        attempts.append((options.batch_size, options.max_batch_size))
        if len(attempts) == 1:
            raise source_module.TransferSourceStreamReadError(
                connection_key="ch_source",
                backend="ch",
                query=options.source_sql,
                original_exception=ProtocolError("unexpected failure to read next chunk"),
            )
        return 3

    monkeypatch.setattr(
        transfer_api_module,
        "run_transfer_attempt",
        fake_run_transfer_attempt,
    )

    rows = transfer_api_module.transfer_table(
        from_db="ch_source",
        to_db="gp_target",
        from_sql="select id from source_table",
        to_table="sandbox.target",
    )

    assert rows == 3
    assert attempts == [(100, 500), (50, 50)]


@pytest.mark.parametrize("estimate_total_rows", [None, 0, 1, "yes"])
def test_transfer_table_validates_estimate_total_rows(
    estimate_total_rows: Any,
) -> None:
    with pytest.raises(ValueError, match="estimate_total_rows"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            dry_run=True,
            estimate_total_rows=estimate_total_rows,
        )


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_transfer_table_validates_progress(progress: Any) -> None:
    with pytest.raises(ValueError, match="progress"):
        transfer_api_module.transfer_table(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            dry_run=True,
            progress=progress,
        )


def test_transfer_truncate_dry_run_orders_clear_before_insert() -> None:
    plan = transfer_api_module.transfer_table(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        write_mode="truncate_insert",
        table_schema={"id": "BIGINT"},
        dry_run=True,
    )
    phases = [statement.phase for statement in plan.statements]
    assert phases.index("clear_target") < phases.index("insert_target")


def test_transfer_upsert_precondition_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = {
        "from_db": "gp",
        "to_db": "trino",
        "from_sql": "select id from source_table",
        "to_table": "sandbox.target",
        "write_mode": "upsert",
        "key_columns": ["id"],
        "dry_run": True,
    }
    with pytest.raises(ValueError, match="upsert_partition_column"):
        transfer_api_module.transfer_table(**common)

    adapter = transfer_api_module.get_backend_adapter("trino")
    monkeypatch.setattr(adapter, "needs_upsert_partition_drop_template", lambda: True)
    defaults = adapter.target_connection_defaults(
        transfer_api_module.get_connection_config("trino")
    )
    monkeypatch.setattr(
        adapter,
        "target_connection_defaults",
        lambda _config: SimpleNamespace(
            s3_transfer_staging_location=defaults.s3_transfer_staging_location,
            upsert_partition_drop_sql_template=None,
            insert_chunk_size=defaults.insert_chunk_size,
        ),
    )
    with pytest.raises(ValueError, match="drop_sql_template"):
        transfer_api_module.transfer_table(**common, upsert_partition_column="event_date")
