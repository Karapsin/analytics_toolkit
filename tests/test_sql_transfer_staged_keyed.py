from __future__ import annotations

# ruff: noqa: EM101, TRY003
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pandas as pd
import pytest
from analytics_toolkit.general import time_print
from analytics_toolkit.sql._log_context import sql_log_context
from analytics_toolkit.sql.backends import common_methods
from analytics_toolkit.sql.backends.ch import lifecycle as ch_lifecycle
from analytics_toolkit.sql.backends.ch.ddl import build_ch_shard_table_name
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.load import stage as load_stage
from analytics_toolkit.sql.dml.transfer.flow import (
    api as transfer_api,
)
from analytics_toolkit.sql.dml.transfer.flow import (
    dry_run,
    finalize,
    row_counts,
    staged_attempt,
    staged_keyed_io,
    staged_keyed_logging,
    staged_keyed_pipeline,
)
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    DropReady,
    KeyReadComplete,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    VerifiedKey,
    freeze_attempt_metadata,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_internal_columns
from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import TransferProgressTracker
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import (
    BoundedConnectionCloseError,
    BoundedConnectionManager,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConcurrency,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)
from analytics_toolkit.sql.execution.operation_runner import tracked_sql_operation
from analytics_toolkit.sql.execution.plans import SqlPlan


