"""Partial worker CREATE acknowledgements remain cleanup-owned (audit 40)."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.dml.load.stage import create_stage_table
from analytics_toolkit.sql.dml.transfer.flow import stage_ownership
from analytics_toolkit.sql.dml.transfer.flow.staged_target import create_sql_worker_stages
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferStageState

from tests.sql._support.row_batches import finalize_module, make_progress_options
from tests.sql._support.stage_ownership_native import (
    create_native_owned_stage,
    native_stage_target,
)


def test_keyed_worker_readiness_failure_cleans_acknowledged_stage_before_return():
    options = make_progress_options(
        to_db_key="gp",
        target_table="sandbox.events",
        transfer_staging_schema="sandbox",
        transfer_id="a" * 32,
        destination_hash="0123456789abcdef",
    )
    state = TransferStageState(
        target_exists=True, source_columns=["payload"], stage_column_types={"payload": "BIGINT"}
    )
    database, connection = native_stage_target()
    original_target = database.target.rows.copy()

    def create(*args, **kwargs):
        create_stage_table(*args, **kwargs)
        message = "worker readiness failed"
        raise SqlTableReadinessError(message)

    with pytest.raises(SqlTableReadinessError):
        create_sql_worker_stages(
            options, {"connection": connection}, state, worker_count=1, create_fn=create
        )
    assert not state.stage_table_created
    assert len(state.owned_stage_scoped_resources) == 1
    resource, logical_stage = state.owned_stage_scoped_resources[0]
    assert resource.certificate is not None
    assert state.owned_stage_resources == [(resource.name, logical_stage)]
    assert database.identity_from_name(resource.name) in database.tables
    finalize_module.cleanup_stage(
        options,
        object(),
        state,
        1,
        target_connection_runner=lambda _role, operation: operation({"connection": connection}),
    )
    assert database.identity_from_name(resource.name) not in database.tables
    assert database.drop_attempts == [database.identity_from_name(resource.name)]
    assert any(statement.startswith("LOCK TABLE ") for statement in database.commands)
    assert database.target.rows == original_target
    connection.close()
    assert database.closed == [connection]


def test_owned_cleanup_attempts_all_distinct_resources_and_preserves_first_error():
    state = TransferStageState(target_exists=True)
    database, connection = native_stage_target()
    options = make_progress_options(to_db_key="gp")
    for table in ("sandbox.a", "sandbox.b"):
        create_native_owned_stage(connection, state, table)
    state.owned_stage_scoped_resources.append(state.owned_stage_scoped_resources[-1])
    primary = RuntimeError("first cleanup failure")
    database.drop_failures = {
        ("sandbox", "b"): primary,
        ("sandbox", "a"): ValueError("later cleanup failure"),
    }

    with pytest.raises(RuntimeError) as error:
        stage_ownership.cleanup_acknowledged_stages(
            options, state, lambda operation: operation({"connection": connection})
        )
    assert error.value is primary
    assert database.drop_attempts == [("sandbox", "b"), ("sandbox", "a")]
    assert all(
        resource.certificate is not None for resource, _stage in state.owned_stage_scoped_resources
    )
    assert ("sandbox", "a") in database.tables
    assert ("sandbox", "b") in database.tables
    assert sum(statement.startswith("LOCK TABLE ") for statement in database.commands) == 2
    connection.close()
    assert database.closed == [connection]
