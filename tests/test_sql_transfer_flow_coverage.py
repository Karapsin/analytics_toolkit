from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.dml.transfer.flow import (
    dry_run,
    row_counts,
    stage_validation,
    staged_attempt,
    superseded,
)
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    freeze_attempt_metadata,
)
from analytics_toolkit.sql.dml.transfer.flow.source_snapshot import OrdinalRange
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_internal_columns
from analytics_toolkit.sql.dml.transfer.runtime import retry
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch


def test_unkeyed_cleanup_attempts_every_resource_and_preserves_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_error = OSError("target coordinator close failed")
    close_roles: list[str] = []

    def close_ref(_ref: Any, _key: str, role: str) -> None:
        close_roles.append(role)
        if role == "target coordinator":
            raise first_error
        if role in {"source", "target"}:
            message = f"{role} close failed"
            raise OSError(message)

    opened_source = object()
    dropped: list[tuple[Any, str]] = []
    monkeypatch.setattr(staged_attempt, "close_connection_ref", close_ref)
    monkeypatch.setattr(staged_attempt, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: opened_source)
    monkeypatch.setattr(
        staged_attempt,
        "cleanup_stage_table",
        lambda _backend, connection, table, **_kwargs: dropped.append((connection, table)),
    )
    options = SimpleNamespace(
        to_db_key="target",
        from_db_key="source",
        from_db_backend="gp",
        retry_cnt=1,
        query_label=None,
    )
    source_ref: dict[str, Any] = {}
    target_ref = {"connection": object()}

    with pytest.raises(OSError, match="target coordinator close failed") as captured:
        staged_attempt._cleanup_unkeyed_attempt(
            options,
            SimpleNamespace(),
            SimpleNamespace(),
            source_ref,
            target_ref,
            snapshot_table="source.snapshot",
            error=RuntimeError("transfer failed"),
        )

    assert captured.value is first_error
    assert source_ref["connection"] is opened_source
    assert dropped == [(opened_source, "source.snapshot")]
    assert close_roles == ["target coordinator", "source", "target"]


def test_unkeyed_attempt_summary_tolerates_exception_without_writable_dict() -> None:
    class LockedError(Exception):
        @property
        def __dict__(self) -> dict[str, Any]:
            message = "exception metadata locked"
            raise AttributeError(message)

    staged_attempt._attach_unkeyed_attempt_failure(
        LockedError(),
        None,
        phase="metadata inspection",
        attempt_started_at=0.0,
    )


def test_unkeyed_insert_without_progress_uses_plain_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    retry_calls: list[dict[str, Any]] = []

    def run_retry(**kwargs: Any) -> int:
        retry_calls.append(kwargs)
        return 1

    def insert_rows(*_args: Any, retry_fn: Any, **_kwargs: Any) -> None:
        assert retry_fn(operation_name="insert", retry_cnt=1, operation=lambda _attempt: 1) == 1

    monkeypatch.setattr(staged_attempt, "run_with_retry", run_retry)
    monkeypatch.setattr(staged_attempt, "insert_rows_batch", insert_rows)
    options = SimpleNamespace(
        to_db_backend="gp",
        timeout_increment=0,
        query_label=None,
        to_db_key="target",
    )

    staged_attempt._insert_unkeyed_range_batch(
        options,
        {"connection": object()},
        SimpleNamespace(stage_column_types={"id": "BIGINT"}),
        "target.stage",
        RowBatch(["id"], [(1,)]),
        (0, 1, 2),
        insert_retry_cnt=1,
        transfer_progress=None,
    )

    assert retry_calls
    assert "log_prefix" not in retry_calls[0]


def _snapshot_options() -> Any:
    return SimpleNamespace(
        from_db_backend="gp",
        transfer_id="transfer",
        canonical_destination_identity="target",
    )


def _snapshot_state() -> Any:
    return SimpleNamespace(
        internal_columns=resolve_internal_columns(["id"], "gp"),
        source_column_types={"id": "bigint"},
    )


def test_unkeyed_snapshot_read_rejects_invalid_raw_and_normalized_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="internal columns"):
        staged_attempt._read_snapshot_range(
            _snapshot_options(),
            object(),
            "source.snapshot",
            ["id"],
            SimpleNamespace(internal_columns=None),
            OrdinalRange(0, 1, 2),
        )

    state = _snapshot_state()
    monkeypatch.setattr(
        staged_attempt,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(
            column_names=["id", "other"],
            columns=[[1], [1, 2]],
        ),
    )
    with pytest.raises(RuntimeError, match="unequal lengths"):
        staged_attempt._read_snapshot_range(
            _snapshot_options(),
            object(),
            "source.snapshot",
            ["id"],
            state,
            OrdinalRange(0, 1, 2),
        )

    monkeypatch.setattr(
        staged_attempt,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(
            column_names=["id"],
            columns=[[1, 2]],
        ),
    )
    with pytest.raises(RuntimeError, match="scheduled limit is 1"):
        staged_attempt._read_snapshot_range(
            _snapshot_options(),
            object(),
            "source.snapshot",
            ["id"],
            state,
            OrdinalRange(0, 1, 2),
        )

    monkeypatch.setattr(
        staged_attempt,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(
            column_names=["id"],
            columns=[[1]],
        ),
    )
    monkeypatch.setattr(
        staged_attempt,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            normalize_transfer_source_batch=lambda *_args: RowBatch(["id"], [(1,), (2,)])
        ),
    )
    with pytest.raises(RuntimeError, match="Normalized source batch"):
        staged_attempt._read_snapshot_range(
            _snapshot_options(),
            object(),
            "source.snapshot",
            ["id"],
            state,
            OrdinalRange(0, 1, 2),
        )


