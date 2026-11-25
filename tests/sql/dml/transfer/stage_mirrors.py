"""Every acknowledged remote stage and coordinator mirror has matching cleanup."""

from __future__ import annotations

import struct

import pytest
from analytics_toolkit import sql

from tests.sql._support.transfer_stage_mirrors import mirror_transfer_target


@pytest.mark.parametrize("transport", ["native", "http"])
def test_stage_mirrors_public_append_cleans_all_four_owned_resources(
    monkeypatch, write_sql_connections, transport
):
    source, state = mirror_transfer_target(monkeypatch, write_sql_connections, transport)
    before = state.snapshot()
    sentinel = before[state.coordinator]["analytics.events_shard"]

    result = sql.transfer(
        "gp",
        "ch",
        from_sql="SELECT id FROM db.source",
        to_table="analytics.events",
        table_schema={"id": "UInt64"},
        write_mode="append",
        ignore_source_staging=True,
        validate_row_count=True,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
        return_metadata=True,
        ch_ddl_ready_timeout_seconds=0.01,
    )

    assert result.rows == 2
    assert state.rows(state.coordinator, "analytics.events") == [
        (11,),
        (12,),
        (13,),
        (201,),
        (202,),
    ]
    assert state.rows(state.remote, "analytics.events_shard") == [
        (11,),
        (12,),
        (13,),
        (201,),
        (202,),
    ]
    assert state.client_rows == [[(201,), (202,)]]
    assert state.client_wire == [struct.pack("<2Q", 201, 202)]
    assert list(source.tables["db.source"].itertuples(index=False, name=None)) == [(201,), (202,)]
    assert len(state.created) == 4
    created = {
        host: {name for endpoint, name in state.created if endpoint == host}
        for host in (state.coordinator, state.remote)
    }
    assert len(created[state.coordinator]) == len(created[state.remote]) == 2
    assert created[state.coordinator] == created[state.remote]
    after = state.snapshot()
    assert all(set(after[host]) == set(before[host]) for host in before)
    assert all(name not in after[host] for host, name in state.created)
    assert after[state.coordinator]["analytics.events_shard"] == sentinel
    assert len(state.rows(state.coordinator, "analytics.events_shard")) == 100
    assert (
        after[state.coordinator]["analytics.events"]
        == before[state.coordinator]["analytics.events"]
    )
    assert (
        after[state.remote]["analytics.events_shard"]["uuid"]
        == before[state.remote]["analytics.events_shard"]["uuid"]
    )
    assert state.opened
    assert all(client.closed and client.close_calls == 1 for client in state.opened)