def _concurrency(read: int = 2, write: int = 2) -> TransferConcurrency:
    return TransferConcurrency(
        legacy_value=None,
        requested_read=read,
        requested_write=write,
        effective_read=read,
        effective_write=write,
        split_requested=True,
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
        "transfer_concurrency": _concurrency(),
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


def _metadata() -> Any:
    state = _state()
    assert state.internal_columns is not None
    return freeze_attempt_metadata(
        source_columns=state.source_columns or [],
        source_column_types=state.source_column_types or {},
        stage_column_types=state.stage_column_types,
        internal_columns=state.internal_columns,
    )


def _ready_task(
    transfer_slice: TransferSlice,
    source_stage: str,
    expected_rows: int,
) -> ReadyKeyTask:
    slice_values = transfer_slice.values  # noqa: PD011
    return ReadyKeyTask(
        transfer_slice=transfer_slice,
        source_stage=source_stage,
        expected_rows=expected_rows,
        tag=(f"[slice={transfer_slice.index + 1}/2 key=key:{slice_values[0]!r}]"),
        materialized_at=0.0,
    )


class _LeaseManager:
    def __init__(self) -> None:
        self.active = 0
        self.high_water_mark = 0
        self.lease_count = 0
        self.released = threading.Event()

    def interrupt_active(self) -> None:
        return

    def resume_for_cleanup(self) -> None:
        return

    def run(self, _role: str, operation: Callable[[dict[str, Any]], Any]) -> Any:
        with self.lease() as connection_ref:
            return operation(connection_ref)

    def close(self) -> None:
        return

    def close_preserving(self, error: BaseException | None) -> None:
        try:
            self.close()
        except BaseException:
            if error is None:
                raise
            error.__dict__["analytics_toolkit_sql_retry_safe"] = False

    @contextmanager
    def lease(
        self,
        *,
        cancellation: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        if cancellation is not None and cancellation.is_set():
            raise RuntimeError("lease cancelled")
        self.active += 1
        self.lease_count += 1
        self.high_water_mark = max(self.high_water_mark, self.active)
        try:
            yield {"connection": object()}
        finally:
            self.active -= 1
            self.released.set()


class _ProgressBar:
    def update(self, _rows: int) -> None:
        return

    def close(self) -> None:
        return


def test_failed_attempt_cleanup_drops_published_zero_row_source_stages(
    monkeypatch: Any,
) -> None:
    options = _options()
    transfer_slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(transfer_slices, read_workers=2, write_workers=2)
    zero_task = _ready_task(transfer_slices[0], "source.zero", 0)
    nonempty_task = _ready_task(transfer_slices[1], "source.nonempty", 1)
    for stage_table in ("source.reserved", zero_task.source_stage, nonempty_task.source_stage):
        assert runtime.live_stage_credits.acquire(blocking=False)
        runtime.reserve_source_stage(stage_table)
    runtime.publish_source_stage(zero_task)
    runtime.publish_source_stage(nonempty_task)
    dropped: list[str] = []
    monkeypatch.setattr(
        staged_keyed_io,
        "cleanup_source_stages",
        lambda _options, _source_ref, stage_tables: dropped.extend(stage_tables),
    )

    staged_keyed_io.cleanup_failed_empty_source_stages(
        options,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
    )

    assert dropped == ["source.reserved", "source.zero"]
    assert runtime.source_stage_tables == ["source.nonempty"]
    assert runtime.source_stages_dropped == 2


def _thread(operation: Callable[[], None]) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            operation()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


def test_keyed_sql_log_context_prefixes_messages_and_suppresses_raw_sql(
    monkeypatch: Any,
) -> None:
    logs: list[str] = []
    secret_sql = "SELECT password FROM customer_secret"

    def fail(*_args: Any) -> None:
        raise RuntimeError("driver included row contents")

    adapter = SimpleNamespace(
        backend="gp",
        _read_columns_impl=fail,
    )
    monkeypatch.setattr(
        "analytics_toolkit.general.time_print",
        lambda message, **_kwargs: logs.append(message),
    )

    with pytest.raises(RuntimeError), sql_log_context(
        "[slice=1/1] ",
        suppress_sql=True,
    ):
        common_methods.read_columns(
            adapter,
            object(),
            secret_sql,
            print_queries=False,
            print_query=lambda *_args: None,
            read_dbapi_columns=object(),
        )

    assert logs
    assert all(message.startswith("[slice=1/1] ") for message in logs)
    assert logs[-1] == "[slice=1/1] Failed SQL (details suppressed)"
    assert secret_sql not in "\n".join(logs)


def test_keyed_sql_log_context_prefixes_nested_logs_and_hides_tracked_preview(
    capsys: Any,
) -> None:
    tag = "[slice=1/1]"
    secret_sql = "SELECT credential_secret FROM private_rows"

    with sql_log_context(f"{tag} ", suppress_sql=True):
        time_print("Nested stage message")
        with tracked_sql_operation(
            operation_name="keyed_stage",
            alias="target",
            backend="gp",
            phase="create_stage",
            preview_sql=secret_sql,
        ):
            pass

    output = capsys.readouterr().out
    relevant_lines = [
        line for line in output.splitlines() if "Nested stage message" in line or "SQL" in line
    ]
    assert relevant_lines
    assert all(tag in line for line in relevant_lines)
    assert secret_sql not in output
    assert "Finished SQL statement" not in output


def test_keyed_io_opts_all_batch_retries_into_safe_exception_logging(
    monkeypatch: Any,
) -> None:
    options = _options(retry_cnt=1)
    task = _ready_task(options.transfer_slices[0], "source.stage", 1)
    metadata = _metadata()
    retry_calls: list[dict[str, Any]] = []

    def retry_once(**kwargs: Any) -> Any:
        retry_calls.append(kwargs)
        return kwargs["operation"](1)

    monkeypatch.setattr(staged_keyed_io, "run_with_retry", retry_once)
    monkeypatch.setattr(
        staged_keyed_io,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(
            column_names=["id"],
            columns=[[1]],
        ),
    )
    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", lambda *_a, **_k: None)

    def insert_once(*_args: Any, **kwargs: Any) -> int:
        assert kwargs["safe_exception_logging"] is True
        assert kwargs["log_prefix"] == f"{task.tag} "
        return kwargs["retry_fn"](
            operation_name="keyed insert",
            retry_cnt=1,
            timeout_increment=0,
            operation=lambda _attempt: 1,
        )

    monkeypatch.setattr(staged_keyed_io, "insert_rows_batch", insert_once)

    staged_keyed_io.read_key_batch(
        options,
        {"connection": object()},
        task,
        metadata,
        1,
        2,
    )
    staged_keyed_io.insert_target_batch(
        options,
        {"connection": object()},
        "target.stage",
        QueuedKeyBatch(
            task=task,
            batch_index=1,
            start_ordinal=1,
            stop_ordinal=1,
            batch=RowBatch(columns=["id"], rows=[(1,)]),
            read_started_at=0.0,
            read_completed_at=0.1,
            approximate_memory_bytes=8,
        ),
        metadata,
        insert_retry_cnt=1,
    )
    staged_keyed_io.drop_source_stage(options, {"connection": object()}, task)

    assert len(retry_calls) == 3
    assert all(call["safe_exception_logging"] is True for call in retry_calls)
    assert all(call["log_prefix"] == f"{task.tag} " for call in retry_calls)
    assert all(callable(call["retry_status"]) for call in retry_calls)


def test_staged_key_batch_replaces_failed_source_connection(monkeypatch: Any) -> None:
    options = _options(retry_cnt=2, timeout_increment=0)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    opened: list[Any] = []
    reads = 0

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        return connection

    def read(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("connection lost")
        return SimpleNamespace(column_names=["id"], columns=[[1]])

    monkeypatch.setattr(staged_keyed_io, "_read_backend", read)
    manager = BoundedConnectionManager(
        "source",
        1,
        role="source batch retry pool",
        open_connection=open_connection,
    )
    with manager.lease() as source_ref:
        batch = staged_keyed_io.read_key_batch(
            options,
            source_ref,
            task,
            _metadata(),
            1,
            2,
            batch_index=1,
        )

    assert batch.rows == [(1,)]
    assert reads == 2
    assert len(opened) == 2
    assert opened[0].close_count == 1
    assert manager.high_water_mark == 1
    manager.close()
    assert opened[1].close_count == 1


def test_staged_key_batch_enforces_sql_and_rowbatch_row_limit(monkeypatch: Any) -> None:
    options = _options(retry_cnt=1, timeout_increment=0)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 3)
    queries: list[str] = []

    def over_return(_backend: str, _connection: Any, sql: str, **_kwargs: Any) -> Any:
        queries.append(sql)
        return SimpleNamespace(column_names=["id"], columns=[[1, 2, 3]])

    monkeypatch.setattr(staged_keyed_io, "_read_backend", over_return)

    with pytest.raises(RuntimeError, match="scheduled limit is 2"):
        staged_keyed_io.read_key_batch(
            options,
            {"connection": object()},
            task,
            _metadata(),
            1,
            3,
            batch_index=1,
        )

    assert len(queries) == 1
    assert queries[0].endswith('ORDER BY "__analytics_toolkit_row_ordinal" LIMIT 2')


def test_staged_key_batch_recovers_after_transient_replacement_open_failure(
    monkeypatch: Any,
) -> None:
    options = _options(retry_cnt=2, timeout_increment=0)
    task = _ready_task((options.transfer_slices or [])[0], "source.stage", 1)
    opened: list[Any] = []
    open_attempts = 0
    reads = 0

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    def open_connection(_key: str) -> Connection:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 2:
            raise OSError("temporary connection open failure")
        connection = Connection()
        opened.append(connection)
        return connection

    def read(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("connection lost")
        return SimpleNamespace(column_names=["id"], columns=[[1]])

    monkeypatch.setattr(staged_keyed_io, "_read_backend", read)
    manager = BoundedConnectionManager(
        "source",
        1,
        role="source replacement-open retry pool",
        open_connection=open_connection,
    )
    with manager.lease() as source_ref:
        batch = staged_keyed_io.read_key_batch(
            options,
            source_ref,
            task,
            _metadata(),
            1,
            2,
            batch_index=1,
        )

    assert batch.rows == [(1,)]
    assert open_attempts == 3
    assert reads == 2
    assert manager.high_water_mark == 1
    manager.close()
    assert [connection.close_count for connection in opened] == [1, 1]


def test_prepare_keyed_attempt_caches_one_immutable_schema_contract(monkeypatch: Any) -> None:
    options = _options()
    refs = TransferConnectionRefs(
        source={"connection": object()},
        target={"connection": object()},
    )
    state = TransferStageState(target_exists=True)
    inspections: list[str] = []
    cleanups: list[str | None] = []
    targets: list[list[str]] = []

    def inspect(_backend: str, _connection: Any, sql: str) -> list[SourceColumn]:
        inspections.append(sql)
        return [SourceColumn("id", "bigint")]

    monkeypatch.setattr(staged_keyed_pipeline, "inspect_source_query_schema", inspect)
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

    metadata = staged_keyed_pipeline._prepare_attempt(options, refs, state)

    assert inspections == ["SELECT 1 AS id"]
    assert metadata.source_columns == ("id",)
    assert dict(metadata.source_column_types) == {"id": "bigint"}
    assert dict(metadata.stage_column_types or {}) == {"id": "BIGINT", "internal": "TEXT"}
    assert state.source_columns == ["id"]
    assert cleanups == ["source_stage", "target_stage"]
    assert targets == [["id"]]
    with pytest.raises(TypeError):
        metadata.source_column_types["id"] = "changed"  # type: ignore[index]

    monkeypatch.setattr(staged_keyed_pipeline, "inspect_source_query_schema", lambda *_: [])
    with pytest.raises(ValueError, match="inspectable source schema"):
        staged_keyed_pipeline._prepare_attempt(options, refs, TransferStageState(True))


def test_lazy_target_stage_uses_cached_staging_ddl_contract(monkeypatch: Any) -> None:
    policy = SimpleNamespace(create_distributed_pair=True)
    options = _options(
        staging_ddl_properties={"fillfactor": "80"},
        staging_ch_policy=policy,
    )
    captured: list[dict[str, Any]] = []

    def create(*_args: Any, **kwargs: Any) -> str:
        captured.append(kwargs)
        return "target_stage.writer_0"

    monkeypatch.setattr(staged_keyed_io, "create_stage_table", create)
    assert (
        staged_keyed_io.create_target_writer_stage(
            options,
            {"connection": object()},
            _metadata(),
            0,
        )
        == "target_stage.writer_0"
    )
    assert captured[0]["ddl_properties"] is options.staging_ddl_properties
    assert captured[0]["ch_creation_policy"] is policy


def test_prepare_keyed_attempt_honors_explicit_schema(monkeypatch: Any) -> None:
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

    metadata = staged_keyed_pipeline._prepare_attempt(options, refs, state)

    assert dict(metadata.stage_column_types or {}) == {"id": "INTEGER"}


def test_materialize_source_key_runs_one_ctas_per_key_without_append(monkeypatch: Any) -> None:
    options = _options()
    metadata = _metadata()
    events: list[tuple[str, str]] = []
    counts = {0: 3, 1: 5}
    adapter = SimpleNamespace(
        execute_command=lambda _connection, sql: events.append(("post", sql)),
    )
    monkeypatch.setattr(staged_keyed_io, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_keyed_io,
        "build_snapshot_select_sql",
        lambda **kwargs: f"SELECT slice_{kwargs['slice_id']}",
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "build_source_snapshot_sql",
        lambda **kwargs: SimpleNamespace(
            create_sql=(
                f"CREATE TABLE {kwargs['snapshot_table']} AS {kwargs['snapshot_select_sql']}"
            ),
            post_create_sqls=(f"POST CREATE {kwargs['snapshot_table']}",),
        ),
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "execute_transfer_materialization",
        lambda _adapter, _backend, _connection, sql: events.append(("ctas", sql)),
    )

    def count(
        _options: Any,
        _connection: Any,
        table: str,
        slice_index: int,
        _metadata: Any,
    ) -> int:
        events.append(("count", table))
        return counts[slice_index]

    monkeypatch.setattr(staged_keyed_io, "count_source_slice", count)
    slices = options.transfer_slices or []

    results = [
        staged_keyed_io.materialize_source_key(
            options,
            {"connection": object()},
            metadata,
            transfer_slice,
            f"source_stage.key_{transfer_slice.index}",
        )
        for transfer_slice in slices
    ]

    assert results == [3, 5]
    ctas = [sql for kind, sql in events if kind == "ctas"]
    assert ctas == [
        "CREATE TABLE source_stage.key_0 AS SELECT slice_0",
        "CREATE TABLE source_stage.key_1 AS SELECT slice_1",
    ]
    assert all("INSERT INTO" not in sql for sql in ctas)
    assert [kind for kind, _value in events] == [
        "ctas",
        "post",
        "count",
        "ctas",
        "post",
        "count",
    ]


def test_source_stage_name_allocation_handles_collisions(monkeypatch: Any) -> None:
    options = _options()
    monkeypatch.setattr(
        staged_keyed_io,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    existence = iter([True, False])
    monkeypatch.setattr(
        staged_keyed_io,
        "table_exists",
        lambda *_args, **_kwargs: next(existence),
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "collision_stage_suffix",
        lambda *_args: "collision",
    )

    assert (
        staged_keyed_io.allocate_source_stage_name(options, {"connection": object()}, 1)
        == "collision"
    )

    monkeypatch.setattr(staged_keyed_io, "table_exists", lambda *_args, **_: True)
    with pytest.raises(RuntimeError, match="unique source stage"):
        staged_keyed_io.allocate_source_stage_name(options, {"connection": object()}, 0)


def test_target_stage_candidates_include_ambiguous_create_outcomes(
    monkeypatch: Any,
) -> None:
    candidates: list[str] = []
    logs: list[str] = []
    existence = iter([False, True, False])
    creates = iter([OSError("ambiguous create"), None])
    monkeypatch.setattr(
        load_stage,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    monkeypatch.setattr(
        load_stage,
        "table_exists",
        lambda *_args, **_kwargs: next(existence),
    )
    monkeypatch.setattr(load_stage, "_collision_stage_suffix", lambda *_args, **_kwargs: "next")

    def create(*_args: Any, **_kwargs: Any) -> None:
        outcome = next(creates)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(load_stage, "_create_sql_table_with_connection", create)
    monkeypatch.setattr(
        load_stage,
        "time_print",
        lambda message, **_kwargs: logs.append(message),
    )

    result = load_stage.create_stage_table(
        "gp",
        object(),
        "public.target",
        pd.DataFrame(columns=["id"]),
        random_suffix="first",
        on_stage_candidate=candidates.append,
        log_prefix="[slice=1/1] ",
    )

    assert result == "next"
    assert candidates == ["first", "next"]
    assert logs
    assert all(message.startswith("[slice=1/1] ") for message in logs)


def test_target_stage_candidate_precedes_partial_clickhouse_create(
    monkeypatch: Any,
) -> None:
    candidates: list[str] = []
    monkeypatch.setattr(
        load_stage,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    monkeypatch.setattr(load_stage, "table_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        load_stage,
        "_create_sql_table_with_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("distributed create failed")),
    )

    with pytest.raises(OSError, match="distributed create failed"):
        load_stage.create_stage_table(
            "ch",
            object(),
            "default.target",
            pd.DataFrame(columns=["id"]),
            random_suffix="partial",
            on_stage_candidate=candidates.append,
            ch_creation_policy=object(),
        )

    assert candidates == ["partial"]


def test_target_stage_validation_checks_payload_counts_without_internal_columns(
    monkeypatch: Any,
) -> None:
    options = _options()
    state = _state()
    validated: list[dict[int, int]] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_transfer_stage_identity",
        lambda **kwargs: validated.append(dict(kwargs["expected_slice_counts"])),
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
    with pytest.raises(RuntimeError, match="no target stage"):
        staged_keyed_pipeline._validate_target_stages(
            options,
            {"connection": object()},
            state,
            [],
            {0: 1},
            {0: 1},
        )
    staged_keyed_pipeline._validate_target_stages(
        options,
        {"connection": object()},
        state,
        [],
        {0: 0},
        {0: 0},
    )
    state.internal_columns = None
    staged_keyed_pipeline._validate_target_stages(
        options,
        {"connection": object()},
        state,
        ["target"],
        {0: 1},
        {0: 1},
    )
    assert validated == [{0: 1}, {0: 1}]


def test_writer_creates_target_stage_on_first_nonempty_key_and_reuses_it(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    tasks = [
        _ready_task(slices[0], "source.empty", 0),
        _ready_task(slices[1], "source.nonempty", 2),
        _ready_task(slices[0], "source.later", 1),
    ]
    target_connections = _LeaseManager()
    progress = TransferProgressTracker(total_key_count=3, active_writers=1)
    events: list[tuple[str, int, str | None]] = []

    def create_stage(
        _options: Any,
        _target_ref: Any,
        _metadata: Any,
        writer_index: int,
        **_kwargs: Any,
    ) -> str:
        events.append(("create", writer_index, None))
        return "target_stage.writer_0"

    def consume(*args: Any, **_kwargs: Any) -> None:
        task = args[9]
        events.append(("consume", task.transfer_slice.index, args[8]))

    monkeypatch.setattr(staged_keyed_pipeline, "create_target_writer_stage", create_stage)
    monkeypatch.setattr(staged_keyed_pipeline, "_consume_key", consume)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    runtime.ready.put(tasks[0])
    worker, errors = _thread(
        lambda: staged_keyed_pipeline._writer_worker(
            options,
            _metadata(),
            _state(),
            runtime,
            target_connections,  # type: ignore[arg-type]
            progress,
            threading.Lock(),
            0,
            1,
        )
    )
    runtime.ready.put(tasks[1], timeout=1)
    runtime.ready.put(tasks[2], timeout=1)
    runtime.ready.put(None, timeout=1)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert events == [
        ("consume", 0, None),
        ("create", 0, None),
        ("consume", 1, "target_stage.writer_0"),
        ("consume", 0, "target_stage.writer_0"),
    ]
    assert runtime.target_stages == {0: "target_stage.writer_0"}
    assert target_connections.lease_count == 1


def test_zero_only_writer_never_creates_target_stage(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    progress = TransferProgressTracker(total_key_count=2, active_writers=1)
    consumed: list[int] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda *_args: pytest.fail("zero-only writer created a target stage"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_consume_key",
        lambda *args, **_kwargs: consumed.append(args[9].transfer_slice.index),
    )

    runtime.ready.put(_ready_task(slices[0], "source.zero_0", 0))
    worker, errors = _thread(
        lambda: staged_keyed_pipeline._writer_worker(
            options,
            _metadata(),
            _state(),
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            progress,
            threading.Lock(),
            0,
            1,
        )
    )
    runtime.ready.put(_ready_task(slices[1], "source.zero_1", 0), timeout=1)
    runtime.ready.put(None, timeout=1)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert consumed == [0, 1]
    assert runtime.target_stages == {}


def test_runtime_bounds_capacity_one_prefetch_and_live_source_stages() -> None:
    slices = [
        TransferSlice(index, (index,), "", f"SELECT {index}", f"key={index}") for index in range(6)
    ]
    runtime = LazyKeyedRuntime(slices, read_workers=2, write_workers=3)

    assert runtime.ready.maxsize == 3
    assert [batch_queue.maxsize for batch_queue in runtime.writer_queues] == [1, 1, 1]
    assert runtime.live_stage_limit == 5
    assert [runtime.live_stage_credits.acquire(blocking=False) for _ in range(5)] == [
        True,
        True,
        True,
        True,
        True,
    ]
    assert runtime.live_stage_credits.acquire(blocking=False) is False

    task = _ready_task(slices[0], "source.exact", 0)
    runtime.reserve_source_stage(task.source_stage)
    runtime.publish_source_stage(task)
    runtime.mark_source_stage_dropped(task.source_stage)

    assert runtime.live_stage_credits.acquire(blocking=False) is True
    assert runtime.live_source_stage_count == 0


def test_concurrent_writers_keep_whole_key_ownership(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 2))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=2)
    tasks = [
        _ready_task(slices[0], "source.key_0", 0),
        _ready_task(slices[1], "source.key_1", 0),
    ]
    progress = TransferProgressTracker(total_key_count=2, active_writers=2)
    barrier = threading.Barrier(2)
    ownership: list[tuple[int, int]] = []
    ownership_lock = threading.Lock()

    def consume(*args: Any, **_kwargs: Any) -> None:
        writer_index = int(args[7])
        task = args[9]
        with ownership_lock:
            ownership.append((task.transfer_slice.index, writer_index))
        barrier.wait(timeout=1)

    monkeypatch.setattr(staged_keyed_pipeline, "_consume_key", consume)
    runtime.ready.put(tasks[0])
    runtime.ready.put(tasks[1])

    def writer_operation(writer_index: int) -> Callable[[], None]:
        def run() -> None:
            staged_keyed_pipeline._writer_worker(
                options,
                _metadata(),
                _state(),
                runtime,
                _LeaseManager(),  # type: ignore[arg-type]
                progress,
                threading.Lock(),
                writer_index,
                1,
            )

        return run

    workers = [_thread(writer_operation(writer_index)) for writer_index in range(2)]
    assert all(task.assignment.wait(timeout=1) for task in tasks)
    runtime.ready.put(None, timeout=1)
    runtime.ready.put(None, timeout=1)
    for worker, _errors in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker, _errors in workers)
    assert all(errors == [] for _worker, errors in workers)
    assert {slice_index for slice_index, _writer in ownership} == {0, 1}
    assert len(ownership) == 2
    assert {writer for _slice_index, writer in ownership} == {0, 1}
    for task in tasks:
        assert task.writer_index is not None
        assert task.batch_queue is runtime.writer_queues[task.writer_index]


def test_concurrent_keyed_batch_logs_follow_monotonic_commit_order(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 2))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=2)
    progress = TransferProgressTracker(total_key_count=2, active_writers=2)
    state = _state()
    state_lock = threading.Lock()
    tasks: list[ReadyKeyTask] = []
    now = time.monotonic()
    for writer_index, transfer_slice in enumerate(slices):
        task = _ready_task(transfer_slice, f"source.key_{writer_index}", 1)
        task.writer_index = writer_index
        task.batch_queue = runtime.writer_queues[writer_index]
        batch = RowBatch(columns=["id"], rows=[(writer_index + 1,)])
        task.batch_queue.put_nowait(
            QueuedKeyBatch(
                task=task,
                batch_index=1,
                start_ordinal=1,
                stop_ordinal=2,
                batch=batch,
                read_started_at=now,
                read_completed_at=now,
                queued_at=now,
                approximate_memory_bytes=batch.approx_memory_bytes(),
            )
        )
        progress.start_key(transfer_slice.index, started_at=now)
        progress.materialize_key(transfer_slice.index, 1, started_at=now)
        progress.assign_key(transfer_slice.index, writer_index)
        tasks.append(task)

    first_log_started = threading.Event()
    release_first_log = threading.Event()
    second_log_started = threading.Event()
    committed_totals: list[int] = []

    def log_batch(_task: ReadyKeyTask, batch_progress: Any) -> None:
        if batch_progress.key_id == slices[0].index:
            first_log_started.set()
            assert release_first_log.wait(2)
        else:
            second_log_started.set()
        committed_totals.append(batch_progress.snapshot.committed_rows)

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "insert_target_batch",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", log_batch)
    monkeypatch.setattr(
        staged_keyed_pipeline, "log_key_verification", lambda *_args, **_kwargs: None
    )

    def consume(writer_index: int) -> None:
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            state,
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            progress,
            state_lock,
            writer_index,
            f"target.stage_{writer_index}",
            tasks[writer_index],
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )

    first, first_errors = _thread(lambda: consume(0))
    assert first_log_started.wait(2)
    tasks[0].batch_queue.put(
        KeyReadComplete(tasks[0], streamed_rows=1, batch_count=1),
        timeout=1,
    )
    second, second_errors = _thread(lambda: consume(1))
    tasks[1].batch_queue.put(
        KeyReadComplete(tasks[1], streamed_rows=1, batch_count=1),
        timeout=1,
    )
    assert not second_log_started.wait(0.1)
    release_first_log.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert first_errors == []
    assert second_errors == []
    assert committed_totals == [1, 2]


def test_key_acknowledgement_is_published_only_after_target_validation(
    monkeypatch: Any,
) -> None:
    options = _options(
        transfer_slices=[_options().transfer_slices[0]],  # type: ignore[index]
        transfer_concurrency=_concurrency(1, 1),
    )
    transfer_slice = (options.transfer_slices or [])[0]

    def setup() -> tuple[
        LazyKeyedRuntime,
        ReadyKeyTask,
        TransferProgressTracker,
    ]:
        runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
        task = _ready_task(transfer_slice, "source.zero", 0)
        task.writer_index = 0
        task.batch_queue = runtime.writer_queues[0]
        task.batch_queue.put_nowait(KeyReadComplete(task, streamed_rows=0, batch_count=0))
        progress = TransferProgressTracker(total_key_count=1, active_writers=1)
        progress.start_key(transfer_slice.index, started_at=0.0)
        progress.materialize_key(transfer_slice.index, 0, started_at=0.0)
        progress.assign_key(transfer_slice.index, 0)
        return runtime, task, progress

    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline, "log_key_verification", lambda *_args, **_kwargs: None
    )
    failed_runtime, failed_task, failed_progress = setup()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("invalid target key")),
    )

    with pytest.raises(RuntimeError, match="invalid target key"):
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            _state(),
            failed_runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            failed_progress,
            threading.Lock(),
            0,
            None,
            failed_task,
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )
    assert failed_runtime.verified == {}
    assert failed_runtime.drop_ready.empty()

    runtime, task, progress = setup()
    events: list[str] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda *_args: events.append("validated"),
    )
    original_mark_verified = runtime.mark_verified

    def mark_verified(checkpoint: Any) -> None:
        events.append("checkpointed")
        original_mark_verified(checkpoint)

    monkeypatch.setattr(runtime, "mark_verified", mark_verified)
    staged_keyed_pipeline._consume_key(
        options,
        _metadata(),
        _state(),
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
        progress,
        threading.Lock(),
        0,
        None,
        task,
        staged_keyed_pipeline._make_batch_sizer(options),
        1,
    )

    acknowledgement = runtime.drop_ready.get_nowait()
    assert events == ["validated", "checkpointed"]
    assert acknowledgement.task is task
    assert runtime.verified[transfer_slice.index].expected_rows == 0


