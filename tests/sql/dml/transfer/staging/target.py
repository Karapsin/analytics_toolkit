from __future__ import annotations

# ruff: noqa: EM101, TRY003
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.dml.transfer.flow import (
    dry_run,
    staged_attempt,
    staged_keyed_io,
    staged_target,
)
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    QueuedKeyBatch,
    ReadyKeyTask,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)
from analytics_toolkit.sql.execution.plans import SqlPlan


def _options(mode: str = "parquet") -> TransferOptions:
    return TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="trino",
        source_sql="SELECT id FROM source",
        target_table="iceberg.sandbox.target",
        transfer_id="a" * 32,
        canonical_destination_identity="iceberg.sandbox.target",
        destination_hash="0123456789abcdef",
        source_transfer_staging_schema="source_stage",
        transfer_staging_schema="iceberg.target_stage",
        s3_transfer_staging_schema="hive.parquet_stage",
        s3_transfer_staging_location="s3://bucket/stage",
        transfer_staging_username="user",
        trino_mode=mode,  # type: ignore[arg-type]
        adaptive_batch_size=False,
    )


def test_prepare_shared_parquet_stage_uses_external_stage(monkeypatch: Any) -> None:
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        stage_column_types={"id": "BIGINT"},
    )
    calls: list[str] = []
    monkeypatch.setattr(
        staged_target,
        "ensure_parquet_staging_dependencies",
        lambda: calls.append("dependencies"),
    )

    def create(_options: Any, _refs: Any, stage_state: TransferStageState) -> None:
        calls.append("create")
        stage_state.stage_table = "hive.parquet_stage.shared"
        stage_state.stage_external_location = "s3://bucket/stage/shared/"
        stage_state.stage_table_created = True

    monkeypatch.setattr(staged_target, "create_parquet_stage_table", create)

    assert staged_target.prepare_shared_parquet_stage(
        _options(),
        TransferConnectionRefs(target={"connection": object()}),
        state,
    )
    assert calls == ["dependencies", "create"]
    assert state.stage_tables == ["hive.parquet_stage.shared"]


def test_prepare_shared_parquet_stage_requires_created_table(monkeypatch: Any) -> None:
    monkeypatch.setattr(staged_target, "ensure_parquet_staging_dependencies", lambda: None)
    monkeypatch.setattr(staged_target, "create_parquet_stage_table", lambda *_args: None)

    with pytest.raises(RuntimeError, match="did not return a stage table"):
        staged_target.prepare_shared_parquet_stage(
            _options(),
            TransferConnectionRefs(target={"connection": object()}),
            TransferStageState(target_exists=True),
        )


def test_unkeyed_parquet_worker_uploads_without_target_connection(monkeypatch: Any) -> None:
    opened: list[str] = []
    writes: list[dict[str, Any]] = []
    batch = RowBatch(["id"], [(1,), (2,)])
    claimed = False

    def claim(_worker: int, _size: int) -> Any:
        nonlocal claimed
        if claimed:
            return None
        claimed = True
        return SimpleNamespace(
            slice_id=0,
            start_ordinal=1,
            stop_ordinal=3,
            row_count=2,
        )

    scheduler = SimpleNamespace(
        claim=claim,
        complete=lambda *_args: None,
    )

    def connection(key: str) -> object:
        opened.append(key)
        return object()

    monkeypatch.setattr(staged_attempt, "get_sql_connection", connection)
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "_read_snapshot_range", lambda *_args: batch)
    monkeypatch.setattr(
        staged_attempt,
        "write_source_staged_batch",
        lambda *_args, **kwargs: writes.append(kwargs) or batch.row_count,
    )

    staged_attempt._range_worker(
        _options(),
        "source_stage.snapshot",
        ["id"],
        TransferStageState(
            target_exists=True,
            stage_column_types={"id": "BIGINT"},
            stage_external_location="s3://bucket/stage/shared/",
        ),
        "hive.parquet_stage.shared",
        scheduler,
        2,
        1,
    )

    assert opened == ["source"]
    assert len(writes) == 1
    assert writes[0]["worker_id"] == 2
    assert writes[0]["slice_index"] == 0
    assert writes[0]["file_index"] == 1
    assert writes[0]["start_ordinal"] == 1
    assert writes[0]["stop_ordinal"] == 3
    assert writes[0]["insert_fn"] is staged_attempt.insert_rows_batch


def test_keyed_parquet_batch_uploads_without_target_lease(monkeypatch: Any) -> None:
    transfer_slice = TransferSlice(3, ("x",), "", "SELECT 1", "key=x")
    task = ReadyKeyTask(transfer_slice, "source.stage", 2, "[slice=4/4]", 0.0)
    queued = QueuedKeyBatch(
        task=task,
        batch_index=5,
        start_ordinal=11,
        stop_ordinal=13,
        batch=RowBatch(["id"], [(1,), (2,)]),
        read_started_at=0.0,
        read_completed_at=1.0,
        approximate_memory_bytes=16,
    )
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        staged_keyed_io,
        "write_source_staged_batch",
        lambda *_args, **kwargs: writes.append(kwargs) or 2,
    )

    class NoLease:
        def lease(self, **_kwargs: Any) -> Any:
            raise AssertionError("Parquet upload must not lease a target connection")

    result = staged_keyed_io.write_keyed_target_batch(
        _options(),
        NoLease(),  # type: ignore[arg-type]
        TransferStageState(
            target_exists=True,
            stage_external_location="s3://bucket/stage/shared/",
        ),
        "hive.parquet_stage.shared",
        queued,
        SimpleNamespace(),
        writer_index=1,
        insert_retry_cnt=2,
        cancellation=threading.Event(),
        insert_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("VALUES insert must not run")
        ),
        committed_rows_getter=lambda: 0,
    )

    assert result == 2
    assert writes[0]["worker_id"] == 1
    assert writes[0]["slice_index"] == 3
    assert writes[0]["file_index"] == 5
    assert writes[0]["start_ordinal"] == 11
    assert writes[0]["stop_ordinal"] == 13


