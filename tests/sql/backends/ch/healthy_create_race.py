"""Healthy-host execution preserves the caller's plain CREATE collision contract."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.outage import Client


class _ConcurrentCreateError(RuntimeError):
    code = 57


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_healthy_plain_create_does_not_adopt_a_concurrent_table(monkeypatch, outage, driver):
    cluster, configure = outage
    configure(driver=driver)
    cluster.alive.remove("h2")
    original = deepcopy(cluster.tables)
    raced = False
    submitted = []
    execute = Client.command

    def collide_at_server(connection, statement, **kwargs):
        nonlocal raced
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Create) and tree.this.this.name == "concurrent":
            submitted.append(tree)
            if not raced:
                raced = True
                cluster.create(
                    connection.host,
                    "CREATE TABLE db.concurrent (id UInt64) ENGINE=MergeTree ORDER BY id",
                )
                cluster.tables[(connection.host, "db.concurrent")].rows = [
                    (value,) for value in range(100)
                ]
            if not tree.args.get("exists"):
                message = "The table was concurrently created."
                raise _ConcurrentCreateError(message)
        return execute(connection, statement, **kwargs)

    monkeypatch.setattr(Client, "command", collide_at_server)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(_ConcurrentCreateError):
        sql.execute(
            "ch",
            "CREATE TABLE db.concurrent ON CLUSTER core (id UInt64) ENGINE=MergeTree ORDER BY id",
            ch_unavailable_shards="healthy_shards",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert raced
    assert len(submitted) == 1
    assert not submitted[0].args.get("exists")
    concurrent = cluster.tables.pop(("h1", "db.concurrent"))
    assert concurrent.rows == [(value,) for value in range(100)]
    assert cluster.tables == original