@pytest.mark.parametrize(
    ("malformation", "message"),
    [
        ("task", "Source batch completion marker is inconsistent"),
        ("batch_count", "Source batch completion marker is inconsistent"),
        ("streamed_rows", "Source and writer batch totals do not match"),
    ],
)
def test_key_consumer_rejects_malformed_read_completion(
    malformation: str,
    message: str,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    task = _ready_task(slices[0], "source.key", 0)
    other_task = _ready_task(slices[1], "source.other", 0)
    task.batch_queue = runtime.writer_queues[0]
    completion = KeyReadComplete(
        other_task if malformation == "task" else task,
        streamed_rows=1 if malformation == "streamed_rows" else 0,
        batch_count=1 if malformation == "batch_count" else 0,
    )
    task.batch_queue.put_nowait(completion)

    with pytest.raises(RuntimeError, match=message):
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            _state(),
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            TransferProgressTracker(total_key_count=2, active_writers=1),
            threading.Lock(),
            0,
            None,
            task,
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )


@pytest.mark.parametrize(
    ("malformation", "message"),
    [
        ("task", "Source batch belongs to a different key"),
        ("batch_order", "Logical source batch order is not contiguous"),
    ],
)
def test_key_consumer_rejects_malformed_queued_batch(
    malformation: str,
    message: str,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    task = _ready_task(slices[0], "source.key", 1)
    other_task = _ready_task(slices[1], "source.other", 1)
    task.batch_queue = runtime.writer_queues[0]
    task.batch_queue.put_nowait(
        QueuedKeyBatch(
            task=other_task if malformation == "task" else task,
            batch_index=2 if malformation == "batch_order" else 1,
            start_ordinal=1,
            stop_ordinal=2,
            batch=RowBatch(columns=["id"], rows=[(1,)]),
            read_started_at=0.0,
            read_completed_at=0.1,
            approximate_memory_bytes=8,
        )
    )

    with pytest.raises(RuntimeError, match=message):
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            _state(),
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            TransferProgressTracker(total_key_count=2, active_writers=1),
            threading.Lock(),
            0,
            "target.stage",
            task,
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )


