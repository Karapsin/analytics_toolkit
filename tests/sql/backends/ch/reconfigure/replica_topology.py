"""Physical shard groups are preserved while replica alias order may change."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.ch_replica_lifecycle import install_replica_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfiguration_rejects_routing_that_splits_one_replication_shard(
    monkeypatch, write_sql_connections, driver
):
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"[Ss]hard|[Gg]roup|[Rr]outing"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            order_by="id DESC",
            ch_distributed_cluster="split",
            ch_distributed_on_cluster="core",
            dry_run=True,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert state.snapshot() == before
    assert state.commands == []
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_replacement_accepts_reversed_replicas_in_an_equivalent_storage_alias(
    monkeypatch, write_sql_connections, driver
):
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    frame = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    original = frame.copy(deep=True)

    result = sql.load_df(
        "ch",
        "analytics.events",
        frame,
        table_schema={"id": "UInt64"},
        write_mode="replace",
        ch_engine="ReplicatedMergeTree('/tables/{table}/{shard}', '{replica}')",
        ch_shard_on_cluster="storage_alias",
        ch_distributed_cluster="storage_alias",
        ch_distributed_on_cluster="core",
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.rows == 2
    assert result.metadata.final_target_rows == 2
    assert result.metadata.source_rows == result.metadata.staged_rows == 2
    for host in state.host_tables:
        assert state.rows(host, "analytics.events") == [(201,), (202,)]
        assert state.host_tables[host]["analytics.events_shard"].rows == [(201,), (202,)]
    assert (
        state.snapshot()[state.coordinator]["analytics.unrelated"]
        == before[state.coordinator]["analytics.unrelated"]
    )
    assert state.replication_pending == {}
    assert not any("__replace_" in name for tables in state.host_tables.values() for name in tables)
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(frame, original)
