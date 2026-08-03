from __future__ import annotations

# ruff: noqa: EM101, TRY003
import threading
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.transfer.flow import staged_attempt, staged_keyed_pipeline
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_internal_columns
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConcurrency,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)


def _options(**overrides: Any) -> TransferOptions:
    slices = [
        TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1"),
        TransferSlice(1, (2,), "", "SELECT 2 AS id", "key=2"),
    ]
    values: dict[str, Any] = {
        "from_db_key": "source",
        "from_db_backend": "gp",
        "to_db_key": "target",
        "to_db_backend": "gp",
        "source_sql": "SELECT id FROM source",
        "target_table": "public.target",
        "transfer_id": "a" * 32,
        "canonical_destination_identity": "public.target",
        "destination_hash": "0123456789abcdef",
        "source_transfer_staging_schema": "source_stage",
        "transfer_staging_schema": "target_stage",
        "transfer_slices": slices,
        "transfer_keys": ["key"],
        "batch_size": 2,
        "min_batch_size": 1,
        "max_batch_size": 4,
        "adaptive_batch_size": False,
        "retry_cnt": 1,
        "transfer_concurrency": TransferConcurrency(None, 2, 2, 2, 2, True),
    }
    values.update(overrides)
    return TransferOptions(**values)


def _state() -> TransferStageState:
    return TransferStageState(
        target_exists=True,
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )


def test_prepare_keyed_staged_attempt_resolves_schema_and_target(monkeypatch: Any) -> None:
    options = _options()
    refs = TransferConnectionRefs(
        source={"connection": object()},
        target={"connection": object()},
    )
    state = TransferStageState(target_exists=True)
    cleanups: list[str | None] = []
    targets: list[list[str]] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("id", "bigint")],
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_superseded_transfer_stages",
        lambda **kwargs: cleanups.append(kwargs["staging_schema"]),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_with_internal_column_types",
        lambda types, *_args: {**types, "internal": "TEXT"},
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "ensure_transfer_target_table",
        lambda _options, _refs, _state, columns: targets.append(columns),
    )

    staged_keyed_pipeline._prepare_attempt(options, refs, state)

    assert state.source_columns == ["id"]
    assert state.stage_column_types == {"id": "BIGINT", "internal": "TEXT"}
    assert cleanups == ["source_stage", "target_stage"]
    assert targets == [["id"]]

    monkeypatch.setattr(staged_keyed_pipeline, "inspect_source_query_schema", lambda *_args: [])
    with pytest.raises(ValueError, match="inspectable source schema"):
        staged_keyed_pipeline._prepare_attempt(options, refs, TransferStageState(True))


def test_prepare_keyed_staged_attempt_honors_explicit_schema(monkeypatch: Any) -> None:
    options = _options(table_schema={"id": "INTEGER"})
    state = TransferStageState(target_exists=True)
    refs = TransferConnectionRefs(source={"connection": object()}, target={"connection": object()})
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("id", "bigint")],
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "cleanup_superseded_transfer_stages", lambda **_: None
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_table_schema_columns",
        lambda schema, columns: {columns[0]: schema[columns[0]]},
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "_with_internal_column_types", lambda value, *_: value
    )
    monkeypatch.setattr(staged_keyed_pipeline, "ensure_transfer_target_table", lambda *_: None)

    staged_keyed_pipeline._prepare_attempt(options, refs, state)
    assert state.stage_column_types == {"id": "INTEGER"}


def test_source_stage_name_allocation_handles_collisions(monkeypatch: Any) -> None:
    options = _options()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    existence = iter([True, False])
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "table_exists",
        lambda *_args, **_kwargs: next(existence),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "collision_stage_suffix",
        lambda *_args: "collision",
    )

    assert (
        staged_keyed_pipeline._allocate_source_stage_name(options, {"connection": object()}, 1)
        == "collision"
    )

    monkeypatch.setattr(staged_keyed_pipeline, "table_exists", lambda *_args, **_: True)
    with pytest.raises(RuntimeError, match="unique source stage"):
        staged_keyed_pipeline._allocate_source_stage_name(
            options,
            {"connection": object()},
            0,
        )