def test_stage_state_sync_requires_every_key_to_be_verified() -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    runtime.mark_verified(
        VerifiedKey(
            slice_index=slices[0].index,
            expected_rows=1,
            streamed_rows=1,
            target_stage="target.stage_0",
        )
    )

    with pytest.raises(RuntimeError, match="Not every transfer key reached"):
        staged_keyed_pipeline._sync_stage_state(
            options,
            _state(),
            runtime,
            require_complete=True,
        )


def test_drop_drain_drops_only_the_exact_acknowledged_source_stage(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    tasks = [
        _ready_task(slices[0], "source.acknowledged", 0),
        _ready_task(slices[1], "source.unverified", 0),
    ]
    for task in tasks:
        assert runtime.live_stage_credits.acquire(blocking=False)
        runtime.reserve_source_stage(task.source_stage)
        runtime.publish_source_stage(task)
    runtime.drop_ready.put_nowait(DropReady(tasks[0], None))
    dropped: list[ReadyKeyTask] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda _options, _source_ref, task: dropped.append(task),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    count = staged_keyed_pipeline._drain_drop_ready(
        options,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
        limit=None,
    )

    assert count == 1
    assert dropped == [tasks[0]]
    assert runtime.source_stage_tables == ["source.unverified"]
    assert runtime.source_stages_dropped == 1


def test_runtime_preserves_first_worker_error_and_unblocks_queue_waiters() -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    waiting = threading.Event()

    def wait_for_ready() -> None:
        waiting.set()
        staged_keyed_pipeline._get_with_cancellation(runtime.ready, runtime)

    worker, errors = _thread(wait_for_ready)
    assert waiting.wait(1)
    first = OSError("writer failed")
    runtime.fail(first)
    runtime.fail(ValueError("later reader failure"))
    worker.join(2)

    assert not worker.is_alive()
    assert runtime.first_error is first
    assert runtime.cancellation.is_set()
    assert len(errors) == 1
    with pytest.raises(OSError, match="writer failed"):
        runtime.raise_first_error()


def test_runtime_rejects_duplicate_logical_batch_commit() -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    logical_id = (0, 1, 1, 3)
    runtime.mark_batch_success(logical_id)
    with pytest.raises(RuntimeError, match="committed twice"):
        runtime.mark_batch_success(logical_id)


def test_keyed_progress_log_messages_are_tag_first_and_phase_complete(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    transfer_slice = (options.transfer_slices or [])[0]
    task = _ready_task(transfer_slice, "source.stage", 20)
    now = [0.0]
    tracker = TransferProgressTracker(
        total_key_count=2,
        active_writers=1,
        clock=lambda: now[0],
    )
    tracker.start_key(transfer_slice.index)
    tracker.materialize_key(transfer_slice.index, 20)
    tracker.assign_key(transfer_slice.index, 0)
    messages: list[str] = []
    monkeypatch.setattr(staged_keyed_logging, "time_print", messages.append)

    staged_keyed_logging.log_pipeline_start(
        options,
        LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1),
    )
    now[0] = 2.0
    first = tracker.commit_batch(
        logical_batch_id=(0, 1),
        key_id=transfer_slice.index,
        batch_index=1,
        rows=10,
        timing=staged_keyed_pipeline.BatchTiming(1.0, 1.2, 1.3, 2.0, 1024),
        writer_id=0,
    )
    assert first is not None
    staged_keyed_logging.log_batch_progress(task, first)
    now[0] = 4.0
    second = tracker.commit_batch(
        logical_batch_id=(0, 2),
        key_id=transfer_slice.index,
        batch_index=2,
        rows=10,
        timing=staged_keyed_pipeline.BatchTiming(3.0, 3.2, 3.3, 4.0, 2048),
        writer_id=0,
    )
    assert second is not None
    staged_keyed_logging.log_batch_progress(task, second)
    verification = tracker.verify_key(transfer_slice.index)
    assert verification is not None
    staged_keyed_logging.log_key_verification(task, verification)
    other_slice = (options.transfer_slices or [])[1]
    tracker.start_key(other_slice.index)
    tracker.materialize_key(other_slice.index, 0)
    tracker.assign_key(other_slice.index, 0)
    assert tracker.verify_key(other_slice.index) is not None
    loading = tracker.mark_loading_complete()
    staged_keyed_logging.log_loading_complete(loading)

    assert messages[0].startswith("Starting keyed source-stage transfer: 2 keys")
    assert "source connection limit 1; target connection limit 1" in messages[0]
    assert messages[1].startswith(f"{task.tag} Staged batch 1: 10 rows")
    assert "rolling rate unavailable" in messages[1]
    assert "load ETA unavailable; total transfer ETA unavailable" in messages[1]
    assert messages[2].startswith(f"{task.tag} Staged batch 2: 10 rows")
    assert "rolling rate 7 rows/s" in messages[2]
    assert "approximate RAM rate" in messages[2]
    assert messages[3].startswith(f"{task.tag} Verified 20 rows")
    assert messages[4].startswith("Completed source-stage loading: 20 rows")


def test_persistent_acknowledged_drop_failure_blocks_finalization(monkeypatch: Any) -> None:
    transfer_slice = TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")
    options = _options(
        transfer_slices=[transfer_slice],
        transfer_concurrency=_concurrency(1, 1),
    )
    metadata = _metadata()
    state = _state()
    finalization_calls: list[str] = []

    class AttemptConnectionManager(_LeaseManager):
        def __init__(
            self,
            _connection_key: str,
            _capacity: int,
            *,
            role: str,
            **_kwargs: Any,
        ) -> None:
            super().__init__()
            self.role = role

        def close(self) -> None:
            return

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "BoundedConnectionManager",
        AttemptConnectionManager,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", lambda *_args: metadata)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda *_args: "source.exact",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        lambda *_args: 0,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda *_args: (_ for _ in ()).throw(OSError("persistent drop failure")),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_validate_target_stages",
        lambda *_args: finalization_calls.append("aggregate validation"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_loaded_stage_row_count",
        lambda **_kwargs: finalization_calls.append("row-count validation"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda *_args: finalization_calls.append("destination mutation"),
    )

    with pytest.raises(
        staged_keyed_pipeline.AcknowledgedSourceStageDropError,
        match="Could not drop an acknowledged source stage",
    ):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert finalization_calls == []
    assert state.source_stage_tables == ["source.exact"]


def test_mid_attempt_schema_drift_fails_cached_contract_without_refresh(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    state = TransferStageState(target_exists=False)
    inspections: list[str] = []
    metadata_uses: list[Any] = []
    materialized: list[int] = []
    validated: list[int] = []
    dropped: list[int] = []
    post_load_phases: list[str] = []

    def inspect(_backend: str, _connection: Any, sql: str) -> list[SourceColumn]:
        inspections.append(sql)
        return [SourceColumn("id", "bigint")]

    def materialize(
        _options: TransferOptions,
        _source_ref: Any,
        metadata: Any,
        transfer_slice: TransferSlice,
        _source_stage: str,
    ) -> int:
        metadata_uses.append(metadata)
        materialized.append(transfer_slice.index)
        return 1

    def read(
        _options: TransferOptions,
        _source_ref: Any,
        task: ReadyKeyTask,
        metadata: Any,
        _start: int,
        _stop: int,
        **_kwargs: Any,
    ) -> RowBatch:
        metadata_uses.append(metadata)
        columns = ["id"] if task.transfer_slice.index == 0 else ["renamed_id"]
        return RowBatch(columns, [(task.transfer_slice.index,)])

    def insert(
        _options: TransferOptions,
        _target_ref: Any,
        _stage: str,
        batch: QueuedKeyBatch,
        metadata: Any,
        **_kwargs: Any,
    ) -> int:
        metadata_uses.append(metadata)
        if tuple(batch.batch.columns) != metadata.source_columns:
            raise RuntimeError("later key is incompatible with cached schema contract")
        return batch.batch.row_count

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "BoundedConnectionManager",
        lambda *_args, **_kwargs: _LeaseManager(),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(staged_keyed_pipeline, "inspect_source_query_schema", inspect)
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
        "allocate_source_stage_name",
        lambda _options, _ref, slice_index: f"source.slice_{slice_index}",
    )
    monkeypatch.setattr(staged_keyed_pipeline, "materialize_source_key", materialize)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda *_args, **_kwargs: "target.writer_0",
    )
    monkeypatch.setattr(staged_keyed_pipeline, "read_key_batch", read)
    monkeypatch.setattr(staged_keyed_pipeline, "insert_target_batch", insert)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda _options, _ref, _metadata, task, *_args: validated.append(task.transfer_slice.index),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda _options, _ref, task: dropped.append(task.transfer_slice.index),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_validate_target_stages",
        lambda *_args: post_load_phases.append("aggregate validation"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda *_args, **_kwargs: post_load_phases.append("destination mutation"),
    )

    with pytest.raises(RuntimeError, match="incompatible with cached schema contract"):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert inspections == ["SELECT 1 AS id"]
    assert len({id(metadata) for metadata in metadata_uses}) == 1
    assert materialized == [0, 1]
    assert validated == [0]
    assert 1 not in dropped
    assert "source.slice_1" in (state.source_stage_tables or [])
    assert post_load_phases == []


def test_public_keyed_full_retry_rematerializes_dropped_key_with_fresh_runtime(  # noqa: PLR0915
    monkeypatch: Any,
) -> None:
    options = _options(
        transfer_concurrency=_concurrency(1, 1),
        replace_target_table=True,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=2,
        full_timeout_increment=0,
    )
    created_runtimes: list[LazyKeyedRuntime] = []
    initial_checkpoints: list[dict[int, VerifiedKey]] = []
    attempt_states: list[TransferStageState] = []
    metadata_attempts: list[int] = []
    transfer_ids: list[str | None] = []
    materialized: dict[int, list[int]] = {}
    validated: dict[int, list[int]] = {}
    dropped: list[tuple[int, int]] = []
    finalized: list[int] = []
    first_key_dropped = threading.Event()

    class RecordingRuntime(LazyKeyedRuntime):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created_runtimes.append(self)
            initial_checkpoints.append(self.verified)

    class AttemptConnectionManager(_LeaseManager):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            super().__init__()

    def create_state(*_args: Any) -> TransferStageState:
        state = _state()
        attempt_states.append(state)
        return state

    def prepare(
        current_options: TransferOptions,
        _refs: TransferConnectionRefs,
        _stage_state: TransferStageState,
    ) -> Any:
        metadata_attempts.append(current_options.attempt_number)
        transfer_ids.append(current_options.transfer_id)
        return _metadata()

    def materialize(
        current_options: TransferOptions,
        _source_ref: Any,
        _metadata_value: Any,
        transfer_slice: TransferSlice,
        _source_stage: str,
    ) -> int:
        materialized.setdefault(current_options.attempt_number, []).append(transfer_slice.index)
        return 1

    def validate(
        current_options: TransferOptions,
        _target_ref: Any,
        _metadata_value: Any,
        task: ReadyKeyTask,
        _target_stage: Any,
        streamed_rows: int,
    ) -> None:
        attempt = current_options.attempt_number
        validated.setdefault(attempt, []).append(task.transfer_slice.index)
        assert streamed_rows == 1
        if attempt == 1 and task.transfer_slice.index == 1:
            assert first_key_dropped.wait(timeout=1)
            raise OSError("second key failed after the first source stage was dropped")

    def drop(
        current_options: TransferOptions,
        _source_ref: Any,
        task: ReadyKeyTask,
    ) -> None:
        dropped.append((current_options.attempt_number, task.transfer_slice.index))
        if current_options.attempt_number == 1 and task.transfer_slice.index == 0:
            first_key_dropped.set()

    monkeypatch.setattr(transfer_api, "build_transfer_options", lambda **_kwargs: options)
    monkeypatch.setattr(staged_keyed_pipeline, "LazyKeyedRuntime", RecordingRuntime)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "BoundedConnectionManager",
        AttemptConnectionManager,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", create_state)
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", prepare)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda current_options, _ref, slice_index: (
            f"source.attempt_{current_options.attempt_number}.slice_{slice_index}"
        ),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "materialize_source_key", materialize)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda current_options, _ref, _metadata_value, _writer, **_kwargs: (
            f"target.attempt_{current_options.attempt_number}.writer_0"
        ),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda _options, _ref, task, _metadata_value, start, stop, **_kwargs: RowBatch(
            ["id"],
            [(task.transfer_slice.index,) for _ordinal in range(start, stop)],
        ),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "insert_target_batch",
        lambda _options, _ref, _stage, batch, _metadata_value, **_kwargs: batch.batch.row_count,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", validate)
    monkeypatch.setattr(staged_keyed_pipeline, "drop_source_stage", drop)
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_consolidate_created_stages",
        lambda *_args: 0,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_loaded_stage_row_count",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda current_options, *_args, **_kwargs: finalized.append(current_options.attempt_number),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "capture_final_target_count", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_loading_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_transfer_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(transfer_api, "time_print", lambda *_args, **_kwargs: None)

    assert transfer_api.transfer_table("source", "target") == 2

    assert initial_checkpoints == [{}, {}]
    assert len(created_runtimes) == 2
    assert created_runtimes[0] is not created_runtimes[1]
    assert set(created_runtimes[0].verified) == {0}
    assert set(created_runtimes[1].verified) == {0, 1}
    assert len(attempt_states) == 2
    assert attempt_states[0] is not attempt_states[1]
    assert metadata_attempts == [1, 2]
    assert len(set(transfer_ids)) == 1
    assert materialized == {1: [0, 1], 2: [0, 1]}
    assert validated == {1: [0, 1], 2: [0, 1]}
    assert dropped == [(1, 0), (2, 0), (2, 1)]
    assert finalized == [2]


