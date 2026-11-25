"""A partition operation never infers physical ownership from a suffix."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_partition_scope import install_partition_scope


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("layout", ["local", "pair"])
def test_public_drop_partitions_mutates_only_verified_existing_storage(
    monkeypatch, write_sql_connections, driver, layout
):
    state = install_partition_scope(
        monkeypatch, write_sql_connections, driver=driver, layout=layout
    )
    before = state.snapshot()

    assert (
        sql.drop_partitions("ch", "analytics.events", ["old"], retry_cnt=1, timeout_increment=0)
        is None
    )

    expected = before
    host = state.coordinator if layout == "local" else state.remote
    table = "analytics.events" if layout == "local" else "analytics.events_shard"
    expected[host][table]["rows"] = [("kept", 2)]
    assert state.snapshot() == expected
    assert len(state.commands) == 1
    statement = parse_one(strip_query_label(state.commands[0]), read=ClickHouseDialect)
    assert isinstance(statement, exp.Alter)
    assert statement.this.name == table.split(".")[1]
    cluster = statement.find(exp.OnCluster)
    assert (cluster is None) is (layout == "local")
    if cluster is not None:
        assert cluster.this.name == "storage"
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_drop_partitions_rejects_externally_owned_storage_before_mutation(
    monkeypatch, write_sql_connections, driver
):
    state = install_partition_scope(
        monkeypatch, write_sql_connections, driver=driver, layout="external"
    )
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"[Oo]wn|[Ee]xternal"):
        sql.drop_partitions("ch", "analytics.events", ["old"], retry_cnt=1, timeout_increment=0)

    assert state.snapshot() == before
    assert state.commands == []
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
