from __future__ import annotations

from tests.sql._support.row_batches import (
    SimpleNamespace,
    dry_run_module,
    keys_module,
    make_progress_options,
    models_module,
    pytest,
    row_counts_module,
    transfer_logging_module,
)


def test_remaining_key_dry_run_logging_and_row_count_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keys_module, "product", lambda *_a: iter(()))
    with pytest.raises(ValueError, match="at least one slice"):
        keys_module.normalize_transfer_slices(
            source_sql="select * from source where {id}",
            transfer_keys="id",
            transfer_key_values=[1],
            concurrency=1,
        )
    with pytest.raises(ValueError, match="at least one placeholder"):
        keys_module.normalize_transfer_keys({})
    key = keys_module.TransferKey("id", "id")
    with pytest.raises(ValueError, match="counts must match"):
        keys_module.render_transfer_slice_source_sql(
            "select * from source where {id}", transfer_keys=[key], values=()
        )
    assert keys_module.render_transfer_literal(None) == "NULL"
    with pytest.raises(ValueError, match="float values must be finite"):
        keys_module.render_transfer_literal(float("inf"))

    no_location = make_progress_options(s3_transfer_staging_location=None)
    assert dry_run_module.dry_run_stage_external_location(no_location) is None
    empty_values = models_module.TransferSlice(0, (), "", "select 1", "slice-0")
    assert (
        transfer_logging_module.format_transfer_slice_log_label(
            make_progress_options(transfer_keys=["id"]), empty_values
        )
        is None
    )

    options = make_progress_options(validate_row_count=True)
    state = models_module.TransferStageState(target_exists=False)
    state.worker_stage_states = [
        SimpleNamespace(stage_state=SimpleNamespace(expected_source_rows=2, slice_counts=[]))
    ]
    with pytest.raises(row_counts_module.TransferRowCountMismatchError):
        row_counts_module.validate_streamed_row_count(
            options=options, stage_state=state, total_rows=1
        )
    state = models_module.TransferStageState(target_exists=False)
    with pytest.raises(RuntimeError, match="source row count"):
        row_counts_module.validate_loaded_stage_row_count(
            options=options,
            connection_refs=models_module.TransferConnectionRefs(),
            stage_state=state,
            total_rows=0,
            open_connection=lambda _key: object(),
        )
    assert row_counts_module._collect_worker_slice_counts(state) == []