def test_target_pool_bounds_every_keyed_target_phase(  # noqa: C901, PLR0915
    monkeypatch: Any,
) -> None:
    transfer_slice = TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")
    options = _options(
        transfer_slices=[transfer_slice],
        transfer_concurrency=_concurrency(1, 2),
    )
    state = _state()
    managers: dict[str, BoundedConnectionManager] = {}
    opened: list[Any] = []
    phases: list[str] = []

    class Connection:
        def __init__(self, key: str) -> None:
            self.key = key
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    def open_connection(key: str) -> Connection:
        connection = Connection(key)
        opened.append(connection)
        return connection

    class RecordingManager(BoundedConnectionManager):
        def __init__(self, key: str, capacity: int, *, role: str, **_kwargs: Any) -> None:
            super().__init__(
                key,
                capacity,
                role=role,
                open_connection=open_connection,
            )
            managers[role] = self

    def observe(phase: str, target_ref: dict[str, Any]) -> None:
        connection = target_ref["connection"]
        assert connection.key == "target"
        assert connection.close_count == 0
        phases.append(phase)

    def prepare(
        _options: Any,
        refs: TransferConnectionRefs,
        _stage_state: TransferStageState,
    ) -> Any:
        observe("metadata", refs.target)
        return _metadata()

    def run_workers(
        _options: Any,
        _metadata_value: Any,
        _stage_state: Any,
        runtime: LazyKeyedRuntime,
        _source_connections: Any,
        target_connections: BoundedConnectionManager,
        progress: TransferProgressTracker,
        **_kwargs: Any,
    ) -> None:
        target_connections.run(
            "per-key validation",
            lambda target_ref: observe("per-key validation", target_ref),
        )
        runtime.register_target_stage(0, "target_stage.writer_0")
        runtime.mark_verified(VerifiedKey(0, 0, 0, "target_stage.writer_0"))
        progress.start_key(0)
        progress.materialize_key(0, 0)
        progress.assign_key(0, 0)
        progress.verify_key(0)

    def validate_aggregate(
        _options: Any,
        target_ref: dict[str, Any],
        *_args: Any,
    ) -> None:
        observe("aggregate validation", target_ref)

    def validate_row_count(**kwargs: Any) -> None:
        kwargs["target_connection_runner"](
            "row-count validation",
            lambda target_ref: observe("row-count validation", target_ref),
        )

    def finalize(*_args: Any, **kwargs: Any) -> None:
        kwargs["target_connection_runner"](
            "finalization",
            lambda target_ref: observe("finalization", target_ref),
        )

    def cleanup(*_args: Any, **kwargs: Any) -> None:
        assert kwargs["safe_exception_logging"] is True
        kwargs["target_connection_runner"](
            "cleanup",
            lambda target_ref: observe("cleanup", target_ref),
        )

    monkeypatch.setattr(staged_keyed_pipeline, "BoundedConnectionManager", RecordingManager)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", prepare)
    monkeypatch.setattr(staged_keyed_pipeline, "_run_lazy_workers", run_workers)
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", validate_aggregate)
    monkeypatch.setattr(
        staged_keyed_pipeline, "validate_loaded_stage_row_count", validate_row_count
    )
    monkeypatch.setattr(staged_keyed_pipeline, "finalize_loaded_stage", finalize)
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", cleanup)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_loading_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_transfer_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    assert (
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )
        == 0
    )

    target_manager = managers["target transfer pool"]
    assert phases == [
        "metadata",
        "per-key validation",
        "aggregate validation",
        "row-count validation",
        "finalization",
        "cleanup",
    ]
    assert target_manager.capacity == 2
    assert target_manager.high_water_mark == 1
    target_connections = [connection for connection in opened if connection.key == "target"]
    assert len(target_connections) == 1
    assert target_connections[0].close_count == 1