def test_source_stage_worker_materializes_owned_keys(monkeypatch: Any) -> None:
    options = _options()
    state = _state()
    commands: list[str] = []
    post_commands: list[str] = []
    adapter = SimpleNamespace(execute_command=lambda _connection, sql: post_commands.append(sql))
    monkeypatch.setattr(staged_keyed_pipeline, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(staged_keyed_pipeline, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "execute_transfer_materialization",
        lambda _adapter, _backend, _connection, sql: commands.append(sql),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_count_source_slice",
        lambda _options, _connection, _table, slice_id, _state: slice_id + 2,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "replace_connection",
        lambda _key, ref: ref.update(connection=object()),
    )

    result = staged_keyed_pipeline._source_stage_worker(
        options,
        state,
        0,
        list(options.transfer_slices or []),
        "source_stage.table",
        threading.Event(),
    )

    assert result.slice_counts == {0: 2, 1: 3}
    assert commands[0].startswith("CREATE TABLE source_stage.table")
    assert commands[1].startswith("INSERT INTO source_stage.table")
    assert len(post_commands) == 2

    cancellation = threading.Event()
    cancellation.set()
    cancelled = staged_keyed_pipeline._source_stage_worker(
        options,
        state,
        0,
        list(options.transfer_slices or []),
        "unused",
        cancellation,
    )
    assert cancelled.slice_counts == {}


def test_source_worker_pool_orders_results_and_propagates_failure(monkeypatch: Any) -> None:
    options = _options()

    def worker(
        _options: Any,
        _state: Any,
        index: int,
        _slices: Any,
        table: str,
        _cancellation: Any,
    ) -> staged_keyed_pipeline.SourceStageResult:
        return staged_keyed_pipeline.SourceStageResult(index, table, {index: 1})

    monkeypatch.setattr(staged_keyed_pipeline, "_source_stage_worker", worker)
    results = staged_keyed_pipeline._run_source_stage_workers(
        options,
        _state(),
        [[options.transfer_slices[0]], [options.transfer_slices[1]]],  # type: ignore[index]
        ["stage_0", "stage_1"],
    )
    assert [result.worker_index for result in results] == [0, 1]

    def failing(*_args: Any) -> Any:
        raise OSError("reader failed")

    monkeypatch.setattr(staged_keyed_pipeline, "_source_stage_worker", failing)
    with pytest.raises(OSError, match="reader failed"):
        staged_keyed_pipeline._run_source_stage_workers(
            options,
            _state(),
            [[options.transfer_slices[0]]],  # type: ignore[index]
            ["stage_0"],
        )

    release = threading.Event()

    def concurrent_failure(
        _options: Any,
        _state: Any,
        index: int,
        _slices: Any,
        _table: str,
        _cancellation: Any,
    ) -> staged_keyed_pipeline.SourceStageResult:
        if index == 0:
            release.wait(timeout=1)
            return staged_keyed_pipeline.SourceStageResult(0, "stage_0", {0: 1})
        threading.Timer(0.05, release.set).start()
        raise OSError("concurrent reader failed")

    monkeypatch.setattr(staged_keyed_pipeline, "_source_stage_worker", concurrent_failure)
    with pytest.raises(OSError, match="concurrent reader failed"):
        staged_keyed_pipeline._run_source_stage_workers(
            options,
            _state(),
            [[options.transfer_slices[0]], [options.transfer_slices[1]]],  # type: ignore[index]
            ["stage_0", "stage_1"],
        )


def test_source_slice_count_and_key_set_validation(monkeypatch: Any) -> None:
    options = _options()
    state = _state()
    adapter = SimpleNamespace(quote_identifier=lambda value: f'"{value}"')
    monkeypatch.setattr(staged_keyed_pipeline, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(columns=([7],)),
    )
    assert (
        staged_keyed_pipeline._count_source_slice(options, object(), "source.stage", 0, state) == 7
    )

    state.internal_columns = None
    with pytest.raises(RuntimeError, match="internal columns"):
        staged_keyed_pipeline._count_source_slice(options, object(), "stage", 0, state)

    staged_keyed_pipeline._validate_source_stage_counts(options, {0: 1, 1: 0})
    with pytest.raises(RuntimeError, match="exactly one result"):
        staged_keyed_pipeline._validate_source_stage_counts(options, {0: 1})


def test_target_stage_creation_records_partial_progress(monkeypatch: Any) -> None:
    options = _options()
    state = _state()
    calls = 0

    def create(*_args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second stage failed")
        return f"target_{kwargs['random_suffix']}"

    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_table", create)
    with pytest.raises(OSError, match="second stage failed"):
        staged_keyed_pipeline._create_target_worker_stages(
            options,
            {"connection": object()},
            state,
            worker_count=2,
        )
    assert state.stage_table_created is True
    assert len(state.stage_tables or []) == 1

    calls = 0
    tables = staged_keyed_pipeline._create_target_worker_stages(
        options,
        {"connection": object()},
        state,
        worker_count=1,
    )
    assert len(tables) == 1


def test_writer_pool_propagates_worker_failure(monkeypatch: Any) -> None:
    options = _options()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_whole_key_writer",
        lambda *_args: (_ for _ in ()).throw(OSError("writer failed")),
    )
    with pytest.raises(OSError, match="writer failed"):
        staged_keyed_pipeline._run_whole_key_writers(
            options,
            _state(),
            ["target_0"],
            deque(),
            insert_retry_cnt=1,
        )

    release = threading.Event()

    def concurrent_failure(*args: Any) -> None:
        worker_index = int(args[2])
        if worker_index == 0:
            release.wait(timeout=1)
            return
        threading.Timer(0.05, release.set).start()
        raise OSError("concurrent writer failed")

    monkeypatch.setattr(staged_keyed_pipeline, "_whole_key_writer", concurrent_failure)
    with pytest.raises(OSError, match="concurrent writer failed"):
        staged_keyed_pipeline._run_whole_key_writers(
            options,
            _state(),
            ["target_0", "target_1"],
            deque(),
            insert_retry_cnt=1,
        )


def test_whole_key_writer_rejects_short_and_duplicate_batches(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=TransferConcurrency(None, 1, 1, 1, 1, True))
    task = staged_keyed_pipeline.WholeKeyTask(options.transfer_slices[0], "source", 1)  # type: ignore[index]
    monkeypatch.setattr(staged_keyed_pipeline, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(staged_keyed_pipeline, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_read_key_batch",
        lambda *_args: RowBatch(["id"], []),
    )
    with pytest.raises(RuntimeError, match="returned 0 row"):
        staged_keyed_pipeline._whole_key_writer(
            options,
            _state(),
            0,
            "target",
            deque([task]),
            threading.Lock(),
            {},
            threading.Lock(),
            threading.Event(),
            1,
        )

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_read_key_batch",
        lambda *_args: RowBatch(["id"], [(1,)]),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "insert_rows_batch", lambda *_args, **_: None)
    with pytest.raises(RuntimeError, match="staged twice"):
        staged_keyed_pipeline._whole_key_writer(
            options,
            _state(),
            0,
            "target",
            deque([task]),
            threading.Lock(),
            {0: 1},
            threading.Lock(),
            threading.Event(),
            1,
        )

    cancellation = threading.Event()
    cancellation.set()
    staged_keyed_pipeline._whole_key_writer(
        options,
        _state(),
        0,
        "target",
        deque([task]),
        threading.Lock(),
        {},
        threading.Lock(),
        cancellation,
        1,
    )


def test_staged_key_batch_replaces_failed_source_connection(monkeypatch: Any) -> None:
    options = _options()
    task = staged_keyed_pipeline.WholeKeyTask(options.transfer_slices[0], "source", 1)  # type: ignore[index]
    source_ref = {"connection": object()}
    rollbacks: list[Any] = []
    replacements: list[str] = []
    monkeypatch.setattr(staged_keyed_pipeline, "rollback_quietly", rollbacks.append)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "replace_connection",
        lambda key, _ref: replacements.append(key),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "run_with_retry",
        lambda **kwargs: kwargs["operation"](1),
    )
    monkeypatch.setattr(
        staged_attempt,
        "_read_snapshot_range",
        lambda *_args: RowBatch(["id"], [(1,)]),
    )
    assert (
        staged_keyed_pipeline._read_key_batch(
            options,
            source_ref,
            task,
            _state(),
            1,
            2,
        ).row_count
        == 1
    )

    monkeypatch.setattr(
        staged_attempt,
        "_read_snapshot_range",
        lambda *_args: (_ for _ in ()).throw(OSError("read failed")),
    )
    with pytest.raises(OSError, match="read failed"):
        staged_keyed_pipeline._read_key_batch(
            options,
            source_ref,
            task,
            _state(),
            1,
            2,
        )
    assert len(rollbacks) == 1
    assert replacements == ["source"]


