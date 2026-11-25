"""A routed read scope does not become coordinator-only facade ownership."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.ch_lifecycle import install_lifecycle
from tests.sql._support.reconfigure_conversion_scope import _name


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("staging_cluster", [None, "storage"], ids=["local-stage", "remote-stage"])
def test_public_replacement_keeps_the_actual_coordinator_only_facade_scope(
    monkeypatch, write_sql_connections, driver, staging_cluster
):
    def configure(values):
        values["ch"]["ddl_ready_timeout_seconds"] = 1
        return write_sql_connections(values)

    state = install_lifecycle(
        monkeypatch,
        configure,
        driver=driver,
        cluster_routing={"cluster": "storage"},
        staging_cluster=staging_cluster,
    )
    original = state.snapshot()
    frame = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    caller = frame.copy(deep=True)

    result = sql.load_df(
        "ch",
        "analytics.events",
        frame,
        table_schema={"id": "UInt64"},
        write_mode="replace",
        ch_distributed_on_cluster=None,
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    assert isinstance(result.metadata.stage_table, str)
    stage = _name(result.metadata.stage_table)
    assert {host for host, table in state.created if table == stage} == (
        {state.coordinator, state.remote} if staging_cluster else {state.coordinator}
    )
    assert state.rows(state.remote, "analytics.events_shard") == [(201,), (202,)]
    assert state.rows(state.coordinator, "analytics.events") == [(201,), (202,)]
    current = state.snapshot()
    assert (
        current[state.coordinator]["analytics.events_shard"]
        == original[state.coordinator]["analytics.events_shard"]
    )
    assert set(current[state.coordinator]) == {"analytics.events", "analytics.events_shard"}
    assert set(current[state.remote]) == {"analytics.events_shard"}
    assert state.host_tables[state.coordinator]["analytics.events"].route == (
        "storage",
        "analytics.events_shard",
    )
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(frame, caller)
