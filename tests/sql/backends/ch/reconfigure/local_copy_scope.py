"""A verified local rebuild must copy its own data despite automatic routing."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import retarget_create
from sqlglot import parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.outage import Client
from tests.sql._support.reconfigure_conversion_scope import _rename_pairs, _scope_sql


def _install_atomic_exchange(monkeypatch):
    original = Client.command

    def command(client, sql, **kwargs):
        cluster, body = _scope_sql(strip_query_label(sql))
        if not body.upper().startswith("EXCHANGE TABLES "):
            return original(client, sql, **kwargs)
        assert cluster is None
        client.cluster.commands.append((client.host, sql, dict(kwargs.get("settings") or {})))
        for source, target in _rename_pairs(body, exchange=True):
            tables = client.cluster.tables
            left, right = (client.host, source), (client.host, target)
            tables[left], tables[right] = tables[right], tables[left]
            for key in (left, right):
                value = tables[key]
                value.ddl = retarget_create(
                    parse_one(value.ddl, read=ClickHouseDialect), key[1], None
                ).sql(dialect=ClickHouseDialect)
        return None

    monkeypatch.setattr(Client, "command", command)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_local_rebuild_preserves_certified_source_over_routed_homonym(
    outage, monkeypatch, driver
):
    _install_atomic_exchange(monkeypatch)
    cluster, configure = outage
    configure(driver=driver, ddl_defaults=None, ddl_ready_timeout_seconds=1)
    cluster.members_by_cluster["core"] = [(1, 1, "h2", 9000)]
    cluster.tables.clear()
    for host, value in (("h1", 1), ("h2", 9)):
        cluster.create(host, "CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY id")
        cluster.tables[(host, "db.events")].rows = [(value,)]
    remote = cluster.tables[("h2", "db.events")]
    local_identity = cluster.tables[("h1", "db.events")].uuid
    remote_identity = remote.uuid
    remote_ddl = remote.ddl

    result = sql.ch_reconfigure_table(
        "ch",
        "db.events",
        order_by="id + 1",
        ch_distributed_table=False,
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.metadata.row_count_validated is True
    assert result.metadata.source_rows == 1
    assert result.metadata.final_target_rows == 1
    assert cluster.tables[("h1", "db.events")].rows == [(1,)]
    assert cluster.tables[("h1", "db.events")].uuid != local_identity
    assert cluster.tables[("h2", "db.events")].rows == [(9,)]
    assert cluster.tables[("h2", "db.events")].uuid == remote_identity
    assert cluster.tables[("h2", "db.events")].ddl == remote_ddl
    assert set(cluster.tables) == {("h1", "db.events"), ("h2", "db.events")}
