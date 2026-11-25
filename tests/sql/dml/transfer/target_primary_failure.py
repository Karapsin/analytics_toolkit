"""Cleanup faults cannot replace a staged transfer's native insertion failure."""

from __future__ import annotations

import struct

import pytest
from analytics_toolkit import sql

from tests.sql._support.transfer_target_primary_failure import (
    CLEANUP_SECRET,
    TARGET,
    PrimaryInsertError,
    target_primary_failure,
)


def _assert_retained_generation(state, failure):
    retained = getattr(failure, "analytics_toolkit_sql_retained_target_resources", ())
    assert len(retained) == 1
    assert retained[0].name == TARGET
    assert retained[0].scope.kind == "local"
    assert retained[0].certificate.uuid == state.acknowledged_target_uuid
    assert retained[0].connection_key == "ch"
    assert retained[0].configuration_digest


@pytest.mark.parametrize(
    "case",
    [
        ("drop_failure", "native"),
        ("drop_failure", "http"),
        ("replacement", "native"),
        ("replacement", "http"),
    ],
    ids=["drop_failure-native", "drop_failure-http", "replacement-native", "replacement-http"],
)
def test_target_primary_failure_survives_owned_cleanup_failure(
    monkeypatch, write_sql_connections, capsys, caplog, case
):
    cleanup_mode, transport = case
    source, state = target_primary_failure(
        monkeypatch, write_sql_connections, transport, cleanup_mode
    )
    before = state.snapshot()

    with pytest.raises(PrimaryInsertError) as caught:
        sql.transfer(
            "gp",
            "ch",
            from_sql="SELECT id FROM db.source",
            to_table=TARGET,
            table_schema={"id": "UInt64"},
            write_mode="append",
            ignore_source_staging=True,
            validate_row_count=True,
            retry_cnt=3,
            full_retry_cnt=3,
            timeout_increment=0,
            full_timeout_increment=0,
            ch_ddl_ready_timeout_seconds=0.01,
        )

    assert caught.value is state.primary_error
    _assert_retained_generation(state, caught.value)
    assert len(state.population_attempts) == 1
    assert state.acknowledged_target_uuid is not None
    assert state.source_batches == [[(201,), (202,)]]
    assert state.client_rows == [[(201,), (202,)]]
    assert state.client_wire == [struct.pack("<2Q", 201, 202)]
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == [(201,), (202,)]

    after = state.snapshot()
    for host, resources in before.items():
        for name, original in resources.items():
            assert after[host][name] == original
    target = after[state.coordinator][TARGET]
    if cleanup_mode == "replacement":
        assert state.target_cleanup_attempts == []
        assert target == state.replacement
        assert target["uuid"] != state.acknowledged_target_uuid
        assert target["rows"] == [(value,) for value in range(100)]
    else:
        cleanup_error = getattr(caught.value, "analytics_toolkit_sql_cleanup_error", None)
        cause = cleanup_error
        while cause is not None and cause is not state.cleanup_error:
            cause = cause.__cause__
        if cause is not state.cleanup_error:
            message = "Cleanup did not reach the actual native DROP fault."
            raise AssertionError(message) from cleanup_error
        assert len(state.target_cleanup_attempts) == 1
        assert target["uuid"] == state.acknowledged_target_uuid
        assert target["rows"] == []

    captured = capsys.readouterr()
    diagnostics = (
        captured.out + captured.err + caplog.text + " ".join(getattr(caught.value, "__notes__", ()))
    )
    assert "cleanup" in diagnostics.lower()
    assert TARGET in diagnostics
    assert CLEANUP_SECRET not in diagnostics
    assert state.source_connections
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.source_connections)
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)
