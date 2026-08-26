from __future__ import annotations

from tests.sql._support.transfer_keyed import (
    Any,
    LazyKeyedRuntime,
    SimpleNamespace,
    SourceColumn,
    TransferConnectionRefs,
    TransferStageState,
    VerifiedKey,
    _Manager,
    _metadata,
    _options,
    pytest,
    staged_attempt,
    staged_keyed_io,
    staged_keyed_pipeline,
    threading,
)


def test_cancelled_writer_exits_without_claiming_a_ready_key() -> None:
    options = _options()
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    runtime.cancellation.set()

    staged_keyed_pipeline._writer_worker(
        options,
        _metadata(),
        TransferStageState(target_exists=True),
        runtime,
        _Manager(),
        SimpleNamespace(),
        threading.Lock(),
        0,
        1,
    )

    assert runtime.ready.empty()


def test_keyed_io_consolidation_count_final_count_host_runner_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(collect_final_target_count=True)
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=2)
    runtime.mark_verified(VerifiedKey(0, 3, 3, "stage.primary"))
    runtime.mark_verified(VerifiedKey(1, 7, 7, "stage.secondary"))
    manager = _Manager()
    consolidated: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        staged_attempt,
        "_consolidate_worker_stages",
        lambda _options, _ref, _state, stages: consolidated.append(tuple(stages)),
    )

    copied = staged_keyed_io.consolidate_created_stages(
        options,
        manager,
        TransferStageState(target_exists=True),
        ["stage.primary", "stage.secondary"],
        runtime,
    )
    assert copied == 7
    assert consolidated == [("stage.primary", "stage.secondary")]

    monkeypatch.setattr(
        staged_keyed_io,
        "best_effort_transfer_target_count",
        lambda _options, target_connection_runner: target_connection_runner(
            "final count",
            lambda _ref: 11,
        ),
    )
    staged_keyed_io.capture_final_target_count(options, manager)
    assert options.final_target_rows == 11

    monkeypatch.setattr(
        staged_keyed_io,
        "get_ch_connection_for_host",
        lambda key, host: f"{key}:{host}",
    )
    host_runner = staged_keyed_io.make_target_host_connection_runner(options, manager)
    assert host_runner("host-a", lambda connection: f"used {connection}") == ("used target:host-a")

    replacements: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        staged_keyed_io,
        "replace_connection",
        lambda key, ref: replacements.append((key, ref)),
    )
    connection_ref: dict[str, Any] = {"connection": object()}
    staged_keyed_io._replace_managed_connection("source", connection_ref)
    assert replacements == [("source", connection_ref)]


def test_live_stage_credit_drains_acknowledgements_before_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    assert runtime.live_stage_credits.acquire(blocking=False)
    assert runtime.live_stage_credits.acquire(blocking=False)
    drains: list[int | None] = []

    def drain(*_args: Any, limit: int | None, **_kwargs: Any) -> int:
        drains.append(limit)
        runtime.live_stage_credits.release()
        return 1

    monkeypatch.setattr(staged_keyed_pipeline, "_drain_drop_ready", drain)

    staged_keyed_pipeline._acquire_live_stage_credit(_options(), runtime, _Manager())

    assert drains == [None]


def test_prepare_attempt_reads_existing_target_insert_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(replace_target_table=False, write_mode="append")
    state = TransferStageState(target_exists=True)
    refs = TransferConnectionRefs(
        source={"connection": object()},
        target={"connection": object()},
    )
    insert_contracts: list[dict[str, str]] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("id", "bigint")],
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "cleanup_superseded_transfer_stages", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(staged_keyed_pipeline, "ensure_transfer_target_table", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "get_existing_target_insert_types",
        lambda _backend, _connection, _table, source_types, **_kwargs: (
            insert_contracts.append(source_types) or {"id": "INTEGER"}
        ),
    )

    staged_keyed_pipeline._prepare_attempt(options, refs, state)

    assert insert_contracts == [{"id": "BIGINT"}]
    assert state.insert_column_types == {"id": "INTEGER"}