def test_target_pool_close_failure_after_finalization_is_nonretryable(
    monkeypatch: Any,
) -> None:
    transfer_slice = TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")
    options = _options(
        transfer_slices=[transfer_slice],
        transfer_concurrency=_concurrency(1, 1),
    )
    finalized: list[int] = []

    class CloseFailingManager(_LeaseManager):
        def __init__(
            self,
            _connection_key: str,
            _capacity: int,
            *,
            role: str,
            **_kwargs: Any,
        ) -> None:
            super().__init__()
            self.role = role

        def close(self) -> None:
            if self.role == "target transfer pool":
                raise BoundedConnectionCloseError("target connection remains live")

    def run_workers(
        _options: Any,
        _metadata_value: Any,
        _stage_state: Any,
        runtime: LazyKeyedRuntime,
        _source_connections: Any,
        _target_connections: Any,
        progress: TransferProgressTracker,
        **_kwargs: Any,
    ) -> None:
        runtime.mark_verified(VerifiedKey(0, 0, 0, None))
        progress.start_key(0)
        progress.materialize_key(0, 0)
        progress.assign_key(0, 0)
        progress.verify_key(0)

    monkeypatch.setattr(staged_keyed_pipeline, "BoundedConnectionManager", CloseFailingManager)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: _state())
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", lambda *_args: _metadata())
    monkeypatch.setattr(staged_keyed_pipeline, "_run_lazy_workers", run_workers)
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "_consolidate_created_stages", lambda *_args: 0)
    monkeypatch.setattr(
        staged_keyed_pipeline, "validate_loaded_stage_row_count", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda *_args, **_kwargs: finalized.append(1),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_loading_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)

    with pytest.raises(BoundedConnectionCloseError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert finalized == [1]
    assert exc_info.value.analytics_toolkit_sql_retry_safe is False


def test_keyed_target_cleanup_retry_redacts_exception_details(monkeypatch: Any) -> None:
    messages: list[str] = []
    state = _state()
    state.stage_table = "target_stage.writer_0"
    state.stage_table_created = True

    def cleanup_with_retry(*_args: Any, **kwargs: Any) -> None:
        def fail(_attempt: int) -> None:
            raise RuntimeError("secret row and SQL text")

        kwargs["retry_fn"](
            operation_name="keyed cleanup",
            retry_cnt=1,
            timeout_increment=0,
            operation=fail,
        )

    monkeypatch.setattr(finalize, "cleanup_stage_table_with_retry", cleanup_with_retry)
    monkeypatch.setattr(
        transfer_retry,
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    with pytest.raises(RuntimeError, match="secret row"):
        finalize.cleanup_stage(
            _options(),
            TransferConnectionRefs(),
            state,
            1,
            target_connection_runner=lambda _role, operation: operation({"connection": object()}),
            safe_exception_logging=True,
        )

    assert messages
    assert all("secret row" not in message for message in messages)
    assert any("RuntimeError" in message for message in messages)


def test_row_count_and_finalization_accept_bounded_target_runner(monkeypatch: Any) -> None:
    options = _options(write_mode="append", validate_row_count=True)
    state = _state()
    state.expected_source_rows = 1
    state.stage_table = "target_stage.writer_0"
    state.stage_tables = [state.stage_table]
    state.first_non_empty_batch = pd.DataFrame({"id": [1]})
    state.insert_column_types = {"id": "BIGINT"}
    roles: list[str] = []
    connection = object()

    def run(role: str, operation: Callable[[dict[str, Any]], Any]) -> Any:
        roles.append(role)
        return operation({"connection": connection})

    monkeypatch.setattr(
        row_counts,
        "count_table_rows",
        lambda _backend, current, _table, **_kwargs: 1 if current is connection else -1,
    )
    row_counts.validate_loaded_stage_row_count(
        options=options,
        connection_refs=TransferConnectionRefs(),
        stage_state=state,
        total_rows=1,
        open_connection=lambda _key: pytest.fail("opened an unbudgeted row-count connection"),
        target_connection_runner=run,
    )

    monkeypatch.setattr(finalize, "validate_stage_uniqueness", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "validate_stage_target_key_overlap", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "finalize_stage_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(finalize, "analyze_table", lambda **_kwargs: None)
    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda *_args, **_kwargs: pytest.fail("opened an unbudgeted finalization connection"),
    )
    finalize.finalize_loaded_stage(
        options,
        TransferConnectionRefs(),
        state,
        1,
        target_connection_runner=run,
    )

    assert roles == [
        "validate_stage_row_count",
        "validate_stage",
        "validate_stage",
        "finalize_target",
        "analyze_target",
    ]


def test_final_target_metadata_count_uses_bounded_target_runner(
    monkeypatch: Any,
) -> None:
    options = _options(collect_final_target_count=True)
    manager = _LeaseManager()
    counted_connections: list[Any] = []

    monkeypatch.setattr(
        row_counts,
        "count_table_rows",
        lambda _backend, connection, _table, **_kwargs: (
            counted_connections.append(connection) or 17
        ),
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "best_effort_transfer_target_count",
        lambda current_options, **kwargs: row_counts.best_effort_transfer_target_count(
            current_options,
            open_connection=lambda _key: pytest.fail("opened an unbudgeted connection"),
            count_rows=row_counts.count_table_rows,
            **kwargs,
        ),
    )

    staged_keyed_io.capture_final_target_count(
        options,
        manager,  # type: ignore[arg-type]
    )

    assert options.final_target_rows == 17
    assert counted_connections
    assert manager.lease_count == 1
    assert manager.high_water_mark == 1


def test_bounded_connection_manager_tracks_high_water_replacement_and_close() -> None:
    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        return connection

    manager = BoundedConnectionManager(
        "source",
        2,
        role="source test pool",
        open_connection=open_connection,
    )
    with manager.lease() as first_ref:
        with manager.lease() as second_ref:
            assert first_ref["connection"] is not second_ref["connection"]
            assert manager.high_water_mark == 2
        failed_connection = first_ref["connection"]
        manager.replace_connection("source", first_ref)
        assert failed_connection.close_count == 1
        assert first_ref["connection"] is opened[-1]
        assert manager.high_water_mark == 2

    manager.close()

    assert len(opened) == 3
    assert [connection.close_count for connection in opened] == [1, 1, 1]
    with pytest.raises(RuntimeError, match="manager is closed"), manager.lease():
        pass


def test_consolidation_replaces_connections_through_bounded_target_pool(
    monkeypatch: Any,
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []
    inserted_with: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(
        staged_attempt,
        "insert_from_table",
        lambda _backend, connection, *_args, **_kwargs: inserted_with.append(connection),
    )
    manager = BoundedConnectionManager(
        "target",
        1,
        role="target consolidation pool",
        open_connection=open_connection,
    )
    with manager.lease() as target_ref:
        original = target_ref["connection"]
        staged_attempt._consolidate_worker_stages(
            _options(transfer_concurrency=_concurrency(1, 1)),
            target_ref,
            _state(),
            ["target.primary", "target.secondary"],
        )
        replacement = target_ref["connection"]

    assert original.close_count == 1
    assert replacement is not original
    assert inserted_with == [replacement]
    assert manager.high_water_mark == 1
    manager.close()
    assert [connection.close_count for connection in opened] == [1, 1]


def test_clickhouse_stage_cleanup_drops_every_policy_created_companion() -> None:
    commands: list[str] = []
    connection = SimpleNamespace(command=commands.append)
    policy = SimpleNamespace(
        create_distributed_pair=True,
        shard_on_cluster="STAGE_SHARDS",
        distributed_on_cluster="STAGE_DISTRIBUTED",
    )
    stage_table = "scratch.transfer_stage"

    load_stage.cleanup_stage_table(
        "ch",
        connection,
        stage_table,
        ch_creation_policy=policy,
    )

    shard_table = build_ch_shard_table_name(stage_table)
    assert len(commands) == 4
    assert any(f"DROP TABLE IF EXISTS {stage_table}" == sql for sql in commands)
    assert any(stage_table in sql and "STAGE_DISTRIBUTED" in sql for sql in commands)
    assert any(f"DROP TABLE IF EXISTS {shard_table}" == sql for sql in commands)
    assert any(shard_table in sql and "STAGE_SHARDS" in sql for sql in commands)


def test_lazy_clickhouse_dry_run_cleans_every_conditional_stage_companion() -> None:
    policy = SimpleNamespace(
        create_distributed_pair=True,
        shard_on_cluster="STAGE_SHARDS",
        distributed_on_cluster="STAGE_DISTRIBUTED",
    )
    options = _options(to_db_backend="ch", staging_ch_policy=policy)
    plan = SqlPlan(operation="transfer_table")

    dry_run._add_target_stage_cleanup(
        plan,
        options,
        "scratch.transfer_stage",
        lazy=True,
    )

    shard_table = build_ch_shard_table_name("scratch.transfer_stage")
    assert len(plan.statements) == 4
    assert all(step.phase == "drop_stage_if_created" for step in plan.statements)
    assert any(shard_table in step.sql for step in plan.statements)
    assert any("STAGE_SHARDS" in step.sql for step in plan.statements)
    assert any("STAGE_DISTRIBUTED" in step.sql for step in plan.statements)


def test_clickhouse_per_host_fallback_stays_inside_target_pool(monkeypatch: Any) -> None:
    active = 0
    high_water = 0
    opened_roles: list[str] = []
    state_lock = threading.Lock()

    class Connection:
        def __init__(self, role: str) -> None:
            nonlocal active, high_water
            self.role = role
            self.closed = False
            opened_roles.append(role)
            with state_lock:
                active += 1
                high_water = max(high_water, active)

        def command(self, _sql: str) -> None:
            return None

        def close(self) -> None:
            nonlocal active
            if self.closed:
                return
            self.closed = True
            with state_lock:
                active -= 1

    waits = 0

    def wait_for_absence(*_args: Any, **_kwargs: Any) -> None:
        nonlocal waits
        waits += 1
        if waits == 1:
            raise TimeoutError("cluster DDL remained visible")

    monkeypatch.setattr(
        ch_lifecycle,
        "_wait_for_ch_distributed_table_pair_absence",
        wait_for_absence,
    )
    monkeypatch.setattr(
        ch_lifecycle,
        "_query_ch_configured_cluster_hosts",
        lambda *_args, **_kwargs: ["host-a", "host-b", "host-c"],
    )
    monkeypatch.setattr(
        ch_lifecycle,
        "_select_ch_hosts_for_local_drop",
        lambda _connection, _pair, **kwargs: kwargs["configured_hosts"],
    )
    manager = BoundedConnectionManager(
        "target",
        1,
        role="target ClickHouse pool",
        open_connection=lambda _key: Connection("coordinator"),
    )

    ch_lifecycle.drop_ch_distributed_table_pair_bounded(
        "sandbox.target",
        "CORE",
        query_label=None,
        ch_retry_per_host_drops=True,
        connection_runner=lambda role, operation: manager.run(
            role,
            lambda ref: operation(ref["connection"]),
        ),
        host_connection_runner=lambda host, operation: manager.run_with_connection(
            "host cleanup",
            lambda: Connection(host),
            operation,
        ),
    )
    manager.close()

    assert high_water == 1
    assert active == 0
    assert {"host-a", "host-b", "host-c"}.issubset(opened_roles)
    assert manager.high_water_mark == 1


def test_nonlazy_clickhouse_finalization_builds_its_own_bounded_pool(
    monkeypatch: Any,
) -> None:
    active = 0
    high_water = 0
    opened: list[str] = []

    class Connection:
        def __init__(self, role: str) -> None:
            nonlocal active, high_water
            self.role = role
            self.closed = False
            opened.append(role)
            active += 1
            high_water = max(high_water, active)

        def close(self) -> None:
            nonlocal active
            if not self.closed:
                self.closed = True
                active -= 1

    class Adapter:
        @staticmethod
        def needs_bounded_replace_preclear(_only_shard: bool) -> bool:
            return True

        def open_transfer_host_connection(self, _key: str, host: str) -> Connection:
            return Connection(host)

        def preclear_distributed_replace_target(self, *_args: Any, **kwargs: Any) -> bool:
            kwargs["connection_runner"]("coordinator", lambda _connection: None)
            for host in ("host-a", "host-b", "host-c"):
                kwargs["host_connection_runner"](host, lambda _connection: None)
            return True

    monkeypatch.setattr(finalize, "get_backend_adapter", lambda _backend: Adapter())
    monkeypatch.setattr(
        finalize,
        "get_sql_connection",
        lambda _key: Connection("coordinator"),
    )

    assert finalize._preclear_clickhouse_replace_target(
        _options(
            to_db_backend="ch",
            write_mode="replace",
            replace_target_table=True,
            transfer_concurrency=_concurrency(1, 1),
        ),
        TransferStageState(target_exists=True),
        target_connection_runner=None,
        target_host_connection_runner=None,
    )
    assert high_water == 1
    assert active == 0
    assert opened == ["coordinator", "host-a", "host-b", "host-c"]


def test_lazy_memory_target_is_shared_across_active_and_prefetched_batches() -> None:
    options = _options(
        transfer_concurrency=_concurrency(2, 2),
        target_batch_memory_bytes=4_000,
        min_batch_memory_bytes=2_000,
        max_batch_memory_bytes=8_000,
    )

    sizer = staged_keyed_pipeline._make_batch_sizer(options)

    assert sizer.target_memory_bytes == 1_000
    assert sizer.min_target_memory_bytes == 500
    assert sizer.max_target_memory_bytes == 2_000


def test_bounded_connection_manager_never_opens_after_close_failure() -> None:
    opened = 0

    class Connection:
        def close(self) -> None:
            raise OSError("close failed")

    def open_connection(_key: str) -> Connection:
        nonlocal opened
        opened += 1
        return Connection()

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source test pool",
        open_connection=open_connection,
    )
    with manager.lease() as ref, pytest.raises(
        RuntimeError,
        match="no replacement was opened",
    ):
        manager.replace_connection("source", ref)
    assert opened == 1


def test_bounded_connection_manager_rejects_open_completed_after_interrupt() -> None:
    open_started = threading.Event()
    release_open = threading.Event()
    yielded = threading.Event()

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        open_started.set()
        assert release_open.wait(2)
        connection = Connection()
        opened.append(connection)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source interrupt race pool",
        open_connection=open_connection,
    )

    def lease() -> None:
        with manager.lease():
            yielded.set()

    worker, errors = _thread(lease)
    assert open_started.wait(2)
    manager.interrupt_active()
    release_open.set()
    worker.join(2)

    assert not worker.is_alive()
    assert not yielded.is_set()
    assert len(errors) == 1
    assert "opening was cancelled" in str(errors[0])
    assert [connection.close_count for connection in opened] == [1]
    manager.close()


def test_cancelled_opener_close_failure_remains_tracked_for_strict_retry() -> None:
    open_started = threading.Event()
    release_open = threading.Event()

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1
            if self.close_count == 1:
                raise OSError("first close failed")

    connection = Connection()

    def open_connection(_key: str) -> Connection:
        open_started.set()
        assert release_open.wait(2)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source rejected-open pool",
        open_connection=open_connection,
    )
    worker, errors = _thread(lambda: manager.run("open", lambda _ref: None))
    assert open_started.wait(2)
    manager.interrupt_active()
    release_open.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], BoundedConnectionCloseError)
    manager.close()
    assert connection.close_count == 2


