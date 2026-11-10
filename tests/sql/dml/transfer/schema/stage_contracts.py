from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.ddl.schema import validate_table_schema_columns
from analytics_toolkit.sql.dml.transfer.flow import attempt, stage, staged_keyed_pipeline
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import LazyKeyedRuntime
from analytics_toolkit.sql.dml.transfer.io import source
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)


def options(**overrides):
    return TransferOptions("gp", "gp", "gp", "gp", "SELECT id", "sandbox.target", **overrides)


def test_prevalidated_keyed_schema_survives_initialization_without_remapping(monkeypatch):
    selected = {"id": "NUMERIC(20, 0)"}
    state = TransferStageState(False, stage_column_types=selected, input_types_resolved=True)
    monkeypatch.setattr(attempt, "ensure_transfer_target_table", lambda *_a: None)
    monkeypatch.setattr(attempt, "create_stage_table", lambda **_k: "sandbox.stage")
    monkeypatch.setattr(
        attempt,
        "map_source_schema_to_target",
        lambda *_a, **_k: pytest.fail("certified type remapped"),
    )
    attempt.initialize_shared_stage_for_keyed_slices(
        options=options(),
        connection_refs=TransferConnectionRefs(target={"connection": object()}),
        stage_state=state,
        source_schema=[SourceColumn("id", "UInt64")],
    )
    assert state.stage_table == "sandbox.stage"
    assert state.stage_column_types["id"] == "NUMERIC(20, 0)"


def test_declared_explicit_stage_schema_has_precedence_and_requires_an_initialized_stage():
    opts = options(table_schema={"id": "SMALLINT"})
    assert stage.select_stage_column_types(
        opts,
        TransferStageState(False),
        [SourceColumn("id", "BIGINT")],
        None,
        validate_table_schema_columns,
    ) == {"id": "SMALLINT"}
    with pytest.raises(RuntimeError, match="initialized before insertion"):
        stage.require_stage_table(TransferStageState(False))


@pytest.mark.parametrize("incoming", [["payload", "id"], ["id"]])
def test_existing_upsert_requires_complete_writable_schema_before_stage_mutation(
    monkeypatch, incoming
):
    adapter = get_backend_adapter("gp")
    monkeypatch.setattr(
        adapter,
        "get_writable_table_column_types",
        lambda *_a, **_k: {"id": "BIGINT", "payload": "TEXT"},
    )
    state = TransferStageState(True)
    opts = options(write_mode="upsert", key_columns=["id"])
    if len(incoming) == 1:
        with pytest.raises(ValueError, match="complete"):
            stage.ensure_transfer_target_table(
                opts, TransferConnectionRefs(target={"connection": object()}), state, incoming
            )
        assert state.insert_column_types is None
    else:
        stage.ensure_transfer_target_table(
            opts, TransferConnectionRefs(target={"connection": object()}), state, incoming
        )
        assert list(state.insert_column_types) == ["id", "payload"]
    assert state.stage_table is None


def test_existing_explicit_schema_metadata_is_kept_for_legacy_staged_replacement(monkeypatch):
    opts = options(
        table_schema={"id": "BIGINT"},
        replace_target_table=False,
        transfer_slices=[TransferSlice(0, (1,), "", "SELECT id", None)],
    )
    state = TransferStageState(True)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "inspect_source_query_schema",
        lambda *_a: [SourceColumn("id", "BIGINT")],
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "cleanup_superseded_transfer_stages", lambda **_k: None
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "get_existing_target_insert_types",
        lambda *_a, **_k: {"id": "BIGINT"},
    )
    staged_keyed_pipeline._prepare_attempt(
        opts,
        TransferConnectionRefs(source={"connection": object()}, target={"connection": object()}),
        state,
    )
    assert state.insert_column_types == {"id": "BIGINT"}


def test_abandoning_owned_or_unknown_stage_does_not_release_a_live_capacity_lease():
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    assert runtime.live_stage_credits.acquire(blocking=False)
    assert runtime.live_stage_credits.acquire(blocking=False)
    runtime.reserve_source_stage("owned")
    runtime.mark_source_stage_owned("owned")
    runtime.abandon_unowned_source_stage("owned")
    runtime.abandon_unowned_source_stage("unknown")
    assert not runtime.live_stage_credits.acquire(blocking=False)
    assert runtime.source_stage_tables == ["owned"]
    runtime.mark_source_stage_dropped("owned")
    assert runtime.live_stage_credits.acquire(blocking=False)


def test_clickhouse_stream_setup_failure_does_not_close_an_unacquired_context():
    primary = RuntimeError("stream codec configuration is invalid")

    def fail(_query):
        raise primary

    connection = SimpleNamespace(query_df_stream=fail)
    batches = source.iter_source_batches(
        "ch", "ch", {"connection": connection}, "SELECT id", 1, 1, 0
    )
    with pytest.raises(RuntimeError, match="codec configuration") as caught:
        next(batches)
    assert caught.value is primary
