"""Array commas remain within one Distributed sharding-key argument."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import exp, parse_one

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfiguration_preserves_array_engine_argument_boundaries(
    monkeypatch, write_sql_connections, driver
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state.host_tables[state.coordinator].pop("analytics.events")
    definition = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) "
        "ENGINE=Distributed('storage','analytics','events_shard',[1,2][1])",
        "analytics.events",
    )
    state._create(state.coordinator, definition)
    state.created.clear()
    original = state.snapshot()

    plan = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_sharding_key="id",
        ch_distributed_on_cluster="coordinator_only",
        dry_run=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    replacements = [step for step in plan.statements if step.phase == "create_replacement"]
    assert len(replacements) == 1
    create = parse_one(replacements[0].sql, read=ClickHouseDialect)
    engine = create.find(exp.EngineProperty).this
    assert isinstance(engine, exp.Anonymous)
    assert engine.name.casefold() == "distributed"
    assert len(engine.expressions) == 4
    assert isinstance(engine.expressions[-1], exp.Column)
    assert engine.expressions[-1].name == "id"
    assert state.snapshot() == original
    assert state.commands == []
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_creation_keeps_array_sharding_key_as_one_engine_argument(
    monkeypatch, write_sql_connections, driver
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    original = state.snapshot()

    assert (
        sql.create_table(
            "ch",
            "analytics.array_events",
            table_schema={"id": "UInt64"},
            ch_distributed_table=True,
            ch_shard_on_cluster="storage",
            ch_distributed_cluster="storage",
            ch_sharding_key="[id, id][1]",
            ch_engine="MergeTree",
            order_by=["id"],
            retry_cnt=1,
            timeout_increment=0,
        )
        is None
    )

    facade = state.host_tables[state.coordinator]["analytics.array_events"]
    engine = facade.create.find(exp.EngineProperty).this
    assert isinstance(engine, exp.Anonymous)
    assert engine.name.casefold() == "distributed"
    assert len(engine.expressions) == 4
    key = engine.expressions[-1]
    assert isinstance(key, exp.Bracket)
    assert [column.name for column in key.find_all(exp.Column)] == ["id", "id"]
    assert facade.route == ("storage", "analytics.array_events_shard")
    assert state.rows(state.remote, "analytics.array_events_shard") == []
    current = state.snapshot()
    assert {
        host: {name: current[host][name] for name in tables} for host, tables in original.items()
    } == original
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
