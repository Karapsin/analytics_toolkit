from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    SimpleNamespace,
    finalize_module,
    general_module,
    make_progress_options,
    models_module,
    pd,
    pytest,
)


def test_ensure_final_upsert_stage_guard_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_upsert = make_progress_options(write_mode="append")
    state = models_module.TransferStageState(target_exists=True)
    finalize_module._ensure_final_upsert_stage_table(non_upsert, state)

    upsert = make_progress_options(write_mode="upsert")
    monkeypatch.setattr(
        finalize_module,
        "get_backend_adapter",
        lambda _b: SimpleNamespace(uses_partition_replacement_upsert=lambda: False),
    )
    finalize_module._ensure_final_upsert_stage_table(upsert, state)
    monkeypatch.setattr(
        finalize_module,
        "get_backend_adapter",
        lambda _b: SimpleNamespace(uses_partition_replacement_upsert=lambda: True),
    )
    finalize_module._ensure_final_upsert_stage_table(
        upsert, models_module.TransferStageState(target_exists=False)
    )
    finalize_module._ensure_final_upsert_stage_table(
        upsert,
        models_module.TransferStageState(target_exists=True, final_upsert_stage_table="already"),
    )


def test_ensure_final_upsert_stage_table_creates_partition_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(write_mode="upsert")
    state = models_module.TransferStageState(
        target_exists=True,
        first_non_empty_batch=pd.DataFrame({"id": [1]}),
        stage_column_types={"id": "BIGINT"},
        insert_column_types={"id": "INTEGER"},
    )
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(
        finalize_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(uses_partition_replacement_upsert=lambda: True),
    )
    monkeypatch.setattr(
        finalize_module,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )

    def create_stage(**kwargs: Any) -> str:
        created.append(kwargs)
        kwargs["on_stage_candidate"]("stage.final")
        return "stage.final"

    monkeypatch.setattr(finalize_module, "create_stage_table", create_stage)

    finalize_module._ensure_final_upsert_stage_table(options, state)

    assert state.final_upsert_stage_table == "stage.final"
    assert state.stage_table_candidates == ["stage.final"]
    assert created[0]["column_types"] == {"id": "INTEGER"}


def test_ensure_final_upsert_stage_table_requires_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(write_mode="upsert")
    state = models_module.TransferStageState(target_exists=True)
    monkeypatch.setattr(
        finalize_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(uses_partition_replacement_upsert=lambda: True),
    )
    with pytest.raises(RuntimeError, match="sample batch"):
        finalize_module._ensure_final_upsert_stage_table(options, state)


def test_final_upsert_partial_stage_candidate_remains_visible_to_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(write_mode="upsert")
    state = models_module.TransferStageState(
        target_exists=True,
        stage_table_created=True,
        stage_table="stage.primary",
        first_non_empty_batch=pd.DataFrame({"id": [1]}),
        stage_column_types={"id": "BIGINT"},
    )
    monkeypatch.setattr(
        finalize_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(uses_partition_replacement_upsert=lambda: True),
    )
    monkeypatch.setattr(
        finalize_module,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )

    def partial_create(**kwargs: Any) -> str:
        kwargs["on_stage_candidate"]("stage.partial_upsert")
        message = "distributed create failed after shard creation"
        raise OSError(message)

    monkeypatch.setattr(finalize_module, "create_stage_table", partial_create)

    with pytest.raises(OSError, match="distributed create failed"):
        finalize_module._ensure_final_upsert_stage_table(options, state)

    assert state.final_upsert_stage_table == "stage.partial_upsert"
    assert finalize_module._stage_tables_to_cleanup(state) == [
        "stage.primary",
        "stage.partial_upsert",
    ]


def test_finalize_existing_target_schema_and_cleanup_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(write_mode="append", replace_target_table=False)
    state = models_module.TransferStageState(
        target_exists=True,
        stage_table="stage.one",
        stage_tables=["stage.one", "stage.one"],
        final_upsert_stage_table="stage.final",
        stage_table_created=True,
        first_non_empty_batch=pd.DataFrame({"id": [1]}),
        stage_column_types={"id": "BIGINT"},
        stage_external_location="s3://bucket/stage",
    )
    roles: list[str] = []
    monkeypatch.setattr(
        finalize_module,
        "_run_with_fresh_target_connection",
        lambda _options, role, operation: roles.append(role) or operation({"connection": object()}),
    )
    monkeypatch.setattr(finalize_module, "validate_stage_uniqueness", lambda **_k: None)
    monkeypatch.setattr(finalize_module, "validate_stage_target_key_overlap", lambda **_k: None)
    monkeypatch.setattr(
        finalize_module,
        "get_existing_target_insert_types",
        lambda *_a, **_k: {"id": "INTEGER"},
    )
    monkeypatch.setattr(finalize_module, "finalize_stage_table", lambda *_a, **_k: None)
    monkeypatch.setattr(finalize_module, "analyze_table", lambda **_k: None)
    finalize_module.finalize_loaded_stage(options, models_module.TransferConnectionRefs(), state, 1)
    assert state.insert_column_types == {"id": "INTEGER"}
    assert "target_metadata" in roles

    cleaned: list[str] = []
    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda *_args, **_k: cleaned.append(_args[3]),
    )
    remote_error = RuntimeError("remote")
    monkeypatch.setattr(
        finalize_module,
        "cleanup_parquet_stage_location",
        lambda _location: (_ for _ in ()).throw(remote_error),
    )
    with pytest.raises(RuntimeError, match="remote"):
        finalize_module.cleanup_stage(
            options,
            models_module.TransferConnectionRefs(),
            state,
            1,
        )
    assert cleaned == ["stage.one", "stage.final"]


