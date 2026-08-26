from __future__ import annotations

from tests.sql._support.transfer_keyed import (
    Any,
    LazyKeyedRuntime,
    SimpleNamespace,
    TransferOptions,
    TransferStageState,
    _concurrency,
    _Manager,
    _metadata,
    _options,
    _patch_attempt_shell,
    _ProgressBar,
    _task,
    pytest,
    staged_keyed_io,
    staged_keyed_pipeline,
    threading,
)


def test_attempt_cleanup_failure_after_success_is_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def complete_loading(
        current_options: TransferOptions,
        _metadata_value: Any,
        _stage_state: TransferStageState,
        _runtime: LazyKeyedRuntime,
        _source_connections: Any,
        _target_connections: Any,
        progress: Any,
        **_kwargs: Any,
    ) -> None:
        for transfer_slice in current_options.transfer_slices or []:
            progress.start_key(transfer_slice.index)
            progress.materialize_key(transfer_slice.index, 0)
            progress.assign_key(transfer_slice.index, 0)
            progress.verify_key(transfer_slice.index)

    options, _state = _patch_attempt_shell(
        monkeypatch,
        run_workers=complete_loading,
        cleanup_stage=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("target cleanup failed")
        ),
    )

    with pytest.raises(staged_keyed_pipeline.FinalizedTargetCleanupError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert exc_info.value.analytics_toolkit_sql_retry_safe is False
    assert "after destination finalization" in str(exc_info.value)


@pytest.mark.parametrize("error_kind", ["no_dict", "no_note"])
def test_attempt_cleanup_metadata_failure_uses_cleanup_error_precedence(
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
) -> None:
    class NoDictError(RuntimeError):
        @property
        def __dict__(self) -> dict[str, Any]:
            message = "error metadata is immutable"
            raise AttributeError(message)

    class NoNoteError(RuntimeError):
        add_note = None

    original = (
        NoDictError("worker failed") if error_kind == "no_dict" else NoNoteError("worker failed")
    )
    options, _state = _patch_attempt_shell(
        monkeypatch,
        run_workers=lambda *_args, **_kwargs: (_ for _ in ()).throw(original),
        cleanup_stage=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("target cleanup failed")
        ),
    )

    expected = (
        staged_keyed_pipeline.FinalizedTargetCleanupError
        if error_kind == "no_dict"
        else NoNoteError
    )
    with pytest.raises(expected):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    if error_kind == "no_note":
        assert original.analytics_toolkit_sql_retry_safe is False


def test_attempt_error_before_stage_state_skips_target_stage_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    original = RuntimeError("stage state creation failed")
    cleanup_calls: list[str] = []
    monkeypatch.setattr(staged_keyed_pipeline, "BoundedConnectionManager", _Manager)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_stage_state",
        lambda *_args: (_ for _ in ()).throw(original),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_failed_empty_source_stages",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_stage",
        lambda *_args, **_kwargs: cleanup_calls.append("target stage"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert exc_info.value is original
    assert cleanup_calls == []


def test_attempt_preserves_original_error_and_marks_failed_cleanup_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("worker failed")
    messages: list[str] = []
    source_cleanup_calls = 0

    def fail_workers(*_args: Any, **_kwargs: Any) -> None:
        raise original

    def fail_source_cleanup(*_args: Any) -> None:
        nonlocal source_cleanup_calls
        source_cleanup_calls += 1
        message = "source cleanup failed"
        raise OSError(message)

    options, _state = _patch_attempt_shell(
        monkeypatch,
        run_workers=fail_workers,
        cleanup_stage=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("target cleanup failed")
        ),
        cleanup_source=fail_source_cleanup,
        messages=messages,
    )

    with pytest.raises(RuntimeError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert exc_info.value is original
    assert original.analytics_toolkit_sql_retry_safe is False
    assert source_cleanup_calls == 1
    assert any("empty attempt-owned source stage" in message for message in messages)
    assert any("Cleanup failed while handling" in message for message in messages)


def test_failed_empty_source_cleanup_includes_zero_keys_and_preserves_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 2))
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=2)
    stages = ["source.unpublished", "source.zero", "source.nonempty"]
    for stage in stages:
        assert runtime.live_stage_credits.acquire(blocking=False)
        runtime.reserve_source_stage(stage)
    zero = _task(options, expected_rows=0)
    zero.source_stage = stages[1]
    nonempty = _task(options, expected_rows=1)
    nonempty.source_stage = stages[2]
    runtime.publish_source_stage(zero)
    runtime.publish_source_stage(nonempty)
    manager = _Manager()
    calls: list[str] = []
    first = OSError("first empty cleanup failed")

    def cleanup(
        _options: TransferOptions,
        _source_ref: dict[str, Any],
        stage_tables: list[str],
    ) -> None:
        calls.extend(stage_tables)
        if stage_tables[0] == stages[0]:
            raise first

    monkeypatch.setattr(staged_keyed_io, "cleanup_source_stages", cleanup)

    with pytest.raises(OSError, match="first empty cleanup failed") as exc_info:
        staged_keyed_io.cleanup_failed_empty_source_stages(options, runtime, manager)

    assert exc_info.value is first
    assert manager.resumed == 1
    assert calls == stages[:2]
    assert stages[0] in runtime.source_stage_tables
    assert stages[1] not in runtime.source_stage_tables
    assert stages[2] in runtime.source_stage_tables