def test_target_validation_checks_counts_identity_and_internal_columns(monkeypatch: Any) -> None:
    options = _options()
    state = _state()
    validated: list[dict[int, int]] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_transfer_stage_identity",
        lambda **kwargs: validated.append(kwargs["expected_slice_counts"]),
    )
    staged_keyed_pipeline._validate_target_stages(
        options,
        {"connection": object()},
        state,
        ["target"],
        {0: 1},
        {0: 1},
    )
    assert validated == [{0: 1}]

    with pytest.raises(RuntimeError, match="row-count mismatch"):
        staged_keyed_pipeline._validate_target_stages(
            options,
            {"connection": object()},
            state,
            ["target"],
            {0: 1},
            {0: 0},
        )
    state.internal_columns = None
    with pytest.raises(RuntimeError, match="internal columns"):
        staged_keyed_pipeline._validate_target_stages(
            options,
            {"connection": object()},
            state,
            ["target"],
            {0: 1},
            {0: 1},
        )


def test_source_cleanup_attempts_every_table_and_preserves_first_error(monkeypatch: Any) -> None:
    dropped: list[str] = []

    def cleanup(_backend: str, _connection: Any, table: str, **_kwargs: Any) -> None:
        dropped.append(table)
        if table == "first":
            raise OSError("first cleanup")
        if table == "second":
            raise RuntimeError("second cleanup")

    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage_table", cleanup)
    with pytest.raises(OSError, match="first cleanup"):
        staged_keyed_pipeline._cleanup_source_stages(
            _options(),
            {"connection": object()},
            ["first", "second", "third"],
        )
    assert dropped == ["first", "second", "third"]

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_stage_table",
        lambda _backend, _connection, table, **_kwargs: dropped.append(table),
    )
    staged_keyed_pipeline._cleanup_source_stages(
        _options(),
        {"connection": object()},
        ["success"],
    )
    assert dropped[-1] == "success"


