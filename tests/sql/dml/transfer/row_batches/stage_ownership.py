"""Partial worker CREATE acknowledgements remain cleanup-owned (audit 40)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.dml.transfer.flow import stage_ownership
from analytics_toolkit.sql.dml.transfer.flow.staged_target import create_sql_worker_stages
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferStageState

from tests.sql._support.fakes import FakeDbapiConnection
from tests.sql._support.row_batches import finalize_module, make_progress_options


def test_keyed_worker_readiness_failure_cleans_acknowledged_stage_before_return(monkeypatch):
    options = make_progress_options()
    state = TransferStageState(
        target_exists=True, source_columns=["id"], stage_column_types={"id": "BIGINT"}
    )
    connection = FakeDbapiConnection()

    def create(*_args, **kwargs):
        kwargs["on_stage_candidate"]("sandbox.partial_worker")
        kwargs["on_resource_created"]("sandbox.partial_worker")
        message = "worker readiness failed"
        raise SqlTableReadinessError(message)

    with pytest.raises(SqlTableReadinessError):
        create_sql_worker_stages(
            options, {"connection": connection}, state, worker_count=1, create_fn=create
        )
    assert not state.stage_table_created
    assert state.owned_stage_resources == [("sandbox.partial_worker", "sandbox.partial_worker")]
    monkeypatch.setattr(
        finalize_module,
        "_run_target_operation",
        lambda _options, _role, operation, **_kwargs: operation({"connection": connection}),
    )
    finalize_module.cleanup_stage(options, object(), state, 1)
    assert connection.executed == ['DROP TABLE IF EXISTS "sandbox"."partial_worker"']


def test_owned_cleanup_attempts_all_distinct_resources_and_preserves_first_error(monkeypatch):
    state = TransferStageState(target_exists=True)
    state.owned_stage_resources = [("a", "stage"), ("b", "stage"), ("b", "stage")]
    options = SimpleNamespace(to_db_backend="gp", staging_ch_policy=None)
    events = []
    primary = RuntimeError("first cleanup failure")

    def drop(_adapter, _connection, resource, **kwargs):
        events.append((resource, kwargs["logical_stage"]))
        raise primary if resource == "b" else ValueError("later cleanup failure")

    monkeypatch.setattr(stage_ownership, "drop_owned_resource", drop)
    with pytest.raises(RuntimeError) as error:
        stage_ownership.cleanup_acknowledged_stages(
            options, state, lambda operation: operation({"connection": object()})
        )
    assert error.value is primary
    assert events == [("b", "stage"), ("a", "stage")]