def test_bounded_connection_manager_rejects_replacement_completed_after_interrupt() -> None:
    replacement_started = threading.Event()
    release_replacement = threading.Event()
    replacement_returned = threading.Event()

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        if len(opened) == 2:
            replacement_started.set()
            assert release_replacement.wait(2)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source replacement race pool",
        open_connection=open_connection,
    )

    def replace() -> None:
        with manager.lease() as ref:
            manager.replace_connection("source", ref)
            replacement_returned.set()

    worker, errors = _thread(replace)
    assert replacement_started.wait(2)
    manager.interrupt_active()
    release_replacement.set()
    worker.join(2)

    assert not worker.is_alive()
    assert not replacement_returned.is_set()
    assert len(errors) == 1
    assert "replacement" in str(errors[0])
    assert [connection.close_count for connection in opened] == [1, 1]
    manager.close()


def test_bounded_connection_manager_close_failure_is_nonretryable_and_tracked() -> None:
    opened = 0

    class Connection:
        def close(self) -> None:
            raise OSError("still live")

    def open_connection(_key: str) -> Connection:
        nonlocal opened
        opened += 1
        return Connection()

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source strict close pool",
        open_connection=open_connection,
    )
    with manager.lease():
        pass
    with pytest.raises(BoundedConnectionCloseError) as exc_info:
        manager.close()

    assert exc_info.value.analytics_toolkit_sql_retry_safe is False
    assert opened == 1
    with pytest.raises(RuntimeError, match="manager is closed"), manager.lease():
        pass


