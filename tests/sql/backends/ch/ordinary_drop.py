"""Ordinary database tables remain explicitly droppable without native UUIDs."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _ordinary_tables(cluster):
    cluster.database_engine = "Ordinary"
    for table in cluster.tables.values():
        table.uuid = _ZERO_UUID


def _opened_connections(monkeypatch, cluster):
    opened = []

    def open_connection(config, **kwargs):
        connection = cluster.open(config, **kwargs)
        raw = connection._client if getattr(connection, "_ch_outage_client", False) else connection
        raw.close_calls = 0
        close = raw.close

        def close_connection():
            raw.close_calls += 1
            close()

        monkeypatch.setattr(raw, "close", close_connection)
        opened.append(raw)
        return connection

    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", open_connection)
    return opened


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_ordinary_local_drop_keeps_independent_remote_homonym(monkeypatch, outage, driver):
    cluster, configure = outage
    configure(driver=driver, cluster_routing=None)
    for index, host in enumerate(cluster.hosts, 1):
        cluster.create(
            host,
            "CREATE TABLE db.ordinary_table (id UInt64) ENGINE=MergeTree ORDER BY id",
        )
        cluster.tables[(host, "db.ordinary_table")].rows = [(index,)]
    _ordinary_tables(cluster)
    before = deepcopy(cluster.tables)
    opened = _opened_connections(monkeypatch, cluster)

    assert (
        sql.drop_tables(
            "ch", "db.ordinary_table", ch_cluster=None, ch_drop_shard=False, if_exists=True
        )
        is None
    )

    expected = deepcopy(before)
    expected.pop(("h1", "db.ordinary_table"))
    assert cluster.tables == expected
    assert len(cluster.commands) == 1
    statement = parse_one(strip_query_label(cluster.commands[0][1]), read=ClickHouseDialect)
    assert isinstance(statement, exp.Drop)
    assert statement.find(exp.OnCluster) is None
    assert opened
    assert all(connection.close_calls == 1 for connection in opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_ordinary_pair_drop_preserves_unrelated_resources(monkeypatch, outage, driver):
    cluster, configure = outage
    configure(driver=driver)
    cluster.create(
        "h1", "CREATE TABLE db.events_shard_shadow (id UInt64) ENGINE=MergeTree ORDER BY id"
    )
    cluster.tables[("h1", "db.events_shard_shadow")].rows = [(value,) for value in range(100)]
    _ordinary_tables(cluster)
    before = deepcopy(cluster.tables)
    opened = _opened_connections(monkeypatch, cluster)

    assert sql.drop_tables("ch", "db.events", ch_cluster="core", if_exists=True) is None

    expected = deepcopy(before)
    for host in cluster.hosts:
        expected.pop((host, "db.events"))
        expected.pop((host, "db.events_shard"))
    assert cluster.tables == expected
    statements = [
        parse_one(strip_query_label(statement), read=ClickHouseDialect)
        for _host, statement, _settings in cluster.commands
    ]
    assert len(statements) == 2
    assert all(isinstance(statement, exp.Drop) for statement in statements)
    assert all(statement.find(exp.OnCluster) is not None for statement in statements)
    assert opened
    assert all(connection.close_calls == 1 for connection in opened)
