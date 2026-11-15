"""Only a fully verified existing layout may reach a reconfiguration cutover."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.reconfigure_pair_scope import (
    PairScopeClient,
    configure_pair_scope,
    connect_pair_scope,
)


@pytest.mark.parametrize("physical_column", ["id UInt32", "id UInt64 DEFAULT 7"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "execute"])
def test_ownership_certification_rejects_a_same_named_pair_with_different_column_identity(
    monkeypatch, write_sql_connections, physical_column, dry_run
):
    configure_pair_scope(write_sql_connections, "core")
    raw = PairScopeClient(managed_remote_facade=True)
    physical = raw.host_tables[raw.remote]["analytics.events_shard"]
    physical.create = parse_create_table(
        f"CREATE TABLE analytics.events_shard ({physical_column}) ENGINE=MergeTree ORDER BY id",
        "analytics.events_shard",
    )
    physical.schema = {
        column.name: column.kind.sql(dialect=ClickHouseDialect)
        for column in physical.create.this.expressions
    }
    original = raw.snapshot()
    connect_pair_scope(monkeypatch, raw)

    with pytest.raises(InvalidSqlInputError, match="not a managed Distributed/_shard pair"):
        sql.ch_reconfigure_table(
            "ch",
            raw.source_table,
            order_by="id + 1",
            ch_distributed_on_cluster="core",
            retry_cnt=3,
            timeout_increment=0,
            dry_run=dry_run,
        )

    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.commands == []
    assert raw.snapshot() == original
    assert raw.host_tables[raw.remote]["analytics.events_shard"] is physical
    assert raw.rows(raw.remote, "analytics.events_shard") == [(1,), (2,), (3,)]
    assert raw.closed


@pytest.mark.parametrize("facade_scope", [None, "coordinator_only"])
def test_ownership_certification_requires_a_known_physical_scope_before_local_pair_creation(
    monkeypatch, write_sql_connections, facade_scope
):
    path = write_sql_connections(
        {
            "ch": {
                "type": "ch",
                "driver": "native",
                "host": "coordinator",
                "port": 9000,
                "user": "user",
                "password": "password",
                "database": "analytics",
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": True,
                        "shard": {"engine": "MergeTree", "on_cluster": None},
                        "distributed": {
                            "engine_template": (
                                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                            ),
                            "cluster": "core",
                            "on_cluster": facade_scope,
                            "sharding_key": "rand()",
                        },
                    }
                },
            }
        }
    )
    set_connections_path(path)
    raw = PairScopeClient()
    raw.host_tables[raw.coordinator].pop(raw.source_table)
    local = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id",
        raw.source_table,
    )
    raw._create(raw.coordinator, local, [(1,), (2,), (3,)])
    raw.created.clear()
    original = raw.snapshot()
    connect_pair_scope(monkeypatch, raw)

    with pytest.raises(InvalidSqlInputError, match="verified physical and routing clusters"):
        sql.ch_reconfigure_table(
            "ch",
            raw.source_table,
            ch_distributed_table=True,
            retry_cnt=3,
            timeout_increment=0,
        )

    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.commands == []
    assert raw.snapshot() == original
    assert raw.rows(raw.coordinator, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.rows(raw.remote, raw.source_table) == raw.remote_original
    assert raw.closed