def test_capacity_one_prefetch_waits_before_read_without_source_lease(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=1)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 1)
    task.batch_size = 1
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    occupied = KeyReadComplete(task, streamed_rows=0, batch_count=0)
    assert task.batch_slot.acquire(blocking=False)
    task.batch_queue.put_nowait(occupied)
    source_connections = _LeaseManager()
    read_called = threading.Event()

    def read(*_args: Any, **_kwargs: Any) -> RowBatch:
        assert source_connections.active == 1
        read_called.set()
        return RowBatch(["id"], [(1,)])

    monkeypatch.setattr(staged_keyed_pipeline, "read_key_batch", read)
    worker, errors = _thread(
        lambda: staged_keyed_pipeline._stream_ready_key(
            options,
            _metadata(),
            task,
            runtime,
            source_connections,  # type: ignore[arg-type]
        )
    )

    assert not read_called.wait(timeout=0.1)
    assert source_connections.active == 0
    assert worker.is_alive()
    assert task.batch_queue.get(timeout=1) is occupied
    task.batch_slot.release()
    assert read_called.wait(timeout=1)
    assert source_connections.released.wait(timeout=1)
    queued_batch = task.batch_queue.get(timeout=1)
    assert isinstance(queued_batch, QueuedKeyBatch)
    assert queued_batch.prefetch_slot is task.batch_slot
    queued_batch.prefetch_slot.release()
    queued_batch.prefetch_slot = None
    completion = task.batch_queue.get(timeout=1)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert isinstance(completion, KeyReadComplete)
    assert source_connections.high_water_mark == 1


def test_adaptive_keyed_stream_keeps_captured_prefetch_size_for_inflight_read(
    monkeypatch: Any,
) -> None:
    options = _options(
        transfer_concurrency=_concurrency(1, 1),
        batch_size=3,
        min_batch_size=1,
        max_batch_size=6,
        adaptive_batch_size=True,
    )
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 8)
    task.batch_size = 3
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    requested_ranges: list[tuple[int, int, int]] = []
    handed_off: list[QueuedKeyBatch] = []

    def read(
        _options: Any,
        _source_ref: Any,
        current_task: ReadyKeyTask,
        _metadata_value: Any,
        start: int,
        stop: int,
        **_kwargs: Any,
    ) -> RowBatch:
        captured_size = stop - start
        requested_ranges.append((start, stop, captured_size))
        if len(requested_ranges) == 2:
            # The writer adapts while this prefetch read is already in flight.
            # This batch must keep its captured size; only later reads use 1.
            current_task.batch_size = 1
        return RowBatch(["id"], [(ordinal,) for ordinal in range(start, stop)])

    def handoff(_queue: Any, item: QueuedKeyBatch, _runtime: Any) -> None:
        handed_off.append(item)
        staged_keyed_pipeline.release_queued_batch_slot(item)

    monkeypatch.setattr(staged_keyed_pipeline, "read_key_batch", read)
    monkeypatch.setattr(staged_keyed_pipeline, "_put_batch_with_cancellation", handoff)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_drain_drop_ready",
        lambda *_args, **_kwargs: 0,
    )

    staged_keyed_pipeline._stream_ready_key(
        options,
        _metadata(),
        task,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
    )

    assert requested_ranges == [
        (1, 4, 3),
        (4, 7, 3),
        (7, 8, 1),
        (8, 9, 1),
    ]
    assert [item.batch.row_count for item in handed_off] == [3, 3, 1, 1]
    assert all(
        item.batch.row_count == item.stop_ordinal - item.start_ordinal for item in handed_off
    )
    completion = task.batch_queue.get_nowait()
    assert isinstance(completion, KeyReadComplete)
    assert completion.streamed_rows == 8


def test_keyed_reader_rejects_a_short_requested_source_range(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=2)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 2)
    task.batch_size = 2
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    source_connections = _LeaseManager()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda *_args, **_kwargs: RowBatch(columns=["id"], rows=[(1,)]),
    )

    with pytest.raises(RuntimeError, match=r"returned 1 row\(s\); expected 2"):
        staged_keyed_pipeline._stream_ready_key(
            options,
            _metadata(),
            task,
            runtime,
            source_connections,  # type: ignore[arg-type]
        )

    assert task.batch_queue.empty()
    assert source_connections.active == 0


def test_keyed_reader_rejects_an_overlong_requested_source_range(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=2)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 2)
    task.batch_size = 2
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    source_connections = _LeaseManager()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda *_args, **_kwargs: RowBatch(columns=["id"], rows=[(1,), (2,), (3,)]),
    )

    with pytest.raises(RuntimeError, match=r"returned 3 row\(s\); expected 2"):
        staged_keyed_pipeline._stream_ready_key(
            options,
            _metadata(),
            task,
            runtime,
            source_connections,  # type: ignore[arg-type]
        )

    assert task.batch_queue.empty()
    assert task.batch_slot.acquire(blocking=False) is True
    assert source_connections.active == 0


def test_reader_processes_at_most_one_acknowledged_drop_between_batches(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=1)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 2)
    task.batch_size = 1
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    drain_limits: list[int | None] = []
    handoffs: list[Any] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_drain_drop_ready",
        lambda *_args, limit, **_kwargs: drain_limits.append(limit) or 0,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda *_args, **_kwargs: RowBatch(columns=["id"], rows=[(1,)]),
    )

    def handoff(_queue: Any, item: QueuedKeyBatch, _runtime: Any) -> None:
        handoffs.append(item)
        assert item.prefetch_slot is not None
        item.prefetch_slot.release()
        item.prefetch_slot = None

    monkeypatch.setattr(staged_keyed_pipeline, "_put_batch_with_cancellation", handoff)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_put_with_cancellation",
        lambda _queue, item, _runtime: handoffs.append(item),
    )

    staged_keyed_pipeline._stream_ready_key(
        options,
        _metadata(),
        task,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
    )

    assert drain_limits == [1, 1, 1]
    assert [item.batch_index for item in handoffs[:-1]] == [1, 2]
    assert isinstance(handoffs[-1], KeyReadComplete)


def test_slice_tag_uses_normalized_index_without_scanning_all_keys() -> None:
    class NonIterableSlices(list):
        def __iter__(self) -> Iterator[TransferSlice]:
            raise AssertionError("slice tag must not linearly scan transfer slices")

    options = _options()
    slices = list(options.transfer_slices or [])
    object.__setattr__(options, "transfer_slices", NonIterableSlices(slices))

    assert staged_keyed_logging.slice_tag(options, slices[1]).startswith("[slice=2/2 ")


def test_source_cleanup_attempts_every_table_and_preserves_first_error(
    monkeypatch: Any,
) -> None:
    dropped: list[str] = []

    def cleanup(_backend: str, _connection: Any, table: str, **_kwargs: Any) -> None:
        dropped.append(table)
        if table == "first":
            raise OSError("first cleanup")
        if table == "second":
            raise RuntimeError("second cleanup")

    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", cleanup)
    with pytest.raises(OSError, match="first cleanup"):
        staged_keyed_io.cleanup_source_stages(
            _options(),
            {"connection": object()},
            ["first", "second", "third"],
        )
    assert dropped == ["first", "second", "third"]


def test_keyed_staged_attempt_guards_and_staged_attempt_delegation(monkeypatch: Any) -> None:
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

    def delegate(_options: Any, *, insert_retry_cnt: int) -> int:
        delegated.append(insert_retry_cnt)
        return 7

    monkeypatch.setattr(
        staged_attempt,
        "run_keyed_staged_source_transfer_attempt",
        delegate,
    )

    assert staged_attempt.run_staged_source_transfer_attempt(_options(), insert_retry_cnt=3) == 7
    assert delegated == [3]
