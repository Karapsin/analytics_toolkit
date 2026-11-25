from __future__ import annotations

from dataclasses import replace

import pytest
from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint
from analytics_toolkit.sql.dml.transfer.flow import finalize, staged_keyed_io
from analytics_toolkit.sql.dml.transfer.flow.target_ownership import target_creation_callbacks
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferConnectionRefs

from tests.sql._support.staged_keyed import _metadata, _state
from tests.sql._support.transfer_ownership import acknowledge_stage
from tests.sql.dml.transfer.finalization.retry import _options, _stage_state


def test_partition_finalize_runs_against_the_leased_target_connection(monkeypatch):
    options = replace(
        _options(write_mode="upsert"), upsert_partition_column="id", key_columns=["id"]
    )
    state = _stage_state(target_exists=True)
    connection = object()
    calls = []
    monkeypatch.setattr(
        finalize,
        "finalize_partition_upsert",
        lambda current, current_state, target: calls.append((current, current_state, target)),
    )
    finalize._finalize_target_once(
        options,
        state,
        state.stage_table,
        state.stage_column_types,
        target_precleared=False,
        fresh_clickhouse_target=False,
        target_connection_runner=lambda _role, operation: operation({"connection": connection}),
    )
    assert calls == [(options, state, connection)]


@pytest.mark.parametrize("structural", [False, True])
def test_uncertain_transfer_cleanup_preserves_all_recovery_resources(monkeypatch, structural):
    state = _stage_state(target_exists=True)
    state.upsert_checkpoint = UpsertCheckpoint(prepared=True)
    monkeypatch.setattr(finalize, "preserve_recovery_artifacts", lambda: structural)
    monkeypatch.setattr(
        finalize,
        "cleanup_stage_table_with_retry",
        lambda *_a, **_k: pytest.fail("recovery deleted"),
    )
    finalize.cleanup_stage(_options(), TransferConnectionRefs(), state, 1, drop_created_target=True)
    assert state.stage_table_created is True
    if structural:
        assert (
            finalize.cleanup_transfer_attempt_stages(
                _options(),
                TransferConnectionRefs(),
                state,
                1,
                None,
                lambda **_k: pytest.fail("structural recovery deleted"),
            )
            is None
        )


@pytest.mark.parametrize("storage_options", [None, {"endpoint_url": "http://run-owned.invalid"}])
def test_owned_cleanup_failure_preserves_first_error_and_retains_remote_data(
    monkeypatch, storage_options
):
    state = _stage_state(target_exists=True)
    acknowledge_stage(state, "sandbox.stage", _options().to_db_backend)
    state.stage_external_location = "s3://run-owned/stage"
    message = "owned stage drop failed"
    failure = RuntimeError(message)
    cleaned = []

    def fail_owned(*_args):
        raise failure

    monkeypatch.setattr(finalize, "cleanup_acknowledged_stages", fail_owned)
    monkeypatch.setattr(
        finalize,
        "cleanup_parquet_stage_location",
        lambda location, **kwargs: cleaned.append((location, kwargs)),
    )
    with pytest.raises(RuntimeError) as raised:
        finalize.cleanup_stage(
            replace(_options(), parquet_storage_options=storage_options),
            TransferConnectionRefs(),
            state,
            1,
        )
    assert raised.value is failure
    assert cleaned == []


def test_fresh_clickhouse_creation_policy_uses_configured_retry_extension(monkeypatch):
    options = replace(_options(), timeout_increment=17)
    state = _stage_state(target_exists=False)
    calls = []
    ensured = []

    def ensure(current, _refs, current_state, *_args, **_kwargs):
        ensured.append(current.regular_ch_policy)
        target_creation_callbacks(current_state)["on_resource_created"](current.target_table)

    monkeypatch.setattr(finalize, "ensure_transfer_target_table", ensure)
    monkeypatch.setattr(
        finalize, "finalize_stage_table", lambda *_a, **kwargs: calls.append(kwargs)
    )
    finalize._finalize_target_once(
        options,
        state,
        state.stage_table,
        state.stage_column_types,
        target_precleared=False,
        fresh_clickhouse_target=True,
        target_connection_runner=lambda _role, operation: operation({"connection": object()}),
    )
    assert calls[0]["ch_creation_policy"].ddl_ready_timeout_increment_seconds == 17
    assert ensured[0].ddl_ready_timeout_increment_seconds == 17
    assert options.regular_ch_policy.ddl_ready_timeout_increment_seconds != 17
    assert state.target_created_by_operation is True


@pytest.mark.parametrize(
    ("track_ownership", "observe"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_keyed_stage_candidate_reaches_both_visibility_and_acknowledgement_observers(
    monkeypatch, track_ownership, observe
):
    state = _state()
    visible = []

    def create(*_args, **kwargs):
        kwargs["on_stage_candidate"]("sandbox.writer")
        if "on_resource_created" in kwargs:
            kwargs["on_resource_created"]("sandbox.writer")
        return "sandbox.writer"

    monkeypatch.setattr(staged_keyed_io, "create_stage_table", create)
    name = staged_keyed_io.create_target_writer_stage(
        _options(),
        {"connection": object()},
        _metadata(),
        0,
        ownership_state=state if track_ownership else None,
        on_stage_candidate=visible.append if observe else None,
    )
    assert name == "sandbox.writer"
    assert visible == (["sandbox.writer"] if observe else [])
    assert state.stage_table_candidates == (["sandbox.writer"] if track_ownership else [])
    assert state.owned_stage_resources == (
        [("sandbox.writer", "sandbox.writer")] if track_ownership else []
    )
