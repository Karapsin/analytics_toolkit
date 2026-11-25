"""Routed truncation uses verified roles without clearing local homonyms."""

from __future__ import annotations

import json
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.config import get_connections_file_path

from tests.sql._support.ch_drop_scope import install_drop_scope
from tests.sql._support.ch_lifecycle import LifecycleConnection


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_public_routed_truncate_keeps_independent_coordinator_table(
    monkeypatch, write_sql_connections, driver, explicit
):
    state = install_drop_scope(
        monkeypatch,
        write_sql_connections,
        driver=driver,
        cluster_routing={"cluster": "storage"},
    )
    state.connection_class = LifecycleConnection
    path = get_connections_file_path()
    config = json.loads(path.read_text())
    config["ch"]["ddl_defaults"]["staging"]["shard"]["engine"] = "MergeTree"
    path.write_text(json.dumps(config))
    set_connections_path(path)
    table = "analytics.events"
    facade = state.host_tables[state.coordinator][table].create.copy()
    state._create(state.remote, facade)
    state.host_tables[state.coordinator].pop(table)
    local = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id", table
    )
    state._create(state.coordinator, local, [(value,) for value in range(100)])
    state.created.clear()
    before = state.snapshot()
    incoming = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    original = incoming.copy(deep=True)
    options = {"table_schema": {"id": "UInt64"}} if explicit else {}

    assert (
        sql.load_df(
            "ch",
            table,
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
    assert state.rows(state.remote, table) == [(201,), (202,)]
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(incoming, original)
