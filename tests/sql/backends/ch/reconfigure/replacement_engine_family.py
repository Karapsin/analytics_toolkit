"""Replacement-engine validation follows the documented MergeTree contract."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.observation import observed_connection

from tests.sql._support.reconfigure import (
    LocalReconfigureClient,
    ReconfigureClient,
    reconfigure_api,
)
from tests.sql._support.reconfigure_independent_replicas import configure_independent_replicas


@pytest.mark.parametrize("source", ["local", "replicated-pair"])
@pytest.mark.parametrize(
    "engine",
    ["Memory", "Log", "Distributed('core', 'analytics', 'events_shard', rand())"],
    ids=["memory", "log", "distributed"],
)
def test_physical_replacement_engine_must_be_merge_tree_family_before_dry_plan(
    monkeypatch, write_sql_connections, source, engine
):
    configure_independent_replicas(write_sql_connections)
    client = LocalReconfigureClient() if source == "local" else ReconfigureClient()
    table = "analytics.local_events" if source == "local" else "analytics.events"
    original_definitions = client.metadata_definitions()
    opened = []

    def connect(key):
        assert key == "ch"
        opened.append(client)
        return observed_connection(client)

    monkeypatch.setattr(reconfigure_api, "get_sql_connection", connect)

    try:
        with pytest.raises(InvalidSqlInputError, match="MergeTree-family engine"):
            sql.ch_reconfigure_table(
                "ch",
                table,
                ch_engine=engine,
                dry_run=True,
                retry_cnt=1,
                timeout_increment=0,
            )
    finally:
        assert opened == [client]
        assert client.closed
        assert client.commands == []
        assert client.metadata_definitions() == original_definitions
        assert all(not query.startswith("SELECT count() FROM ") for query in client.queries)
