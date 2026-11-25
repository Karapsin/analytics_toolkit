"""Supported layouts keep their identity throughout public lifecycle operations."""

from __future__ import annotations

import json
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.connection.config import get_connections_file_path

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_memory_pair_creation_truncation_and_drop_keep_other_storage(
    monkeypatch, write_sql_connections, driver
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state.clusters["storage"] = (state.coordinator,)
    before = state.snapshot()
    table = "analytics.memory_events"
    physical = "analytics.memory_events_shard"
    assert (
        sql.create_table(
            "ch",
            table,
            table_schema={"id": "UInt64"},
            ch_engine="Memory",
            ch_distributed_table=True,
            retry_cnt=1,
            timeout_increment=0,
        )
        is None
    )
    created = state.snapshot()[state.coordinator]
    assert state.host_tables[state.coordinator][physical].engine == "Memory"
    incoming = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    original = incoming.copy(deep=True)

    assert (
        sql.load_df(
            "ch", table, incoming, write_mode="truncate_insert", retry_cnt=1, timeout_increment=0
        )
        == 2
    )

    assert state.rows(state.coordinator, table) == [(201,), (202,)]
    current = state.snapshot()[state.coordinator]
    for name in (table, physical):
        assert current[name]["uuid"] == created[name]["uuid"]
        assert current[name]["ddl"] == created[name]["ddl"]
    assert state.snapshot()[state.remote] == before[state.remote]
    pd.testing.assert_frame_equal(incoming, original)
    assert sql.drop_tables("ch", table, ch_cluster="storage", if_exists=False) is None
    assert state.snapshot() == before
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_public_unqualified_existing_layout_uses_effective_coordinator_database(
    monkeypatch, write_sql_connections, driver, explicit
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    path = get_connections_file_path()
    connections = json.loads(path.read_text())
    connections["ch"].pop("database")
    path.write_text(json.dumps(connections))
    set_connections_path(path)
    before = state.snapshot()
    incoming = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    original = incoming.copy(deep=True)
    options = {"table_schema": {"id": "UInt64"}} if explicit else {}

    assert (
        sql.load_df(
            "ch",
            "events",
            incoming,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )
        == 2
    )

    expected = deepcopy(before)
    expected[state.remote]["analytics.events_shard"]["rows"] = [(201,), (202,)]
    assert state.snapshot() == expected
    assert state.rows(state.coordinator, "analytics.events") == [(201,), (202,)]
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(incoming, original)
