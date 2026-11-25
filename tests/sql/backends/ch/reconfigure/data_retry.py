"""Losing a data-copy acknowledgement does not prohibit a later structural cutover."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql

from tests.sql._support.ch_recovery_inventory import command_parts, install_recovery


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfigure_retries_a_data_copy_with_lost_acknowledgement(
    monkeypatch, write_sql_connections, driver
):
    state = install_recovery(monkeypatch, write_sql_connections, driver=driver)
    original = state.snapshot()
    failure = ConnectionError("data-copy acknowledgement lost")
    copies = []

    def lose_first_copy_acknowledgement(_connection, statement):
        kind, _scope, _body = command_parts(statement)
        if kind != "INSERT":
            return
        copies.append(statement)
        if len(copies) == 1:
            current = state.snapshot()
            assert {
                host: {name: current[host][name] for name in tables}
                for host, tables in original.items()
            } == original
            raise failure

    state.after_raw_command = lose_first_copy_acknowledgement

    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        order_by="id DESC",
        ch_distributed_table=True,
        ch_shard_on_cluster="storage",
        ch_distributed_cluster="storage",
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows is None
    assert result.metadata.source_rows == result.metadata.staged_rows == 3
    assert result.metadata.final_target_rows == 3
    assert result.metadata.row_count_validated is True
    assert len(copies) == 2
    expected_rows = original[state.remote]["analytics.events_shard"]["rows"]
    assert state.rows(state.remote, "analytics.events_shard") == expected_rows
    assert state.rows(state.coordinator, "analytics.events") == expected_rows
    current = state.snapshot()
    assert (
        current[state.coordinator]["analytics.events_shard"]
        == original[state.coordinator]["analytics.events_shard"]
    )
    assert set(current[state.coordinator]) == {"analytics.events", "analytics.events_shard"}
    assert set(current[state.remote]) == {"analytics.events_shard"}
    assert len(state.opened) >= 2
    assert all(connection.close_calls == 1 for connection in state.opened)
