from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    SimpleNamespace,
    backends_module,
    finalize_module,
    general_module,
    make_keyed_options,
    make_progress_options,
    models_module,
    parquet_stage_module,
    pytest,
    staging_module,
    warnings,
)


def test_cleanup_and_infer_parquet_helpers_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[tuple[str, bool]] = []
    fs = SimpleNamespace(rm=lambda path, recursive: removed.append((path, recursive)))
    fsspec_module = SimpleNamespace(
        core=SimpleNamespace(url_to_fs=lambda uri: (fs, uri[len("s3://") :]))
    )
    parquet_stage_module.cleanup_parquet_stage_location(
        "s3://bucket/stage",
        fsspec_module=fsspec_module,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            infer_parquet_stage_column_types_from_rows=lambda batch: {batch.columns[0]: "BIGINT"}
        ),
    )
    batch = models_module.RowBatch(columns=["id"], rows=[(1,)])
    assert parquet_stage_module.infer_trino_column_types_from_rows(batch) == {"id": "BIGINT"}
    assert removed == [("bucket/stage", True)]


def test_cleanup_stage_drops_each_worker_stage_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_keyed_options()
    connection_refs = models_module.TransferConnectionRefs(
        target={"connection": FakeTransferConnection("target")},
    )
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table_created=True,
        stage_table="stage_w00000",
        stage_tables=["stage_w00000", "stage_w00001", "stage_w00001"],
    )
    dropped: list[str] = []

    def fake_cleanup_stage_table_with_retry(*args: Any, **_kwargs: Any) -> None:
        dropped.append(args[3])

    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        fake_cleanup_stage_table_with_retry,
    )
    monkeypatch.setattr(
        finalize_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )

    finalize_module.cleanup_stage(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
        read_retry_cnt=1,
    )

    assert dropped == ["stage_w00000", "stage_w00001"]


def test_cleanup_stage_drops_stage_table_and_removes_remote_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped: list[str | None] = []
    removed: list[str] = []
    stage_state = models_module.TransferStageState(
        target_exists=False,
        stage_table_created=True,
        stage_table="object_storage.sandbox.target__stage__abcd1234",
        stage_external_location="s3://bucket/tmp/stage/",
    )
    options = make_progress_options(
        to_db_key="trino",
        to_db_backend="trino",
        transfer_staging_schema="object_storage.sandbox",
        s3_transfer_staging_location="s3://bucket/tmp",
        trino_mode="parquet",
    )

    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda connection_type, connection_key, connection_ref, stage_table, **kwargs: (
            dropped.append(stage_table)
        ),
    )
    monkeypatch.setattr(
        finalize_module,
        "cleanup_parquet_stage_location",
        lambda stage_external_location: removed.append(stage_external_location),
    )
    monkeypatch.setattr(
        finalize_module,
        "get_sql_connection",
        lambda key: FakeTransferConnection(key),
    )

    finalize_module.cleanup_stage(
        options=options,
        connection_refs=models_module.TransferConnectionRefs(
            target={"connection": object()},
        ),
        stage_state=stage_state,
        read_retry_cnt=1,
    )

    assert dropped == ["object_storage.sandbox.target__stage__abcd1234"]
    assert removed == ["s3://bucket/tmp/stage/"]


def test_cleanup_stage_preserves_stage_cleanup_as_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options()
    state = models_module.TransferStageState(
        target_exists=False,
        target_existed_at_start=False,
        target_created_by_operation=True,
        stage_table_created=True,
        stage_table="stage.temp",
        stage_external_location="s3://bucket/stage",
    )
    stage_error = RuntimeError("stage cleanup")
    messages: list[str] = []

    def run_target(_options: Any, role: str, operation: Any) -> Any:
        if role == "cleanup_stage":
            raise stage_error
        return operation({"connection": object()})

    monkeypatch.setattr(finalize_module, "_run_with_fresh_target_connection", run_target)
    monkeypatch.setattr(
        finalize_module,
        "cleanup_parquet_stage_location",
        lambda _location: (_ for _ in ()).throw(RuntimeError("remote cleanup")),
    )
    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("target cleanup")),
    )
    monkeypatch.setattr(
        general_module,
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    with pytest.raises(RuntimeError, match="stage cleanup"):
        finalize_module.cleanup_stage(
            options,
            models_module.TransferConnectionRefs(),
            state,
            1,
            drop_created_target=True,
        )
    assert any("Remote Parquet" in message for message in messages)
    assert any("Target cleanup" in message for message in messages)

    with pytest.raises(RuntimeError, match="stage"):
        finalize_module.cleanup_stage(
            options,
            models_module.TransferConnectionRefs(),
            models_module.TransferStageState(
                target_exists=False,
                stage_table="stage.only",
                stage_table_created=True,
            ),
            1,
        )


def test_cleanup_stale_stage_tables_clean_all_drops_user_gp_stage_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    query_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target user",
        ),
    )
    monkeypatch.setattr(
        backends_module.get_backend_adapter("gp"),
        "query_transfer_stage_table_names",
        lambda connection, *, connection_key, transfer_staging_schema, table_pattern: (
            query_calls.append((transfer_staging_schema, table_pattern))
            or [
                "0123456789abcdef__target" + "a" * 32,
                "target__analytics_toolkit_target_user__stage__match",
                "other__analytics_toolkit_target_user__stage__match",
                "target__analytics_toolkit_other_user__stage__ignore",
                "plain_table",
            ]
        ),
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        clean_all=True,
    )

    assert discovered == [
        'transfer_schema."0123456789abcdef__target' + "a" * 32 + '"',
        "transfer_schema.target__analytics_toolkit_target_user__stage__match",
        "transfer_schema.other__analytics_toolkit_target_user__stage__match",
    ]
    assert query_calls == [("transfer_schema", "%")]


