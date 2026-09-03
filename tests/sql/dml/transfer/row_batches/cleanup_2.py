from __future__ import annotations

from dataclasses import replace

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    SimpleNamespace,
    backends_module,
    finalize_module,
    make_progress_options,
    models_module,
    pytest,
    staging_module,
    warnings,
)


def test_cleanup_stale_stage_tables_preserves_trino_catalog_schema_for_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
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
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables_with_connection(
        db_key="trino",
        target_table="analytics.target",
        connection_ref={"connection": object()},
        read_retry_cnt=3,
        stage_tables=[
            "stage_x",
            "iceberg.scratch.stage_y",
        ],
    )

    assert discovered == [
        'hive.scratch."stage_x"',
        "iceberg.scratch.stage_y",
    ]


def test_cleanup_stale_stage_tables_public_clean_all_allows_missing_target_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeTransferConnection("target")
    discovered: list[str] = []
    monkeypatch.setattr(staging_module, "get_sql_connection", lambda db_key: connection)
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
        lambda connection, *, connection_key, transfer_staging_schema, table_pattern: [
            "target__analytics_toolkit_target_user__stage__match",
        ],
    )
    monkeypatch.setattr(
        staging_module,
        "cleanup_stage_table_with_retry",
        lambda *args, **kwargs: discovered.append(args[3]),
    )

    staging_module.cleanup_stale_stage_tables("gp", clean_all=True)

    assert discovered == ["transfer_schema.target__analytics_toolkit_target_user__stage__match"]
    assert connection.close_calls == 1


def test_cleanup_stale_stage_tables_quotes_discovered_gp_stage_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered: list[str] = []
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema="pa_core_stage",
            user="karapsin_de",
        ),
    )
    monkeypatch.setattr(
        backends_module.get_backend_adapter("gp"),
        "query_transfer_stage_table_names",
        lambda connection, *, connection_key, transfer_staging_schema, table_pattern: [
            "26cc4c2__analytics_toolkit_karapsin_de__stage__9bd5fbfe__w00000",
        ],
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
        clean_all=True,
    )

    assert discovered == [
        'pa_core_stage."26cc4c2__analytics_toolkit_karapsin_de__stage__9bd5fbfe__w00000"'
    ]


def test_cleanup_stale_stage_tables_rejects_unqualified_explicit_without_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        staging_module,
        "get_connection_config",
        lambda db_key: SimpleNamespace(
            connection_key=db_key,
            backend="gp",
            transfer_staging_schema=None,
            user="target_user",
        ),
    )

    with pytest.raises(
        staging_module.InvalidSqlInputError,
        match="Unqualified stage table names require transfer_staging_schema",
    ):
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="gp",
            target_table="analytics.target",
            connection_ref={"connection": object()},
            read_retry_cnt=3,
            stage_tables=["target__analytics_toolkit_target_user__stage__implicit"],
        )


def test_cleanup_stale_stage_tables_requires_target_table_for_target_discovery(
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
        match="target_table is required when clean_all=False and stage_tables=None",
    ):
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="gp",
            target_table=None,
            connection_ref={"connection": object()},
            read_retry_cnt=3,
        )


def test_cleanup_stale_stage_tables_suppresses_connection_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CloseFailure:
        def close(self) -> None:
            message = "close failed"
            raise RuntimeError(message)

    monkeypatch.setattr(staging_module, "get_sql_connection", lambda _key: CloseFailure())
    monkeypatch.setattr(
        staging_module,
        "cleanup_stale_stage_tables_with_connection",
        lambda **_kwargs: None,
    )
    staging_module.cleanup_stale_stage_tables("gp", stage_tables=[])


def test_cleanup_stale_stage_tables_warns_once_when_staging_schema_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        connection_key="staging_warning_db",
        backend="gp",
        transfer_staging_schema=None,
        user="target_user",
    )
    connection_ref: dict[str, Any] = {"connection": FakeTransferConnection("target")}
    monkeypatch.setattr(staging_module, "get_connection_config", lambda db_key: config)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="staging_warning_db",
            target_table="schema.target",
            connection_ref=connection_ref,
            read_retry_cnt=1,
        )
        staging_module.cleanup_stale_stage_tables_with_connection(
            db_key="staging_warning_db",
            target_table="schema.target",
            connection_ref=connection_ref,
            read_retry_cnt=1,
        )

    assert len(caught) == 1
    assert (
        "clean_transfer_staging_schema is enabled, "
        "but transfer_staging_schema is not configured for the target connection"
        in str(caught[0].message)
    )


