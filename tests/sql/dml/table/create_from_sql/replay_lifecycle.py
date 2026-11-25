"""Acknowledged data replay releases query input while accepting duplicate effects."""

from __future__ import annotations

import shutil
import struct

import pytest
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.observation import observed_connection

from tests.sql._support.adaptive_transfer import track_spools
from tests.sql._support.query_client_validation import (
    adaptive_input,
    create,
    install_query_client,
    legacy,
    transfer_retry,
)


@pytest.mark.parametrize("transport", ["native", "http"])
def test_public_query_replay_success_releases_checkpoint_after_real_outage_ack_loss(
    monkeypatch, write_sql_connections, transport
):
    client = install_query_client(
        monkeypatch, write_sql_connections, transport, "Int256", [1, 2], exists=True
    )

    def connect(alias):
        config = get_connection_config(alias)
        client.opens.append((alias, config.driver, config.host))
        return OutageClient(observed_connection(client), config)

    for module in (create, legacy, adaptive_input, transfer_retry):
        monkeypatch.setattr(module, "get_sql_connection", connect)
    client.fail_insert_ack = 1
    paths = track_spools(monkeypatch)
    try:
        result = create.create_table_from_sql(
            "ch",
            "analytics.events",
            "SELECT payload FROM analytics.events",
            table_schema={"payload": "Int64"},
            drop_target_if_exists=True,
            retry_cnt=2,
            timeout_increment=0,
            return_metadata=True,
        )

        assert result.rows is None
        assert result.metadata.inserted_rows == 2
        assert result.metadata.data_write_replay_possible is True
        assert client.insert_attempts == 2
        assert client.scans == [True]
        assert client.rows == [{"payload": value} for value in [1, 2, 1, 2]]
        assert client.wire_columns == [("Int64", struct.pack("<2q", 1, 2))] * 2
        assert paths
        assert all(not path.exists() for path in paths)
        assert all(not (path.parent / "manifest.json").exists() for path in paths)
        assert client.close_calls == len(client.opens)
    finally:
        for path in paths:
            shutil.rmtree(path.parent, ignore_errors=True)