def test_cleanup_stale_stage_tables_clean_all_preserves_trino_catalog_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    query_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="trino",
            transfer_staging_schema="hive.scratch",
            user="target_user",
        ),
    )
    monkeypatch.setattr(
        backends_module.get_backend_adapter("trino"),
        "query_transfer_stage_table_names",
        lambda connection, *, connection_key, transfer_staging_schema, table_pattern: (
            query_calls.append((transfer_staging_schema, table_pattern))
            or [
                "target__analytics_toolkit_target_user__stage__match",
                "target__analytics_toolkit_other_user__stage__ignore",
            ]
        ),
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="trino",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        clean_all=True,
    )

    assert discovered == ["hive.scratch.target__analytics_toolkit_target_user__stage__match"]
    assert query_calls == [("hive.scratch", "%")]


def test_cleanup_stale_stage_tables_clean_all_rejects_explicit_stage_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target_user",
        ),
    )

    with pytest.raises(
        staging_module.InvalidSqlInputError,
        match="clean_all=True cannot be combined with explicit stage_tables",
    ):
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="gp",
            target_table="analytics.target",
            connection_ref={"connection": object()},
            read_retry_cnt=3,
            clean_all=True,
            stage_tables=["target__analytics_toolkit_target_user__stage__explicit"],
        )


def test_cleanup_stale_stage_tables_clean_all_warns_once_when_schema_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        connection_key="clean_all_staging_warning_db",
        backend="gp",
        transfer_staging_schema=None,
        user="target_user",
    )
    connection_ref: dict[str, Any] = {"connection": FakeTransferConnection("target")}
    monkeypatch.setattr(staging_module, "get_connection_config", lambda db_key: config)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="clean_all_staging_warning_db",
            target_table="schema.target",
            connection_ref=connection_ref,
            read_retry_cnt=1,
            clean_all=True,
        )
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="clean_all_staging_warning_db",
            target_table="schema.target",
            connection_ref=connection_ref,
            read_retry_cnt=1,
            clean_all=True,
        )

    assert len(caught) == 1
    assert (
        "clean_transfer_staging_schema is enabled, "
        "but transfer_staging_schema is not configured for the target connection"
        in str(caught[0].message)
    )


def test_cleanup_stale_stage_tables_discovers_matching_gp_stage_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    query_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target user",
        ),
    )
    monkeypatch.setattr(
        backends_module.get_backend_adapter("gp"),
        "query_transfer_stage_table_names",
        lambda connection, *, connection_key, transfer_staging_schema, table_pattern: (
            query_calls.append((transfer_staging_schema, table_pattern))
            or [
                "bbf9072adfdac666__target" + "a" * 32,
                "target__analytics_toolkit_target_user__stage__match",
                "other__analytics_toolkit_target_user__stage__ignore",
            ]
        ),
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
    )

    assert discovered == [
        "transfer_schema.bbf9072adfdac666__target" + "a" * 32,
        "transfer_schema.target__analytics_toolkit_target_user__stage__match",
    ]
    assert query_calls == [
        ("transfer_schema", "bbf9072adfdac666__%"),
        ("transfer_schema", "target__analytics_toolkit_target_user__stage__%"),
    ]


def test_cleanup_stale_stage_tables_drops_explicit_stage_tables_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    query_called = 0

    def fake_query_stage_tables(
        connection: Any,
        *,
        connection_key: str,
        transfer_staging_schema: str,
        table_pattern: str,
    ) -> list[str]:
        nonlocal query_called
        query_called += 1
        del connection, connection_key, transfer_staging_schema, table_pattern
        raise AssertionError("query should not be used for explicit stage tables")

    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target_user",
        ),
    )
    monkeypatch.setattr(
        backends_module.get_backend_adapter("gp"),
        "query_transfer_stage_table_names",
        fake_query_stage_tables,
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        stage_tables=[
            "analytics.target__analytics_toolkit_target_user__stage__explicit",
            "target__analytics_toolkit_target_user__stage__implicit",
        ],
    )

    assert discovered == [
        "analytics.target__analytics_toolkit_target_user__stage__explicit",
        "transfer_schema.target__analytics_toolkit_target_user__stage__implicit",
    ]
    assert query_called == 0


def test_cleanup_stale_stage_tables_empty_explicit_list_drops_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_called = 0
    query_called = 0

    def fake_query_stage_tables(
        connection: Any,
        *,
        connection_key: str,
        transfer_staging_schema: str,
        table_pattern: str,
    ) -> list[str]:
        nonlocal query_called
        query_called += 1
        del connection, connection_key, transfer_staging_schema, table_pattern
        return ["target__analytics_toolkit_target_user__stage__stale"]

    def fake_cleanup_stage_table_with_retry(*_args: Any, **_kwargs: Any) -> None:
        nonlocal cleanup_called
        cleanup_called += 1

    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target_user",
        ),
    )
    monkeypatch.setattr(
        backends_module.get_backend_adapter("gp"),
        "query_transfer_stage_table_names",
        fake_query_stage_tables,
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        fake_cleanup_stage_table_with_retry,
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        stage_tables=[],
    )

    assert cleanup_called == 0
    assert query_called == 0


def test_cleanup_stale_stage_tables_explicit_stage_tables_allow_missing_target_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="transfer_schema",
            user="target_user",
        ),
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="gp",
        target_table=None,
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        stage_tables=["target__analytics_toolkit_target_user__stage__explicit"],
    )

    assert discovered == ["transfer_schema.target__analytics_toolkit_target_user__stage__explicit"]