def test_keyed_io_cleanup_attempts_every_stage_and_preserves_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    calls: list[str] = []
    first = OSError("first cleanup failed")

    def cleanup(_backend: str, _connection: Any, stage: str, **_kwargs: Any) -> None:
        calls.append(stage)
        if stage == "stage.a":
            raise first

    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", cleanup)

    with pytest.raises(OSError, match="first cleanup failed") as exc_info:
        staged_keyed_io.cleanup_source_stages(
            options,
            {"connection": object()},
            ["stage.a", "stage.b"],
        )

    assert exc_info.value is first
    assert calls == ["stage.a", "stage.b"]

    calls.clear()
    monkeypatch.setattr(
        staged_keyed_io,
        "cleanup_stage_table",
        lambda _backend, _connection, stage, **_kwargs: calls.append(stage),
    )
    staged_keyed_io.cleanup_source_stages(
        options,
        {"connection": object()},
        ["stage.a", "stage.b"],
    )
    assert calls == ["stage.a", "stage.b"]


def test_keyed_io_drop_retry_replaces_failed_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=2)
    task = _task(options)
    attempts: list[Any] = []
    rollbacks: list[Any] = []
    replacements: list[tuple[str, Any]] = []
    first_connection = object()
    second_connection = object()
    source_ref: dict[str, Any] = {"connection": first_connection}

    def cleanup(_backend: str, connection: Any, _stage: str, **_kwargs: Any) -> None:
        attempts.append(connection)
        if connection is first_connection:
            message = "connection lost during drop"
            raise OSError(message)

    def replace(key: str, ref: dict[str, Any]) -> None:
        replacements.append((key, ref["connection"]))
        ref["connection"] = second_connection

    source_ref["bounded_replace_connection"] = replace
    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", cleanup)
    monkeypatch.setattr(staged_keyed_io, "rollback_quietly", rollbacks.append)

    staged_keyed_io.drop_source_stage(options, source_ref, task)

    assert attempts == [first_connection, second_connection]
    assert rollbacks == [first_connection]
    assert replacements == [("source", first_connection)]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_reader_materialization_failure_cleans_exact_reserved_stage_when_possible(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    options = _options(transfer_slices=[_options().transfer_slices[0]])
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    logs: list[str] = []
    cleaned: list[str] = []

    def cleanup(
        _options: TransferOptions,
        _source_ref: dict[str, Any],
        stages: list[str],
    ) -> None:
        cleaned.extend(stages)
        if cleanup_fails:
            message = "cleanup also failed"
            raise OSError(message)

    monkeypatch.setattr(staged_keyed_pipeline, "_drain_drop_ready", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda *_args: "source.reserved",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        lambda *_args: (_ for _ in ()).throw(OSError("CTAS failed")),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_source_stages", cleanup)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", logs.append)

    with pytest.raises(OSError, match="CTAS failed"):
        staged_keyed_pipeline._reader_worker(
            options,
            _metadata(),
            runtime,
            _Manager(),
            SimpleNamespace(start_key=lambda _key: None),
            threading.Lock(),
            0,
        )

    assert cleaned == ["source.reserved"]
    assert runtime.cancellation.is_set()
    assert ("source.reserved" in runtime.source_stage_tables) is cleanup_fails
    assert any("could not be removed" in message for message in logs) is cleanup_fails
