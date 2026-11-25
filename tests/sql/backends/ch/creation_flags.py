"""A CREATE candidate never receives the privileges of an acknowledged resource."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.outage import Client


class _ConcurrentCreateError(RuntimeError):
    code = 57


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("clustered", [False, True], ids=["local", "cluster"])
@pytest.mark.parametrize("mode", ["collision", "if_not_exists"])
def test_public_create_candidate_has_no_acknowledged_ownership_privileges(
    monkeypatch, outage, driver, clustered, mode
):
    cluster, configure = outage
    configure(driver=driver, cluster_routing=None)
    name = "candidate__stage__race"
    target = "db." + name
    original = deepcopy(cluster.tables)
    raced = False
    competing = None
    execute = Client.command

    def server_boundary(connection, statement, **kwargs):
        nonlocal raced, competing
        tree = parse_one(statement, read=ClickHouseDialect)
        if mode == "collision" and isinstance(tree, exp.Create) and tree.this.this.name == name:
            if not raced:
                raced = True
                cluster.create(
                    connection.host,
                    f"CREATE TABLE {target} (id UInt64) ENGINE=MergeTree ORDER BY id",
                )
                cluster.tables[(connection.host, target)].rows = [(777,)]
                competing = deepcopy(cluster.tables)
            message = "A competing table already exists."
            raise _ConcurrentCreateError(message)
        return execute(connection, statement, **kwargs)

    monkeypatch.setattr(Client, "command", server_boundary)
    clause = " ON CLUSTER core" if clustered else ""
    modifier = " IF NOT EXISTS" if mode == "if_not_exists" else ""
    statement = f"CREATE TABLE{modifier} {target}{clause} (id UInt64) ENGINE=MergeTree ORDER BY id"
    with backend_operation() as operation:
        if mode == "collision":
            with pytest.raises(_ConcurrentCreateError):
                sql.execute("ch", statement, retry_cnt=1, timeout_increment=0)
        else:
            assert sql.execute("ch", statement, retry_cnt=1, timeout_increment=0) is None

        assert not operation.resources.get(("ch_owned", "ch", ("db", name)))
        assert not operation.resources.get(("ch_owned_local", "ch", ("db", name)))

    if mode == "collision":
        assert raced
        assert cluster.tables == competing
    else:
        expected_hosts = cluster.hosts if clustered else ["h1"]
        for host in expected_hosts:
            assert cluster.tables[(host, target)].rows == []
        for key, value in original.items():
            assert cluster.tables[key] == value