def test_write_source_staged_batch_uploads_parquet(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        staged_target,
        "ensure_parquet_staging_dependencies",
        lambda: ("pa", "pq", "fsspec"),
    )

    def write(batch: RowBatch, **kwargs: Any) -> int:
        captured.update(kwargs)
        return batch.row_count

    monkeypatch.setattr(staged_target, "write_batch_to_parquet_stage", write)
    state = TransferStageState(
        target_exists=True,
        stage_external_location="s3://bucket/stage/shared/",
    )
    batch = RowBatch(["id"], [(1,), (2,)])

    assert (
        staged_target.write_source_staged_batch(
            _options(),
            {},
            state,
            "hive.parquet_stage.shared",
            batch,
            worker_id=2,
            slice_index=3,
            file_index=4,
            start_ordinal=10,
            stop_ordinal=12,
            insert_retry_cnt=1,
        )
        == 2
    )
    assert captured["stage_external_location"] == "s3://bucket/stage/shared/"
    assert captured["worker_id"] == 2
    assert captured["slice_index"] == 3

    with pytest.raises(RuntimeError, match="external location"):
        staged_target.write_source_staged_batch(
            _options(),
            {},
            TransferStageState(target_exists=True),
            "hive.parquet_stage.shared",
            batch,
            worker_id=0,
            slice_index=0,
            file_index=0,
            start_ordinal=0,
            stop_ordinal=2,
            insert_retry_cnt=1,
        )


def test_select_unkeyed_parquet_workers_share_one_stage() -> None:
    state = TransferStageState(
        target_exists=True,
        stage_table="hive.parquet_stage.shared",
    )
    worker_tables, stage_tables = staged_target.select_unkeyed_worker_stages(
        _options(),
        {},
        state,
        worker_count=3,
        parquet_target=True,
        replace_target_fn=lambda *_args: (_ for _ in ()).throw(
            AssertionError("Parquet staging must not replace a target connection")
        ),
    )
    assert worker_tables == ["hive.parquet_stage.shared"] * 3
    assert stage_tables == ["hive.parquet_stage.shared"]

    with pytest.raises(RuntimeError, match="shared Parquet target stage"):
        staged_target.select_unkeyed_worker_stages(
            _options(),
            {},
            TransferStageState(target_exists=True),
            worker_count=1,
            parquet_target=True,
        )


def test_prepare_keyed_target_stage_registers_shared_parquet_stage(monkeypatch: Any) -> None:
    state = TransferStageState(target_exists=True)
    registrations: list[tuple[Any, ...]] = []
    runtime = SimpleNamespace(
        register_target_stage_candidate=lambda table: registrations.append(("candidate", table)),
        register_target_stage=lambda index, table: registrations.append(("stage", index, table)),
    )
    progress = SimpleNamespace(
        set_primary_writer=lambda index: registrations.append(("primary", index))
    )

    def prepare(_options: Any, _refs: Any, stage_state: TransferStageState) -> bool:
        stage_state.stage_table = "hive.parquet_stage.shared"
        return True

    monkeypatch.setattr(staged_keyed_io, "prepare_shared_parquet_stage", prepare)
    staged_keyed_io.prepare_keyed_target_stage(
        _options(),
        TransferConnectionRefs(),
        state,
        runtime,
        progress,
    )

    assert registrations == [
        ("candidate", "hive.parquet_stage.shared"),
        ("stage", 0, "hive.parquet_stage.shared"),
        ("primary", 0),
    ]

    monkeypatch.setattr(
        staged_keyed_io,
        "prepare_shared_parquet_stage",
        lambda *_args: True,
    )
    with pytest.raises(RuntimeError, match="shared Parquet target stage"):
        staged_keyed_io.prepare_keyed_target_stage(
            _options(),
            TransferConnectionRefs(),
            TransferStageState(target_exists=True),
            runtime,
            progress,
        )


def test_values_mode_does_not_prepare_parquet_stage(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        staged_target,
        "ensure_parquet_staging_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("Parquet setup must not run")),
    )
    assert not staged_target.prepare_shared_parquet_stage(
        _options("values"),
        TransferConnectionRefs(target={"connection": object()}),
        TransferStageState(target_exists=True),
    )


def test_transfer_parquet_dry_run_does_not_inherit_sql_staging_properties() -> None:
    options = replace(
        _options(),
        staging_ddl_properties={"compression_codec": "'ZSTD'"},
        parquet_ddl_properties={"parquet_marker": 7},
    )
    plan = SqlPlan(operation="transfer")

    dry_run._add_target_stage_templates(
        plan,
        options,
        ["hive.parquet_stage.shared"],
        "s3://bucket/stage/shared/",
        lazy=False,
    )

    assert "compression_codec" not in plan.sqls[0]
    assert "parquet_marker = 7" in plan.sqls[0]