def test_finalize_empty_transfer_warns_only_for_missing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = replace(
        make_progress_options(write_mode="replace", replace_target_table=True),
        empty_source_policy="keep",
    )
    messages: list[str] = []
    monkeypatch.setattr(
        finalize_module,
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )
    with pytest.warns(UserWarning, match="zero rows"):
        finalize_module.finalize_empty_transfer(
            options,
            models_module.TransferConnectionRefs(),
            models_module.TransferStageState(target_exists=False),
        )
    finalize_module.finalize_empty_transfer(
        options,
        models_module.TransferConnectionRefs(),
        models_module.TransferStageState(target_exists=True),
    )
    assert len(messages) == 2


def test_finalize_empty_transfer_replaces_target_through_empty_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = replace(
        make_progress_options(write_mode="replace", replace_target_table=True),
        empty_source_policy="replace",
    )
    state = models_module.TransferStageState(
        target_exists=True,
        source_columns=["id", "missing"],
        stage_column_types={"id": "BIGINT"},
    )
    finalizations: list[tuple[Any, ...]] = []

    def runner(_role: str, operation: Any) -> Any:
        return operation({"connection": object()})

    monkeypatch.setattr(
        finalize_module,
        "create_stage_table",
        lambda **_kwargs: "scratch.empty_stage",
    )
    monkeypatch.setattr(
        finalize_module,
        "_finalize_target_once",
        lambda *args, **_kwargs: finalizations.append(args),
    )

    finalize_module.finalize_loaded_stage(
        options,
        models_module.TransferConnectionRefs(),
        state,
        total_rows=0,
        target_connection_runner=runner,
    )

    assert state.stage_table == "scratch.empty_stage"
    assert state.stage_table_created is True
    assert state.stage_table_candidates == ["scratch.empty_stage"]
    assert list(state.first_non_empty_batch.columns) == ["id", "missing"]
    assert state.insert_column_types == {"id": "BIGINT"}
    assert finalizations[0][0].write_mode == "replace"


def test_finalize_empty_transfer_error_and_explicit_schema_paths() -> None:
    options = replace(
        make_progress_options(write_mode="replace", replace_target_table=True),
        empty_source_policy="error",
    )
    with pytest.raises(finalize_module.EmptySourceError, match="zero rows"):
        finalize_module.finalize_empty_transfer(
            options,
            models_module.TransferConnectionRefs(),
            models_module.TransferStageState(target_exists=True),
        )

    schema_options = replace(options, table_schema={"id": "INTEGER"})
    assert finalize_module._empty_transfer_target_types(
        schema_options,
        models_module.TransferStageState(target_exists=True),
    ) == {"id": "INTEGER"}
    assert (
        finalize_module._empty_transfer_target_types(
            options,
            models_module.TransferStageState(target_exists=True),
        )
        is None
    )


def test_finalize_empty_transfer_reuses_existing_empty_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = replace(
        make_progress_options(write_mode="replace", replace_target_table=True),
        empty_source_policy="replace",
    )
    state = models_module.TransferStageState(
        target_exists=True,
        first_non_empty_batch=finalize_module.pd.DataFrame({"id": []}),
        stage_table="scratch.existing_empty",
        stage_column_types={"id": "BIGINT"},
        source_columns=["id"],
    )
    monkeypatch.setattr(
        finalize_module,
        "create_stage_table",
        lambda **_kwargs: pytest.fail("existing empty stage must be reused"),
    )
    monkeypatch.setattr(finalize_module, "_finalize_target_once", lambda *_args, **_kwargs: None)

    finalize_module.finalize_empty_transfer(
        options,
        models_module.TransferConnectionRefs(),
        state,
    )

    assert state.stage_table_created is False


def test_preclear_failure_is_preserved_and_incomplete_target_cleanup_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = replace(
        make_progress_options(
            to_db_key="ch",
            to_db_backend="ch",
            write_mode="replace",
            replace_target_table=True,
        ),
    )
    state = models_module.TransferStageState(target_exists=True)

    class Adapter:
        def needs_bounded_replace_preclear(self, _only_shard: bool) -> bool:
            return True

        def preclear_distributed_replace_target(self, *_args: Any, **_kwargs: Any) -> None:
            message = "preclear failed"
            raise OSError(message)

    monkeypatch.setattr(finalize_module, "get_backend_adapter", lambda _backend: Adapter())

    def runner(_role: str, operation: Any) -> Any:
        return operation({"connection": object()})

    with pytest.raises(OSError, match="preclear failed"):
        finalize_module._preclear_clickhouse_replace_target(
            options,
            state,
            target_connection_runner=runner,
            target_host_connection_runner=lambda _host, _operation: None,
        )

    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda *_args, **_kwargs: cleanup_calls.append("cleanup"),
    )
    finalize_module._drop_incomplete_fresh_target(
        options,
        target_connection_runner=runner,
    )
    assert cleanup_calls == ["cleanup"]
