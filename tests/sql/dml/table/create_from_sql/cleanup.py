from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import DropScopeState, _DropConnection
from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    FakeDbapiConnection,
    create_module,
)


def test_drop_target_if_exists_drops_clickhouse_distributed_pair(
    monkeypatch, write_sql_connections
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["ch"].update(
        host="coordinator",
        database="analytics",
        cluster_routing=None,
        ddl_defaults={
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                "distributed": {
                    "engine_template": (
                        "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                    ),
                    "cluster": "storage",
                    "on_cluster": None,
                    "sharding_key": "rand()",
                },
            },
            "staging": deepcopy(DEFAULT_SQL_CONNECTIONS["ch"]["ddl_defaults"]["staging"]),
        },
    )
    set_connections_path(write_sql_connections(connections))
    state = DropScopeState()
    state.clusters["storage"] = (state.coordinator,)
    for table in ("analytics.events", "analytics.events_shard"):
        state.host_tables[state.coordinator].pop(table)
    columns = "(id UInt64, amount Decimal(12, 2))"
    physical = parse_create_table(
        f"CREATE TABLE analytics.events_shard {columns} ENGINE=MergeTree ORDER BY id",
        "analytics.events_shard",
    )
    facade = parse_create_table(
        f"CREATE TABLE analytics.events {columns} "
        "ENGINE=Distributed('storage', 'analytics', 'events_shard', rand())",
        "analytics.events",
    )
    state._create(state.coordinator, physical, [(7, Decimal("1.25"))])
    state._create(state.coordinator, facade)
    before = state.snapshot()
    target = _DropConnection(state, state.coordinator, native=False)

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        message = f"Unexpected connection key: {connection_key}"
        raise AssertionError(message)

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        insert_data=False,
        drop_target_if_exists=True,
    )

    statements = [
        parse_one(strip_query_label(statement), read=ClickHouseDialect)
        for statement in state.commands
    ]
    resources = [
        (
            f"{table.db}.{table.name}",
            drop.find(exp.OnCluster).this.name if drop.find(exp.OnCluster) else None,
        )
        for drop in statements
        if isinstance(drop, exp.Drop)
        for table in drop.args.get("tables") or [drop.this]
    ]
    assert resources == [
        ("analytics.events", None),
        ("analytics.events_shard", "storage"),
    ]
    assert all(isinstance(statement, exp.Drop) for statement in statements[:2])
    assert all(isinstance(statement, exp.Create) for statement in statements[2:])
    actual = state.host_tables[state.coordinator]
    assert set(actual) == {"analytics.events", "analytics.events_shard"}
    assert actual["analytics.events"].route == ("storage", "analytics.events_shard")
    assert actual["analytics.events"].schema == actual["analytics.events_shard"].schema
    assert list(actual["analytics.events"].schema) == ["id", "amount"]
    assert state.rows(state.coordinator, "analytics.events") == []
    for table in actual:
        assert actual[table].uuid != before[state.coordinator][table]["uuid"]
    assert state.snapshot()[state.remote] == before[state.remote]
    assert state.query_settings
    assert any(settings == {"skip_unavailable_shards": 0} for settings in state.query_settings)