def test_finalize_loaded_stage_handles_empty_and_invalid_stage_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options()
    refs = models_module.TransferConnectionRefs()
    state = models_module.TransferStageState(target_exists=True)
    calls: list[str] = []
    monkeypatch.setattr(
        finalize_module,
        "finalize_empty_transfer",
        lambda *_args: calls.append("empty"),
    )

    finalize_module.finalize_loaded_stage(options, refs, state, 0)
    assert calls == ["empty"]
    with pytest.raises(RuntimeError, match="non-empty batch"):
        finalize_module.finalize_loaded_stage(options, refs, state, 1)

    state.first_non_empty_batch = pd.DataFrame({"id": [1]})
    with pytest.raises(RuntimeError, match="stage table"):
        finalize_module.finalize_loaded_stage(options, refs, state, 1)


def test_finalize_loaded_stage_validates_finalizes_and_analyzes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(write_mode="append")
    state = models_module.TransferStageState(
        target_exists=False,
        stage_table="stage.temp",
        first_non_empty_batch=pd.DataFrame({"id": [1]}),
        stage_column_types={"id": "BIGINT"},
    )
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        finalize_module,
        "_run_with_fresh_target_connection",
        lambda _options, role, operation: operation({"connection": role}),
    )
    monkeypatch.setattr(
        finalize_module,
        "validate_stage_uniqueness",
        lambda **kwargs: events.append(("unique", kwargs)),
    )
    monkeypatch.setattr(
        finalize_module,
        "validate_stage_target_key_overlap",
        lambda **kwargs: events.append(("overlap", kwargs)),
    )
    monkeypatch.setattr(
        finalize_module,
        "finalize_stage_table",
        lambda *_args, **kwargs: events.append(("finalize", kwargs)),
    )
    monkeypatch.setattr(
        finalize_module,
        "analyze_table",
        lambda **kwargs: events.append(("analyze", kwargs)),
    )

    finalize_module.finalize_loaded_stage(
        options,
        models_module.TransferConnectionRefs(),
        state,
        1,
    )

    assert [name for name, _kwargs in events] == [
        "unique",
        "overlap",
        "finalize",
        "analyze",
    ]
    assert state.insert_column_types == {"id": "BIGINT"}
    assert events[2][1]["target_column_types"] == {"id": "BIGINT"}


def test_finalize_no_types_upsert_overlap_and_cleanup_error_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(write_mode="upsert")
    state = models_module.TransferStageState(
        target_exists=True,
        stage_table="stage.one",
        first_non_empty_batch=pd.DataFrame({"id": [1]}),
        stage_column_types=None,
    )
    events: list[str] = []
    monkeypatch.setattr(
        finalize_module,
        "_run_with_fresh_target_connection",
        lambda _options, role, operation: (
            events.append(role) or operation({"connection": object()})
        ),
    )
    monkeypatch.setattr(finalize_module, "validate_stage_uniqueness", lambda **_k: None)
    monkeypatch.setattr(
        finalize_module,
        "validate_stage_target_key_overlap",
        lambda **_k: events.append("overlap"),
    )
    monkeypatch.setattr(
        finalize_module,
        "get_backend_adapter",
        lambda _b: SimpleNamespace(uses_partition_replacement_upsert=lambda: False),
    )
    monkeypatch.setattr(finalize_module, "finalize_stage_table", lambda *_a, **_k: None)
    monkeypatch.setattr(finalize_module, "analyze_table", lambda **_k: None)
    finalize_module.finalize_loaded_stage(options, models_module.TransferConnectionRefs(), state, 1)
    assert state.insert_column_types is None
    assert "overlap" not in events

    assert finalize_module._stage_tables_to_cleanup(
        models_module.TransferStageState(
            target_exists=False,
            stage_table="stage.single",
            final_upsert_stage_table="stage.final",
        )
    ) == ["stage.single", "stage.final"]

    cleanup_state = models_module.TransferStageState(
        target_exists=False,
        target_existed_at_start=False,
        target_created_by_operation=True,
        stage_external_location="s3://bucket/stage",
    )
    messages: list[str] = []
    monkeypatch.setattr(
        finalize_module,
        "cleanup_parquet_stage_location",
        lambda _location: (_ for _ in ()).throw(RuntimeError("remote")),
    )
    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("target")),
    )
    monkeypatch.setattr(
        general_module,
        "time_print",
        lambda message, **_k: messages.append(message),
    )
    with pytest.raises(RuntimeError, match="remote"):
        finalize_module.cleanup_stage(
            options,
            models_module.TransferConnectionRefs(),
            cleanup_state,
            1,
            drop_created_target=True,
        )
    assert any("remote Parquet" in message for message in messages)

    cleanup_state.stage_external_location = None
    with pytest.raises(RuntimeError, match="target"):
        finalize_module.cleanup_stage(
            options,
            models_module.TransferConnectionRefs(),
            cleanup_state,
            1,
            drop_created_target=True,
        )

    cleanup_state.stage_table = "stage.failed"
    cleanup_state.stage_table_created = True
    cleanup_state.stage_external_location = "s3://bucket/stage"
    monkeypatch.setattr(
        finalize_module,
        "cleanup_stage_table_with_retry",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("stage")),
    )
    messages.clear()
    with pytest.raises(RuntimeError, match="stage"):
        finalize_module.cleanup_stage(
            options,
            models_module.TransferConnectionRefs(),
            cleanup_state,
            1,
            drop_created_target=True,
        )
    assert any("Remote Parquet" in message for message in messages)
    assert any("Target cleanup" in message for message in messages)