def test_best_effort_target_count_runner_failure_is_none() -> None:
    options = SimpleNamespace(
        to_db_backend="gp",
        target_table="target.table",
        query_label=None,
    )

    def fail_runner(_role: str, _operation: Any) -> None:
        message = "runner failed"
        raise OSError(message)

    assert (
        row_counts.best_effort_transfer_target_count(
            options,
            target_connection_runner=fail_runner,
        )
        is None
    )


def test_stage_slice_validation_rejects_prequery_contract_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal = resolve_internal_columns(["id"], "gp")
    options = SimpleNamespace(
        transfer_id="transfer",
        canonical_destination_identity="target",
        to_db_backend="gp",
    )
    with pytest.raises(RuntimeError, match="streamed 1 row"):
        stage_validation.validate_transfer_stage_slice(
            options=options,
            connection=object(),
            stage_table="target.stage",
            internal_columns=internal,
            slice_id=0,
            expected_count=2,
            streamed_count=1,
        )
    with pytest.raises(RuntimeError, match="has no target stage"):
        stage_validation.validate_transfer_stage_slice(
            options=options,
            connection=object(),
            stage_table=None,
            internal_columns=internal,
            slice_id=0,
            expected_count=1,
            streamed_count=1,
        )
    with pytest.raises(RuntimeError, match="identity was not initialized"):
        stage_validation.validate_transfer_stage_slice(
            options=SimpleNamespace(
                transfer_id=None,
                canonical_destination_identity="target",
            ),
            connection=object(),
            stage_table="target.stage",
            internal_columns=internal,
            slice_id=0,
            expected_count=0,
            streamed_count=0,
        )

    monkeypatch.setattr(stage_validation, "_rows", lambda *_args, **_kwargs: [(1,)])
    with pytest.raises(RuntimeError, match="empty validation failed"):
        stage_validation.validate_transfer_stage_slice(
            options=options,
            connection=object(),
            stage_table=None,
            internal_columns=internal,
            slice_id=0,
            expected_count=0,
            streamed_count=0,
        )


def test_superseded_identity_empty_and_ambiguous_contracts() -> None:
    options = SimpleNamespace(
        transfer_id="current",
        canonical_destination_identity="target",
    )
    assert superseded._is_superseded_stage_identity(
        [],
        "current",
        options,
        include_current_transfer_id=True,
    )
    assert not superseded._is_superseded_stage_identity(
        [("one", "target"), ("two", "target")],
        "old",
        options,
        include_current_transfer_id=False,
    )


def test_retry_safe_logging_and_close_failure_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    long_named_error = type("X" * 100, (Exception,), {})
    assert retry._logged_exception(long_named_error(), safe=True).endswith("...")

    monkeypatch.setattr(retry, "close_connection_ref", lambda *_args: None)
    retry.close_connection_refs_preserving(None, ({}, "gp", "source"))

    close_error = OSError("close failed")

    def fail_close(*_args: Any) -> None:
        raise close_error

    monkeypatch.setattr(retry, "close_connection_ref", fail_close)
    with pytest.raises(OSError, match="close failed") as captured:
        retry.close_connection_refs_preserving(None, ({}, "gp", "source"))
    assert captured.value is close_error

    class LockedError(Exception):
        @property
        def __dict__(self) -> dict[str, Any]:
            message = "exception metadata locked"
            raise AttributeError(message)

    with pytest.raises(OSError, match="close failed"):
        retry.close_connection_refs_preserving(
            LockedError(),
            ({}, "gp", "source"),
        )


def test_lazy_dry_run_source_batch_labels_cover_unassigned_and_writer_paths() -> None:
    options = SimpleNamespace(
        transfer_slices=[SimpleNamespace(index=0), SimpleNamespace(index=1)],
    )
    monkey_options = SimpleNamespace(**vars(options))
    monkey_options.source_transfer_staging_schema = "stage"
    monkey_options.transfer_concurrency = SimpleNamespace(effective_write=2)
    assert (
        dry_run.source_batches_label(monkey_options)
        == "dynamically scheduled ready whole-key batches"
    )
    assert dry_run.source_batches_label(monkey_options, 1) == (
        "writer 1 dynamically claimed ready whole-key batches"
    )


def test_frozen_metadata_is_immutable() -> None:
    internal = resolve_internal_columns(["id"], "gp")
    metadata = freeze_attempt_metadata(
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
        internal_columns=internal,
    )
    with pytest.raises(TypeError):
        metadata.source_column_types["id"] = "text"  # type: ignore[index]