def test_keyed_staged_attempt_guards_and_cleanup_precedence(monkeypatch: Any) -> None:
    with pytest.raises(ValueError, match="requires transfer slices"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            _options(transfer_slices=[]),
            insert_retry_cnt=1,
        )
    with pytest.raises(RuntimeError, match="runtime identity"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            _options(transfer_id=None),
            insert_retry_cnt=1,
        )

    delegated: list[int] = []
    monkeypatch.setattr(
        staged_attempt,
        "run_keyed_staged_source_transfer_attempt",
        lambda _options, *, insert_retry_cnt: delegated.append(insert_retry_cnt) or 7,
    )
    assert (
        staged_attempt.run_staged_source_transfer_attempt(
            _options(),
            insert_retry_cnt=3,
        )
        == 7
    )
    assert delegated == [3]

    monkeypatch.setattr(staged_keyed_pipeline, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_stage_state",
        lambda *_args: TransferStageState(target_exists=True),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_prepare_attempt",
        lambda *_args: (_ for _ in ()).throw(ValueError("primary")),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secondary")),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "replace_connection",
        lambda _key, ref: ref.update(connection=object()),
    )
    with pytest.raises(ValueError, match="primary"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            _options(),
            insert_retry_cnt=1,
        )

    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_allocate_source_stage_name",
        lambda _options, _ref, worker: f"source_{worker}",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_run_source_stage_workers",
        lambda *_args: [
            staged_keyed_pipeline.SourceStageResult(0, "source_0", {0: 1}),
            staged_keyed_pipeline.SourceStageResult(1, "source_1", {1: 1}),
        ],
    )
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_source_stage_counts", lambda *_: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_create_target_worker_stages",
        lambda *_args, **_kwargs: ["target_0", "target_1"],
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_run_whole_key_writers",
        lambda *_args, **_kwargs: {0: 1, 1: 1},
    )
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", lambda *_: None)
    monkeypatch.setattr(staged_attempt, "_consolidate_worker_stages", lambda *_: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_loaded_stage_row_count",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "finalize_loaded_stage", lambda *_: None)
    with pytest.raises(OSError, match="secondary"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            _options(),
            insert_retry_cnt=1,
        )


def test_logged_phase_does_not_log_completion_after_failure(monkeypatch: Any) -> None:
    messages: list[str] = []
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", messages.append)
    staged_keyed_pipeline._run_logged_phase("work", lambda: None)
    assert messages == [
        "Starting keyed transfer pipeline work",
        "Completed keyed transfer pipeline work",
    ]

    messages.clear()
    with pytest.raises(OSError, match="failed"):
        staged_keyed_pipeline._run_logged_phase(
            "work",
            lambda: (_ for _ in ()).throw(OSError("failed")),
        )
    assert messages == ["Starting keyed transfer pipeline work"]
