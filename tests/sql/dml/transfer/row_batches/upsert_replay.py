"""Certified payloads replay data writes without restarting preservation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint
from analytics_toolkit.sql.dml.transfer.flow.upsert_finalization import (
    finalize_partition_upsert,
    retry_certified_finalization,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferStageState
from analytics_toolkit.sql.dml.transfer.runtime.retry import run_with_retry

from tests.sql._support.fakes import FakeDbapiConnection
from tests.sql._support.row_batches import make_progress_options


def test_live_partition_finalization_retains_complete_payload_between_attempts(monkeypatch):
    options = make_progress_options(
        to_db_key="trino",
        to_db_backend="trino",
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="month",
        trino_upsert_partition_drop_sql_template=(
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    )
    state = TransferStageState(
        target_exists=True,
        stage_table="sandbox.incoming",
        final_upsert_stage_table="sandbox.payload",
        source_columns=["id", "month"],
        insert_column_types={"id": "BIGINT", "month": "VARCHAR"},
    )
    adapter = get_backend_adapter("trino")
    statements = []
    monkeypatch.setattr(adapter, "execute_command", lambda _connection, sql: statements.append(sql))
    monkeypatch.setattr(adapter, "fetch_upsert_partition_values", lambda *_a, **_k: ["Jan"])
    connection = FakeDbapiConnection()
    finalize_partition_upsert(options, state, connection)
    payload = state.upsert_checkpoint
    assert payload is not None
    assert payload.prepared
    assert payload.target_table == options.target_table
    finalize_partition_upsert(options, state, connection)
    assert state.upsert_checkpoint is payload
    assert sum("FROM sandbox.target AS target_dst" in statement for statement in statements) == 1
    assert sum("DROP PARTITION" in statement for statement in statements) == 2
    assert len(connection.executed) == 2
    assert all("is_nullable = 'NO'" in query for query in connection.executed)


@pytest.mark.parametrize("prepared", [None, False, True])
def test_exhausted_partition_replay_protects_only_certified_target_mutation(prepared):
    options = make_progress_options(retry_cnt=2, timeout_increment=0)
    checkpoint = None if prepared is None else UpsertCheckpoint(prepared=prepared)
    state = SimpleNamespace(upsert_checkpoint=checkpoint)
    attempts = []

    def fail(attempt):
        attempts.append(attempt)
        message = "partition acknowledgement lost"
        raise ConnectionError(message)

    with pytest.raises(ConnectionError) as error:
        retry_certified_finalization(options, state, fail, run_with_retry)
    assert attempts == [1, 2]
    assert getattr(error.value, "analytics_toolkit_sql_retry_safe", None) is (
        False if prepared else None
    )


def test_partition_retry_success_preserves_original_operation_budget():
    options = make_progress_options(retry_cnt=2, timeout_increment=0)
    attempts = []

    def succeed_after_transient_failure(attempt):
        attempts.append(attempt)
        if attempt == 1:
            message = "temporary transport failure"
            raise ConnectionError(message)

    retry_certified_finalization(
        options,
        SimpleNamespace(upsert_checkpoint=None),
        succeed_after_transient_failure,
        run_with_retry,
    )
    assert attempts == [1, 2]
